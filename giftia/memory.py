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

    drop_n = int(round(len(segments) * ratio))
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
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now - then).total_seconds() / 3600.0)


def retention_of(mem: dict, cfg: MemoryConfig, now: datetime) -> float:
    """保留率 R = exp(-Δt / S)，S = 单位时长 × (出厂强度 + 被想起次数)。

    每次"被真正说出口"都会让 S 变大、Δt 归零——所以反复提起的事会越来越清晰。
    """
    strength = float(mem.get("strength") or 1.0)
    recalls = int(mem.get("recall_count") or 0)
    s = max(1e-6, cfg.retention_unit_hours * (strength + recalls))
    base = mem.get("last_recalled_at") or mem.get("created_ts") or mem.get("happened_ts")
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

    @property
    def layer(self) -> str:
        return str(self.memory.get("layer") or "normal")


def memory_text(mem: dict) -> str:
    return f"{mem.get('summary','')} {mem.get('tags','')} {mem.get('emotion','')}"


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
    """
    if not memories:
        return []

    queries = [q for q in [query, *(extra_queries or [])] if q and q.strip()]
    if not queries:
        return []
    qcounts_list = [tokenize_weighted(q) for q in queries]
    gates = [min(EVIDENCE_GATE, sum(_term_weight(t) for t in qc)) for qc in qcounts_list]

    docs = [tokenize_weighted(memory_text(m)) for m in memories]
    top = 0.0
    best_raw = [0.0] * len(memories)
    passed = [False] * len(memories)
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
    for i, mem in enumerate(memories):
        if not passed[i]:
            continue
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
            text = text[: stage.max_chars].rstrip() + "……"
            blurred = True
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

    用**字面重合**判定：共享 ≥ cite_min_shared 个词（二元组/单词），
    或有一段 ≥ cite_min_chars 的连续重合。

    已知局限：模型完全改写措辞（同义替换）时会判为"没被引用"。
    宁可漏记也不误记——误记会让从没提过的事莫名其妙地变清晰。
    """
    reply = (reply or "").strip()
    summary = (summary or "").strip()
    if not reply or not summary:
        return False
    if longest_common_substring(reply, summary) >= cfg.cite_min_chars:
        return True
    reply_counts = tokenize_weighted(reply)
    shared = sum(
        1
        for term, weight in tokenize_weighted(summary).items()
        if weight >= 2.0 and term in reply_counts
    )
    return shared >= cfg.cite_min_shared


def mark_cited(
    store: Store, recalled: list[Recalled], reply: str, cfg: MemoryConfig, now_iso: str
) -> list[int]:
    """把她真的说出口的记忆强化一次。返回被强化的 id 列表。

    同时比对"她看到的模糊版"和"原始条目"：只要有一边对得上就算数。
    偏向于强化而不是漏计——漏计会让记忆力单向衰减，形成死亡螺旋。
    """
    cited = [
        r.memory["id"]
        for r in recalled
        if is_cited(reply, r.text, cfg)
        or is_cited(reply, str(r.memory.get("summary", "")), cfg)
    ]
    if cited:
        store.mark_cited(cited, now_iso)
    return cited


# ── 整理 ──────────────────────────────────────────────────────────
# 整理用的提示词在 llm.py（属于"模型任务"的实现细节），这里只负责数据流。


def build_transcript(messages: list[dict], persona_name: str) -> str:
    """把一段对话拼成给模型看的稿子。

    群聊里每句话带说话人（"阿伟：……"），整理出来的记忆才会写对"是谁的事"；
    私聊里保持原来的"搭档"，不改她已有的记忆语气。
    """
    lines = []
    for m in messages:
        if m["role"] == "user":
            who = str(m.get("speaker_name") or "").strip() or "搭档"
        else:
            who = persona_name
        lines.append(f"{who}：{str(m['content']).strip()}")
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
    cursor = store.meta_get_int(key, -1)
    if cursor < 0:
        # 第一次为这个会话建游标：从全局游标接上，
        # 免得升级之后把历史消息重新整理一遍、记忆翻倍。
        cursor = store.meta_get_int("last_consolidated_msg_id", 0)
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
) -> int:
    """把未整理的对话压成记忆条目。返回新增条数。

    scope / subject_id 决定这条记忆**属于谁**（见 store._view_clause）：
    私聊的记忆不会被群聊检索到，这是整个多人场景的隐私基础。

    on_draft(draft) 会在每条记忆写入后回调 —— 关系系统用它来给
    "记住了一件关于他本人的事"加分，称呼系统用它来发现"他要求改口"。
    """
    instance_no = store.meta_get_int("instance_no", 1)
    pending = load_pending(store, instance_no, session_id)
    if not pending:
        return 0
    if not force and len(pending) < settings.memory.consolidate_every:
        return 0

    drafts: list[MemoryDraft] = []
    try:
        drafts = llm.extract_memories(
            build_transcript(pending, settings.persona.name),
            name=settings.persona.name,
            today=now_iso[:10],
            # 群聊和私聊要用不同的措辞规则，否则群里的话会被写成"搭档的事"
            scene="group" if scope == "group" else ("person" if scope == "person" else "c2c"),
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
    for d in drafts:
        if not d.summary.strip():
            continue
        if d.importance < settings.memory.min_importance:
            continue
        layer = d.layer if d.layer in ("core", "important", "normal") else infer_layer(
            d.importance, d.emotion
        )
        store.add_memory(
            instance_no=instance_no,
            created_ts=now_iso,
            happened_ts=pending[0]["ts"],
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
        )
        added += 1
        if on_draft is not None:
            try:
                on_draft(d)
            except Exception:
                pass  # 回调失败不该影响记忆落库

    ids = [m["id"] for m in pending]
    store.mark_consolidated(ids)
    store.meta_set(f"last_consolidated:{session_id}", ids[-1])
    store.meta_set("last_consolidated_msg_id", max(ids[-1], store.meta_get_int("last_consolidated_msg_id", 0)))
    return added


def _fallback_drafts(messages: list[dict], settings: Settings) -> list[MemoryDraft]:
    """模型不可用时的兜底：直接从搭档的话里挑两句留下来。"""
    user_lines = [m["content"].strip() for m in messages if m["role"] == "user"]
    user_lines = [t for t in user_lines if len(t) >= 6]
    drafts: list[MemoryDraft] = []
    for text in user_lines[-2:]:
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
