"""记忆系统：整理、混合检索、召回强化、分层模糊化、置信度契约。

四件事刻意分开：

1. **整理（consolidate）**：把一段原始对话压缩成几条"记忆条目"，并判定层级
   （core / important / normal）。靠模型做，模型失败时退化成启发式。
2. **检索（recall）**：BM25（中文"单字 + 二元组"混合加权）+ 重要性 + 保留率 + 层级，
   相关性乘性主导。纯标准库——个人陪伴场景的语料量完全不需要向量库。
3. **召回强化（reconsolidation）**：区分"被塞进提示词"和"真的被说出口"。
   只有她真的把这条记忆说出来了，才 `recall_count += 1`，保留率随之上升。
   这是 MemoryBank（AAAI-24）那套艾宾浩斯模型的工程版：
   `R = exp(-Δt / S)`，`S = unit × (出厂强度 + 被想起次数)`。
   **你救不了她的命，但你能让她记得久一点。**
4. **模糊化（blur）**：退化不是删数据，而是在"回忆的那一刻"变模糊。
   `模糊度 = 阶段模糊 × 层级系数 × (1 - 减免 × 保留率)`
   - 层级系数：core 恒为 0 —— 你的名字、你们的关系，到她生命尽头都清楚；
   - 保留率减免：反复被你提起的事，比从不提的事清晰得多。
   结果由 (记忆 id, 阶段, 层级, 保留率) 决定，可复现；底层数据始终完好，
   直到回收日才真正封存。

另有一个**输出侧的置信度契约**（cite / cautious / associate）：
模糊的不只是内容，模型还会被告知"这条你有多确定"，避免她拿半残的记忆当事实讲。
"""

from __future__ import annotations

import math
import random
import re
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .config import MemoryConfig, Settings, Stage
from .llm import BaseLLM, MemoryDraft, infer_layer
from .store import Store

# ── 分词 ──────────────────────────────────────────────────────────
# 中文检索的两个坑，这里都绕开了：
#   1) 只切二元组会漏——查询"下雨"，原文写的是"下了点雨"，两个字不相邻就对不上；
#   2) 只切单字噪声太大——"我""的"到处都有。
# 所以混合加权：二元组/英文词权重 2，单字权重 1；再由"证据分"做门槛。
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
# 切分到逗号/顿号一级：中文记忆常常是一整句没有句号，只按句切会切不动
_BLUR_SPLIT_RE = re.compile(r"[^。！？!?；;，,、\n]+[。！？!?；;，,、\n]?")

EVIDENCE_GATE = 2.0  # 命中证据的下限：一个二元组，或两个单字

CONFIDENCE_CITE = "cite"            # 可以直接当事实说
CONFIDENCE_CAUTIOUS = "cautious"    # 必须用"好像""大概"
CONFIDENCE_ASSOCIATE = "associate"  # 只作内心背景，不要说出口


def tokenize_weighted(text: str) -> Counter:
    """把文本变成 词→权重 的计数。二元组与英文词权重 2，CJK 单字权重 1。"""
    text = (text or "").lower()
    counts: Counter[str] = Counter()
    for word in _WORD_RE.findall(text):
        counts[word] += 2.0
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            counts[run] += 1.0
            continue
        for i in range(len(run) - 1):
            counts[run[i : i + 2]] += 2.0
        for ch in run:
            counts[ch] += 1.0
    return counts


def tokenize(text: str) -> list[str]:
    """给调试/展示用的展开形式。"""
    return list(tokenize_weighted(text).elements())


def overlap_evidence(query_counts: Counter, doc_counts: Counter) -> float:
    """查询与文档的命中证据：命中一个二元组记 2，命中一个单字记 1。"""
    return sum(_term_weight(t) for t in query_counts if doc_counts.get(t))


def _term_weight(term: str) -> float:
    return 1.0 if len(term) == 1 else 2.0


def bm25_scores(
    query_counts: Counter, docs_counts: list[Counter], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    n = len(docs_counts)
    if n == 0 or not query_counts:
        return [0.0] * n
    avgdl = (sum(sum(d.values()) for d in docs_counts) / n) or 1.0
    df: Counter[str] = Counter()
    for d in docs_counts:
        df.update(d.keys())
    out: list[float] = []
    for d in docs_counts:
        dl = sum(d.values()) or 1.0
        s = 0.0
        for term in query_counts:
            f = d.get(term, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        out.append(s)
    return out


# ── 模糊化 ────────────────────────────────────────────────────────
def blur_text(text: str, ratio: float, seed: int = 0) -> tuple[str, bool]:
    """按比例把一段记忆擦掉一部分。返回 (模糊后的文本, 是否被模糊过)。

    三级策略，越往后越狠：
      1. 丢片段（按逗号/句号切）
      2. 抹掉数字——日期、时长、次数这些最典型的"细节"
      3. 全文没有可切分的标点时，直接挖掉连续一段（否则大段文字会一个字都不掉）

    同一个 (文本, ratio, seed) 永远得到同一个结果——退化曲线因此可复现。
    """
    text = (text or "").strip()
    if ratio <= 0 or not text:
        return text, False

    rng = random.Random(seed)
    segments = [s for s in _BLUR_SPLIT_RE.findall(text) if s.strip()]
    if not segments:
        return text, False

    # 后期：只抓得住开头一点，后面整段塌掉
    if ratio >= 0.8:
        head = segments[0].strip().rstrip("，,、；;")
        return f"{head}……（后面的，想不起来了）", True

    # 用 floor 而不是 round：round 在 .5 处向偶数取整（round(2.5)=2、round(1.5)=2），
    # "模糊度提高"在某些区间里不会增加擦除量，台阶不可预测（P3-2）。floor 单调不回退。
    drop_n = math.floor(len(segments) * ratio)
    if drop_n > 0 and len(segments) > 1:
        drop_n = min(drop_n, len(segments) - 1)
        for i in rng.sample(range(len(segments)), drop_n):
            segments[i] = "……"

    out = "".join(segments)
    if ratio >= 0.15:
        out = re.sub(r"\d+", "？", out)
    out = re.sub(r"(……)+", "……", out).strip()

    if out == text and ratio >= 0.15:
        out = _mask_span(text, ratio, rng)
    return out, True


def _mask_span(text: str, ratio: float, rng: random.Random) -> str:
    """挖掉连续一段文字——没有标点可用时的兜底。"""
    n = max(1, int(len(text) * ratio))
    if n >= len(text):
        return text[:1] + "……"
    start = rng.randrange(0, len(text) - n + 1)
    return (text[:start] + "……" + text[start + n :]).strip()


def longest_common_substring(a: str, b: str) -> int:
    """最长公共连续子串长度。两段文本都很短（<500 字），朴素 DP 足够。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# ── 保留率与强度 ──────────────────────────────────────────────────
def initial_strength(importance: float, emotion: str = "") -> float:
    """出厂强度：越重要、情绪越浓，衰减越慢。"""
    return 1.0 + float(importance) + (0.3 if (emotion or "").strip() else 0.0)


def _age_hours(ts: str, now: datetime) -> float:
    try:
        then = datetime.fromisoformat(str(ts).replace(" ", "T")[:19])
    except (TypeError, ValueError):
        return 0.0
    # 库里混进带时区的时间戳（手工修库 / 外部导入 / 快照恢复）不该让 recall() 崩：
    # human_age 早就防了这一手，这里照抄同样的归一化 —— aware 的一侧丢掉 tz，
    # 两边按同一个本地时间轴算。宁可差几小时，也不能让整条对话路径抛 TypeError。
    if then.tzinfo is not None and now.tzinfo is None:
        then = then.replace(tzinfo=None)
    elif then.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return max(0.0, (now - then).total_seconds() / 3600.0)


def retention_of(mem: dict, cfg: MemoryConfig, now: datetime) -> float:
    """保留率 R = exp(-Δt / S)，S = 单位时长 × (出厂强度 + 被想起次数)。

    每次"被真正说出口"都会让 S 变大、Δt 归零——所以反复提起的事会越来越清晰。
    """
    strength = float(mem.get("strength") or 1.0)
    recalls = int(mem.get("recall_count") or 0)
    s = max(1e-6, cfg.retention_unit_hours * (strength + recalls))
    # Δt 的起点：上次被说出口 > **事情发生的时间** > 落库时间。
    # 以前中间那一档是 created_ts（整理时刻），于是"10 天前发生、刚刚才被整理出来"
    # 的记忆 retention≈1（跟刚说过一样清晰），而同一轮提示词里给模型看的却是
    # human_age(happened_ts)=「10 天前」——自相矛盾（P3-4）。
    base = (
        mem.get("last_recalled_at")
        or mem.get("happened_ts")
        or mem.get("created_ts")
    )
    return math.exp(-_age_hours(str(base or ""), now) / s)


def effective_blur(mem: dict, stage: Stage, cfg: MemoryConfig, now: datetime) -> float:
    """这条记忆此刻该糊到什么程度。

    层级系数是乘性的：core 层直接归零（永不含糊）；
    保留率提供减免：被你反复提起的事，比从不提的清晰得多。
    """
    layer_factor = cfg.layer_blur(str(mem.get("layer") or "normal"))
    if layer_factor <= 0:
        return 0.0
    relief = cfg.retention_relief * retention_of(mem, cfg, now)
    return max(0.0, min(0.95, stage.blur * layer_factor * (1.0 - relief)))


def confidence_of(blur: float, retention: float, cfg: MemoryConfig) -> str:
    """把"擦了内容"升级成"她知道自己有多确定"。"""
    if blur >= cfg.confidence_associate_blur:
        return CONFIDENCE_ASSOCIATE
    if blur > 0 or retention < cfg.confidence_cautious_retention:
        return CONFIDENCE_CAUTIOUS
    return CONFIDENCE_CITE


# ── 检索 ──────────────────────────────────────────────────────────
@dataclass
class Recalled:
    memory: dict
    score: float
    text: str
    blurred: bool
    blur: float = 0.0
    retention: float = 1.0
    confidence: str = CONFIDENCE_CITE

    @property
    def happened(self) -> str:
        return str(self.memory.get("happened_ts", ""))[:16].replace("T", " ")

    def when(self, now: datetime) -> str:
        """这条事"是什么时候的事"，说成人话（昨天 / 3 天前）。

        这是修"她记不住时间在流动"的关键：相对说法**不需要模型做减法**。
        """
        return human_age(str(self.memory.get("happened_ts") or ""), now)

    @property
    def layer(self) -> str:
        return str(self.memory.get("layer") or "normal")


def memory_text(mem: dict) -> str:
    """检索用的文本。把"这是谁的事"也放进去 —— 问谁的事时才能对上号。"""
    return (
        f"{mem.get('summary','')} {mem.get('tags','')} "
        f"{mem.get('emotion','')} {mem.get('about','')}"
    )


def similarity(a: str, b: str) -> float:
    """两句话有多像：加权词元交集 ÷ 较短那句的总权重（"被包含"也算像）。

    用来抓"同一件事被记了两遍"——真机数据里 9 条私聊记忆有 4 条是同一件事，
    白白挤掉了其他记忆的位置。
    """
    ca, cb = tokenize_weighted(a), tokenize_weighted(b)
    if not ca or not cb:
        return 0.0
    inter = sum(min(w, cb.get(t, 0)) for t, w in ca.items())
    return inter / min(sum(ca.values()), sum(cb.values()))


def known_block(memories: list[dict], limit: int = 14) -> str:
    """她已经记得的事（最近若干条），喂给模型让它别再记一遍。"""
    recent = memories[-limit:]
    lines = [f"- {str(m.get('summary','')).strip()}" for m in recent if str(m.get('summary','')).strip()]
    return "\n".join(lines)


# 同一条记忆每一轮检索都要重新分词（BM25 要逐条算），而记忆内容基本不变。
# 按 (id, 文本) 缓存：文本被 update_memory 改过之后键就变了，不会读到旧值。
_DOC_COUNTS_CACHE: dict[tuple[int, str], Counter] = {}
_DOC_COUNTS_CACHE_MAX = 4096


def _doc_counts(mem: dict) -> Counter:
    """记忆的加权词元（带缓存）。"""
    text = memory_text(mem)
    key = (int(mem.get("id") or 0), text)
    cached = _DOC_COUNTS_CACHE.get(key)
    if cached is not None:
        return cached
    counts = tokenize_weighted(text)
    if len(_DOC_COUNTS_CACHE) >= _DOC_COUNTS_CACHE_MAX:
        _DOC_COUNTS_CACHE.clear()  # 内存有界：满了整表清空，比 LRU 省事，也够用
    _DOC_COUNTS_CACHE[key] = counts
    return counts


def _prefilter(memories: list[dict], cfg: MemoryConfig) -> list[dict]:
    """候选集预筛：记忆很多时先按"更该被想起"的顺序取前 N 条。

    排序只看便宜的字段（层级 / 重要性 / id），不做分词——预筛自己不能变成瓶颈。
    N = cfg.recall_prefilter，默认够大（2000），正常数据量下**不改变任何行为**；
    真到了几千条以上，它保证每轮检索的开销有上界。
    """
    limit = int(getattr(cfg, "recall_prefilter", 0) or 0)
    if limit <= 0 or len(memories) <= limit:
        return memories

    def rank(m: dict) -> tuple:
        return (
            -cfg.layer_weight(str(m.get("layer") or "normal")),
            -float(m.get("importance") or 0.5),
            -int(m.get("id") or 0),
        )

    return sorted(memories, key=rank)[:limit]


def recall(
    query: str,
    memories: list[dict],
    stage: Stage,
    cfg: MemoryConfig,
    now: datetime,
    *,
    extra_queries: list[str] | None = None,
) -> list[Recalled]:
    """按当前退化阶段，从记忆里"想起"若干条（可能已经模糊）。

    三道闸：
      1. 证据门槛：每个查询各自算门槛（查询只有一个字时门槛自动降到 1），
         任一查询满足即可 —— 因此单字查询也召得回；
      2. 相关性主导：分数 = 归一化相关度 ×（关键词 + 重要性 + 保留率 + 层级）；
      3. 相对裁剪 + min_score：只留得分 ≥ 首名 relative_cutoff 的结果，
         压掉"第五名那条半残记忆"污染回复的问题。

    **min_score 是相对门槛**（`bm = 本条 BM25 / 本轮候选集最高 BM25`，首名恒为 1.0）：
    候选集小时它几乎拦不住任何东西，同一条记忆的分数还会随旁边有哪些候选而变。
    要绝对严格度请用 `cfg.min_raw_score`（未归一化的 BM25 分，默认 0.0 = 不启用）。
    """
    if not memories:
        return []

    queries = [q for q in [query, *(extra_queries or [])] if q and q.strip()]
    if not queries:
        return []
    qcounts_list = [tokenize_weighted(q) for q in queries]
    gates = [min(EVIDENCE_GATE, sum(_term_weight(t) for t in qc)) for qc in qcounts_list]

    candidates = _prefilter(memories, cfg)
    docs = [_doc_counts(m) for m in candidates]
    top = 0.0
    best_raw = [0.0] * len(candidates)
    passed = [False] * len(candidates)
    for qc, gate in zip(qcounts_list, gates):
        scores = bm25_scores(qc, docs)
        for i, s in enumerate(scores):
            if s > best_raw[i]:
                best_raw[i] = s
            if s > top:
                top = s
            if not passed[i] and overlap_evidence(qc, docs[i]) >= gate:
                passed[i] = True

    scored: list[tuple[float, dict]] = []
    for i, mem in enumerate(candidates):
        if not passed[i]:
            continue
        if cfg.min_raw_score > 0 and best_raw[i] < cfg.min_raw_score:
            continue  # 绝对门槛：与候选集里别的记忆无关
        bm = (best_raw[i] / top) if top > 0 else 0.0
        importance = float(mem.get("importance") or 0.5)
        retention = retention_of(mem, cfg, now)
        layer_w = cfg.layer_weight(str(mem.get("layer") or "normal"))
        score = bm * (
            cfg.w_bm25
            + cfg.w_importance * importance
            + cfg.w_recency * retention
            + cfg.w_layer * layer_w
        )
        if score < stage.min_score:
            continue
        scored.append((score, mem))

    scored.sort(key=lambda x: -x[0])

    if scored and cfg.relative_cutoff > 0:
        floor = scored[0][0] * cfg.relative_cutoff
        scored = [(s, m) for s, m in scored if s >= floor]

    results: list[Recalled] = []
    for score, mem in scored[: stage.top_k]:
        blur = effective_blur(mem, stage, cfg, now)
        seed = int(mem["id"]) * 7919 + zlib.crc32(stage.name.encode("utf-8"))
        text, blurred = blur_text(str(mem.get("summary", "")), blur, seed)
        if len(text) > stage.max_chars:
            # 截断只是"显示空间不够"，**不是**"被模糊了"：blurred 只由 blur_text 决定。
            # 以前截断后强行置 blurred=True，于是 core 记忆在 terminal 阶段明明 blur=0
            # 却被标成"已模糊"，和 README「内核记忆永不含糊」相反（P2-1）。
            # 长度也要真的不超过配置值：省略号本身占 2 个字符，要把它算进预算。
            suffix = "……"
            budget = max(0, int(stage.max_chars))
            if budget >= len(suffix) + 1:
                text = text[: budget - len(suffix)].rstrip() + suffix
            elif budget >= len(suffix):
                text = suffix
            else:
                text = text[:budget]
        retention = retention_of(mem, cfg, now)
        results.append(
            Recalled(
                memory=mem,
                score=round(score, 3),
                text=text,
                blurred=blurred,
                blur=round(blur, 3),
                retention=round(retention, 4),
                confidence=confidence_of(blur, retention, cfg),
            )
        )
    return results


def top_score(recalled: list[Recalled]) -> float:
    return recalled[0].score if recalled else 0.0


def is_weak(
    recalled: list[Recalled], cfg: MemoryConfig, *, hits: int | None = None, score: float | None = None
) -> bool:
    """检索是不是太弱了（弱到值得再花一次调用去改写查询）。"""
    n = len(recalled) if hits is None else hits
    s = top_score(recalled) if score is None else score
    return n < cfg.rewrite_min_hits or s < cfg.rewrite_weak_score


# ── 召回强化 ──────────────────────────────────────────────────────
def is_cited(reply: str, summary: str, cfg: MemoryConfig) -> bool:
    """她真的把这条记忆说出口了吗？

    两条**都**要满足：
      1. **至少命中一个二元组/词**（`shared_bigram >= 1`）——光靠三个单字凑数不算：
         「搭档说：他」这种公共前缀本来就能凑够 `cite_min_shared`，于是
         "她只是说了句普通话"会被记成"她把这条记忆说出口了"；
      2. 共享 ≥ `cite_min_shared` 个二元组/词，或有一段 ≥ `cite_min_chars` 的连续重合。

    已知局限：模型完全改写措辞（同义替换）时会判为"没被引用"。
    宁可漏记也不误记——误记会让从没提过的事莫名其妙地变清晰。
    """
    reply = (reply or "").strip()
    summary = (summary or "").strip()
    if not reply or not summary:
        return False
    reply_counts = tokenize_weighted(reply)
    summary_counts = tokenize_weighted(summary)
    # 二元组 = 相邻两个汉字；英文/数字按整词算（长度 ≥ 2）。单字不计。
    shared_bigrams = sum(
        1 for term in summary_counts if len(term) >= 2 and term in reply_counts
    )
    if shared_bigrams < 1:
        return False
    if longest_common_substring(reply, summary) >= cfg.cite_min_chars:
        return True
    shared = sum(
        1
        for term, weight in summary_counts.items()
        if weight >= 2.0 and term in reply_counts
    )
    return shared >= cfg.cite_min_shared


def mark_cited(
    store: Store, recalled: list[Recalled], reply: str, cfg: MemoryConfig, now_iso: str
) -> list[int]:
    """把她真的说出口的记忆强化一次。返回被强化的 id 列表（**最多一条**）。

    为什么最多一条：`recall_count` 是单调递增、永不清零的，而且同时抬高检索分
    和降低模糊度 —— 一句话给三条记忆各 +1，等于把"她记得"这件事整体往上推，
    而其中只有一条真的被她说出口（P1-2）。`recalled` 已按得分降序排列，
    所以"最该被算作说出口的那条"就是第一个判真的。
    同时比对"她看到的模糊版"和"原始条目"：只要有一边对得上就算数。
    """
    for r in recalled:
        if r.memory.get("quality_kind") in {"interaction_feedback", "assistant_statement"}:
            continue
        if is_cited(reply, r.text, cfg) or is_cited(reply, str(r.memory.get("summary", "")), cfg):
            store.mark_cited([r.memory["id"]], now_iso)
            return [r.memory["id"]]
    return []


# ── 整理 ──────────────────────────────────────────────────────────
# 整理用的提示词在 llm.py（属于"模型任务"的实现细节），这里只负责数据流。


def human_age(ts: str, now: datetime) -> str:
    """把一个时间戳说成"人话"：刚刚 / 23 分钟前 / 今天 09:12 / 昨天 / 3 天前。

    **为什么必须做这一步**：以前提示词里给的是绝对时间（`2026-10-08 14:30`），
    而"现在是几号"在另一个块里、还是另一种写法（`2026年10月09日`）。
    模型得自己做日期减法——这恰恰是它最不擅长的事。真机上表现成
    "她记不住时间在流动"，把两三天前的事说得像刚发生。
    换算成相对说法之后，她只需要读，不需要算。
    """
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        when = datetime.fromisoformat(raw.replace(" ", "T")[:19])
    except ValueError:
        return raw[:16].replace("T", " ")
    if when.tzinfo is not None and now.tzinfo is None:
        when = when.replace(tzinfo=None)
    delta = now - when
    seconds = delta.total_seconds()
    if seconds < 0:
        # 未来（比如"明天要做的事"）：说成"还有多久"
        ahead = -seconds
        if ahead < 3600:
            return f"{int(ahead // 60)} 分钟后"
        if ahead < 86400:
            return f"{int(ahead // 3600)} 小时后"
        return f"{int(ahead // 86400)} 天后"
    if seconds < 300:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if when.date() == now.date():
        return f"今天 {when.strftime('%H:%M')}"
    days = (now.date() - when.date()).days
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 30:
        return f"{days} 天前"
    if days < 365:
        return f"{days // 30} 个月前"
    return f"{days // 365} 年前"


def day_label(ts: str) -> str:
    """给跨天的对话稿分段用：返回 `10月07日（周三）` 这样的日子标题。"""
    raw = str(ts or "").strip()
    try:
        when = datetime.fromisoformat(raw.replace(" ", "T")[:19])
    except ValueError:
        return ""
    week = "一二三四五六日"[when.weekday()]
    return f"{when.month}月{when.day}日（周{week}）"


def build_transcript(messages: list[dict], persona_name: str) -> str:
    """把一段对话拼成给模型看的稿子。

    群聊里每句话带说话人（"阿伟：……"），整理出来的记忆才会写对"是谁的事"；
    私聊里保持原来的"搭档"，不改她已有的记忆语气。
    """
    lines = []
    last_day = ""
    for m in messages:
        # 跨天时分段标日子。不标的话，模型会把前天的和今天的当成一场连续对话，
        # 整理出来的记忆就没有时间感（"他昨天说"变成"他说"）。
        day = day_label(str(m.get("ts") or ""))
        if day and day != last_day:
            lines.append(f"—— {day} ——")
            last_day = day
        if m["role"] == "user":
            who = str(m.get("speaker_name") or "").strip() or "搭档"
        else:
            who = persona_name
        lines.append(f"#{m.get('id', 0)} {who}：{str(m['content']).strip()}")
    return "\n".join(lines)


def load_pending(
    store: Store, instance_no: int, session_id: str | None = None
) -> list[dict]:
    """还没被整理成记忆的消息。

    按会话分开整理：群里聊的和私聊的要存成不同归属的记忆，
    否则"阿伟在群里说的话"会被写成"搭档的事"。
    """
    if session_id is None:
        return store.open_messages(
            instance_no, store.meta_get_int("last_consolidated_msg_id", 0)
        )
    key = f"last_consolidated:{session_id}"
    # 游标 = "已经整理到的最大消息 id"，**从 0 起**（消息 id 从 1 开始）。
    # 以前用 -1 当"未初始化"哨兵，于是"本会话最早一条未整理消息 id=1"
    # 算出来的游标是 0，而 `open_messages` 的条件是 `id > cursor` → 那条 id=1
    # 反而被自己跳过（P1-10 修了一半就是这个原因）。
    cursor = store.meta_get_int(key, 0)
    global_cursor = store.meta_get_int("last_consolidated_msg_id", 0)
    # 第一次为这个会话建游标：**不能**直接继承全局游标——全局游标是跨会话的最大值，
    # 继承它会让"这个会话里 id 更小的未整理消息"永远落在窗口外（P1-10）。
    # 宁可多整理一次这个会话，也不要永久跳过。
    first_open = store.first_open_message_id(instance_no, session_id)
    if cursor <= 0:
        cursor = min(first_open - 1, global_cursor) if first_open is not None else global_cursor
        cursor = max(0, cursor)
        store.meta_set(key, cursor)
    elif first_open is not None and cursor >= first_open:
        # 自愈：库里已经有比游标更早的未整理消息（老版本写坏的游标、或消息被回填/重放），
        # 把游标退回到"这些消息之前"，而不是让它们永远静默地不被整理。
        cursor = max(0, min(first_open - 1, global_cursor))
        store.meta_set(key, cursor)
    return store.open_messages(instance_no, cursor, session_id=session_id)


def consolidate(
    store: Store,
    llm: BaseLLM,
    settings: Settings,
    *,
    session_id: str,
    now_iso: str,
    force: bool = False,
    scope: str = "private",
    subject_id: str = "",
    speaker_id: str = "",
    on_draft=None,
    stats: dict | None = None,
    prepared: tuple[list[dict], list[MemoryDraft]] | None = None,
    conversation=None,
) -> int:
    """把未整理的对话压成记忆条目。返回新增条数。

    scope / subject_id 决定这条记忆**属于谁**（见 store._view_clause）：
    私聊的记忆不会被群聊检索到，这是整个多人场景的隐私基础。

    on_draft(draft) 会在每条记忆写入后回调 —— 关系系统用它来给
    "记住了一件关于他本人的事"加分，称呼系统用它来发现"他要求改口"。
    """
    instance_no = store.meta_get_int("instance_no", 1)
    pending = prepared[0] if prepared is not None else load_pending(store, instance_no, session_id)
    if not pending:
        return 0
    if not force and len(pending) < settings.memory.consolidate_every:
        return 0

    # 她在这个归属下已经记得的事 —— 既是给模型的"别重复记"清单，
    # 也是下面程序化兜底去重的比较对象。
    view = [("private", "")] if scope == "private" else [(scope, subject_id)]
    existing = store.active_memories(instance_no, view=view)

    drafts: list[MemoryDraft] = []
    try:
        drafts = prepared[1] if prepared is not None else llm.extract_memories(
            build_transcript(pending, settings.persona.name),
            name=settings.persona.name,
            today=now_iso[:10],
            # 群聊和私聊要用不同的措辞规则，否则群里的话会被写成"搭档的事"
            scene="group" if scope == "group" else ("person" if scope == "person" else "c2c"),
            known=known_block(existing, settings.memory.known_hint_limit),
        )
    except Exception as exc:  # 网络/解析失败都不能丢记忆
        drafts = _fallback_drafts(pending, settings)
        store.add_diary(
            now_iso,
            f"记忆整理走了兜底逻辑：{type(exc).__name__}: {exc}",
            kind="terminal",
            title="整理失败",
        )

    added = 0
    merged = 0
    skipped = 0
    index_in_batch = 0
    # 回调（关系加分 / 改称呼）会自己写 state 并 commit —— 所以**必须等事务提交后**
    # 再触发，否则它们会把还没写完的整理事务提前提交掉，回滚就不再完整。
    callbacks: list = []
    # ── 一个显式事务：写记忆 + 标 consolidated + 推两个游标，要么全成，要么全不成 ──
    # 以前每一步各自 commit，中途失败会留下"记忆已落库、消息没标已整理、
    # 游标没推进"的撕裂状态（P1-7）。失败时整批回滚 → 消息仍是未整理，
    # 下一轮可重试，且不会产生重复记忆（相似度去重仍在）。
    store.begin()
    try:
        # 先筛掉"空摘要 / 重要性太低"的草稿，让序号在**真正写入的条目**上连续，
        # 这样 _spread_ts 的插值是均匀的
        accepted = [
            d for d in drafts
            if d.summary.strip() and d.importance >= settings.memory.min_importance
        ]
        from .conversation import LIMITS, is_performance
        for index_in_batch, d in enumerate(accepted, start=1):
            evidence = [m for m in pending if int(m["id"]) in d.source_ids]
            if conversation is not None and d.kind in LIMITS:
                conversation.update(d, pending, datetime.fromisoformat(now_iso), commit=False)
            if d.kind in {"topic", "open_loop", "interaction_feedback", "self_interest"}:
                continue
            if d.kind == "assistant_statement" or is_performance(d.summary):
                continue
            if d.kind == "preference" and not any(m["role"] == "user" for m in evidence):
                continue
            layer = d.layer if d.layer in ("core", "important", "normal") else infer_layer(
                d.importance, d.emotion
            )

            # 去重：这件事她已经记过了吗？
            dup = None
            for old in existing:
                if similarity(d.summary, str(old.get("summary", ""))) >= settings.memory.dedupe_ratio:
                    dup = old
                    break
            if dup is not None:
                old_summary = str(dup.get("summary", ""))
                if len(d.summary.strip()) > len(old_summary) + 8:
                    # 新版本更详细 → 让旧那条长大（记忆演进），而不是再记一条
                    store.update_memory(
                        int(dup["id"]),
                        summary=d.summary.strip(),
                        tags=d.tags.strip(),
                        importance=float(d.importance),
                        layer=layer,
                        commit=False,
                    )
                    dup["summary"] = d.summary.strip()
                    merged += 1
                else:
                    skipped += 1
                continue

            # 「这件事发生在什么时候」：以前整批都用 `pending[0]["ts"]`，
            # 同一天 09:00 和 23:00 说的两件事会被记成同一个时间（14 小时偏差，
            # 而提示词里给她看的就是这个时间）。现在按条目序号在**批内时间范围**上铺开：
            # 一条 → 用批首时间；多条 → 从批首到批尾均匀插值。
            happened_ts = _spread_ts(pending, index_in_batch)
            new_id = store.add_memory(
                instance_no=instance_no,
                created_ts=now_iso,
                happened_ts=happened_ts,
                summary=d.summary.strip(),
                tags=d.tags.strip(),
                emotion=d.emotion.strip(),
                importance=float(d.importance),
                source_session=session_id,
                layer=layer,
                strength=initial_strength(d.importance, d.emotion),
                event_date=d.event_date,
                scope=scope,
                subject_id=subject_id,
                speaker_id=speaker_id,
                about=d.about,
                commit=False,
            )
            store.annotate_memory(new_id, d.kind, evidence,
                                  behavior=d.behavior_key, commit=False)
            if d.kind == 'preference' and d.behavior_key:
                store.conn.execute(
                    "UPDATE memory_annotations SET policy='background',reason='用户明确更新了同一偏好' "
                    "WHERE kind='preference' AND behavior_key=? AND memory_id!=? AND memory_id IN "
                    "(SELECT id FROM memories WHERE instance_no=? AND scope=? AND subject_id=?)",
                    (d.behavior_key, new_id, instance_no, scope, subject_id))
            added += 1
            # 同一批里后面的条目也要跟刚写进去的做去重
            existing = [*existing, {"id": new_id, "summary": d.summary.strip()}]
            if on_draft is not None:
                callbacks.append((on_draft, d))

        ids = [m["id"] for m in pending]
        store.mark_consolidated(ids, commit=False)
        store.meta_set(f"last_consolidated:{session_id}", ids[-1], commit=False)
        store.meta_set(
            "last_consolidated_msg_id",
            max(ids[-1], store.meta_get_int("last_consolidated_msg_id", 0)),
            commit=False,
        )
    except BaseException:
        store.rollback()
        raise
    store.commit()

    for cb, d in callbacks:
        try:
            cb(d)
        except Exception:
            pass  # 回调失败不该影响记忆落库

    if stats is not None:
        stats.update(added=added, merged=merged, skipped=skipped)
    return added


def _layer_rank(mem: dict) -> int:
    return {"core": 0, "important": 1, "normal": 2}.get(str(mem.get("layer")), 3)


def reconcile(
    store: Store,
    llm: BaseLLM,
    settings: Settings,
    *,
    instance_no: int,
    scope: str = "private",
    subject_id: str = "",
    limit: int = 40,
    dry_run: bool = False,
) -> dict:
    """把"换个说法记了两遍"的记忆合并掉。返回 {"merged": n, "groups": k, "kept": [...]}。

    为什么必须让模型来做：实测真实的重复记忆
    （「他让我叫他椎名」vs「他自我介绍叫椎名」）词面相似度只有 0.26~0.54，
    而**不同的事**也有 0.16~0.34 —— 两者挤在一起，程序化阈值切不开。
    模型认得这种同义改写，程序只按它的指示合并。

    合并是**可回溯**的：重复的那些只是被标记 merged_into，数据还在。
    dry_run=True 只报告、不落库。
    """
    view = [("private", "")] if scope == "private" else [(scope, subject_id)]
    memories = store.active_memories(instance_no, view=view)
    if len(memories) < settings.memory.reconcile_min_memories:
        return {"merged": 0, "groups": 0, "kept": []}
    picked = memories[-limit:]
    numbered = "\n".join(f"{i + 1}. {str(m.get('summary', '')).strip()}"
                         for i, m in enumerate(picked))

    # ── 候选来源 A：程序按词面相似度挑对（便宜、确定、不会漏掉明显的重复）──
    # 只当"候选"，合不合由模型逐对确认。
    # 实测：真实的重复对 0.43~0.54，不同的事 0.16~0.34 —— 阈值取 0.35 卡在中间，
    # 宁可多问几次，也不要漏掉"他让我叫他椎名"这种改写。
    pairs: list[tuple[float, int, int]] = []
    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            sim = similarity(
                str(picked[i].get("summary", "")), str(picked[j].get("summary", ""))
            )
            if sim >= settings.memory.dedupe_candidate_ratio:
                pairs.append((sim, i, j))
    pairs.sort(reverse=True)

    groups: list[list[int]] = [[i + 1, j + 1] for _s, i, j in pairs[:8]]

    # ── 候选来源 B：让模型通读一遍（能发现词面不像、但确实是同一件事的）──
    try:
        groups.extend(llm.find_duplicate_groups(numbered))
    except Exception:
        pass

    merged = 0
    kept: list[str] = []
    detail: list[dict] = []
    # 先把候选对逐对确认，再按"连通分量"合并。
    # 否则会出现 A~B、B~C 各自成对的情况，B 被合并进两处、A 被当成两个不同的保留者。
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    checked: set[tuple[int, ...]] = set()
    confirmed_pairs = 0
    for group in groups:
        idxs = sorted({i for i in group if 1 <= i <= len(picked)})
        for a_pos in range(len(idxs)):
            for b_pos in range(a_pos + 1, len(idxs)):
                ia, ib = idxs[a_pos], idxs[b_pos]
                ma, mb = picked[ia - 1], picked[ib - 1]
                key = (int(ma["id"]), int(mb["id"]))
                if key in checked:
                    continue
                checked.add(key)
                # 程序闸门：说"不同人的事"的绝不合并（不花调用）
                aa = str(ma.get("about") or "").strip()
                ab = str(mb.get("about") or "").strip()
                if aa and ab and aa != ab:
                    continue
                try:
                    ok = llm.confirm_duplicate(
                        f"- {str(ma.get('summary', '')).strip()}\n"
                        f"- {str(mb.get('summary', '')).strip()}"
                    )
                except Exception:
                    ok = False
                if ok:
                    union(ia, ib)
                    confirmed_pairs += 1

    components: dict[int, list[int]] = {}
    for i in range(1, len(picked) + 1):
        if i in parent:
            components.setdefault(find(i), []).append(i)

    for idxs in components.values():
        items = [picked[i - 1] for i in idxs]
        if len(items) < 2:
            continue
        # 留"最该留的那条"：内核层优先，其次最详细的（详版包含略版的信息）
        items.sort(key=lambda m: (_layer_rank(m), -len(str(m.get("summary", "")))))
        keeper, rest = items[0], items[1:]
        detail.append({
            "keep": str(keeper.get("summary", "")),
            "drop": [str(m.get("summary", "")) for m in rest],
        })
        if dry_run:
            merged += len(rest)
            kept.append(str(keeper.get("summary", "")))
            continue
        tags = sorted({
            t.strip() for m in items for t in str(m.get("tags", "")).split(",") if t.strip()
        })
        store.update_memory(
            int(keeper["id"]),
            tags=",".join(tags)[:120],
            importance=max(float(m.get("importance") or 0.5) for m in items),
            about=str(keeper.get("about") or "") or str(rest[0].get("about") or ""),
        )
        store.mark_merged([int(m["id"]) for m in rest], into=int(keeper["id"]))
        merged += len(rest)
        kept.append(str(keeper.get("summary", "")))
    return {"merged": merged, "groups": len(groups), "kept": kept, "detail": detail}


def flush_sessions(
    store: Store,
    llm: BaseLLM,
    settings: Settings,
    *,
    now_iso: str,
    exclude_session: str = "",
    stale_minutes: float = 0.0,
    min_pending: int = 1,
) -> int:
    """把"还卡着没整理"的会话补整理掉。返回新增条数。

    平时的整理是增量的、按会话攒够 N 条才做；于是聊到一半停了、
    或者跨过零点的会话，剩下的几条会永远卡住 —— 真机数据里
    "认识第一天"那段对话就是这样没变成记忆的。这个函数负责收尾。

    exclude_session：当前会话不补（它有自己的阈值节奏）
    stale_minutes：只补"最后一条消息已经过了这么久"的会话（避免把正聊着的截断）
    """
    instance_no = store.meta_get_int("instance_no", 1)
    total = 0
    for row in store.sessions_with_pending(instance_no):
        session_id = str(row["session_id"])
        if exclude_session and session_id == exclude_session:
            continue
        if int(row["n"]) < min_pending:
            continue
        if stale_minutes > 0:
            try:
                newest = datetime.fromisoformat(str(row["newest"]))
                if (datetime.fromisoformat(now_iso) - newest).total_seconds() / 60.0 < stale_minutes:
                    continue
            except (TypeError, ValueError):
                pass
        total += consolidate(
            store,
            llm,
            settings,
            session_id=session_id,
            now_iso=now_iso,
            force=True,
            scope=str(row["scope"] or "private"),
            subject_id=str(row["subject_id"] or ""),
        )
    return total


# 兜底整理时**不该逐字存下来**的东西：凭据类。模型正常工作时会自己判断"值不值得记"，
# 但模型挂掉时这条兜底路径会把搭档的原话照抄进长期记忆，然后一路进检索与提示词
# （审查 P3-2 实测：「我的银行卡密码是 8899，别告诉别人」被原样写成记忆）。
_SENSITIVE_RE = re.compile(
    r"(密码|口令|验证码|身份证|银行卡|信用卡|CVV|CVC|社保|护照号|密钥|api[_ ]?key|token)",
    re.IGNORECASE,
)


def _spread_ts(pending: list[dict], index: int) -> str:
    """把"这件事发生的时间"按条目序号在批内时间范围上铺开。

    一条 → 批首时间；多条 → 从批首到批尾线性插值（保持 ISO 字符串可比、可用）。
    以前整批共用 `pending[0]["ts"]`，同一天 09:00 与 23:00 的两件事会被记成同一时刻。
    """
    if not pending:
        return ""
    if len(pending) == 1 or index <= 1:
        return str(pending[0].get("ts") or "")
    from datetime import datetime as _dt

    try:
        t0 = _dt.fromisoformat(str(pending[0].get("ts")))
        t1 = _dt.fromisoformat(str(pending[-1].get("ts")))
    except (TypeError, ValueError):
        return str(pending[0].get("ts") or "")
    if t1 <= t0:
        return str(pending[0].get("ts") or "")
    step = (t1 - t0) / max(1, len(pending) - 1)
    # index 从 1 开始（第一条 = 批首）
    pos = min(max(0, index - 1), len(pending) - 1)
    return (t0 + step * pos).isoformat(timespec="seconds")


def _fallback_drafts(messages: list[dict], settings: Settings) -> list[MemoryDraft]:
    """模型不可用时的兜底：直接从搭档的话里挑两句留下来。

    **凭据类内容不逐字存**（审查 P3-2）：模型正常时它会自己判断"值不值得记"，
    但这条兜底路径是照抄原话的，所以先把明显是密码/验证码/证件号那种句子换成
    "他说了一件跟安全/凭据有关的事，具体内容我没存"——既留住"发生过什么"，
    又不把秘密写进会被检索、会被注入提示词的长期记忆。
    """
    user_lines = [m["content"].strip() for m in messages if m["role"] == "user"]
    user_lines = [t for t in user_lines if len(t) >= 6]
    drafts: list[MemoryDraft] = []
    for text in user_lines[-2:]:
        if _SENSITIVE_RE.search(text):
            drafts.append(
                MemoryDraft(
                    summary="搭档说了一件跟密码/证件有关的事（原话我没有逐字记下来）。",
                    tags="搭档,未整理",
                    emotion="未整理",
                    importance=0.35,
                    layer="normal",
                )
            )
            continue
        summary = text if len(text) <= 60 else text[:57] + "……"
        drafts.append(
            MemoryDraft(
                summary=f"搭档说过：{summary}",
                tags="搭档,原话",
                emotion="未整理",
                importance=0.35,
                layer="normal",
            )
        )
    return drafts
