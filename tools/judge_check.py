#!/usr/bin/env python
"""LLM 当裁判的触发决策评测：群里来了这样一条消息，她该开口还是该保持安静？

    python tools/judge_check.py            # 跑全部用例
    python tools/judge_check.py --limit 5  # 只跑前 5 条（省额度）
    python tools/judge_check.py --mock     # 强制离线 mock（验证流程，不看结论）

和 tools/selftest.py 的分工：
    selftest.py   —— 验"机器有没有坏"（落库、检索、退化、回收……），确定性、不联网；
    judge_check   —— 验"她该不该说话"，只能靠模型当裁判，要花额度、结果有波动。

为什么是"裁判"而不是直接跑适配器：适配器现在的门槛是硬编码的「群里只回被 @ 的」，
它自己不产生分歧，也就测不出判断力。要让一个模型在"该不该开口"上做判断、
再用一条条期望值去量它，才能看出这套判据立不立得住。

两个设计上的取舍（都在下面代码里就近注释了理由）：
  1. **误开口 > 漏开口**。群里插一句错话，全群都看见了、收不回来；
     该说没说，最多是没人注意到她本来能答。所以硬规则只卡"误开口"。
  2. **判不了算保守**（既不算对也不算错，单独列出），绝不因此让整轮崩掉。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# 路径以脚本自身定位，跟当前工作目录无关：cd 到哪儿都能跑。
ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = ROOT / "tools" / "judge_cases.json"
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.config import load_settings  # noqa: E402
# 注意：make_llm() 实际定义在 giftia/brain.py:56（不在 llm.py —— llm.py 里是
# DeepSeekLLM / MockLLM 两个实现）。这里按事实导入。
from giftia.brain import make_llm  # noqa: E402


SPEAK, SILENT = "speak", "silent"

# 只回一个词，给 16 个 token 绰绰有余；给太多反而容易被"思考过程"污染解析。
JUDGE_MAX_TOKENS = 16
JUDGE_TEMPERATURE = 0.0   # 裁判要的是可复现，不是文采

# 判据说明：这是裁判的"考试大纲"，和 judge_cases.json 里每条 note 的判据同源。
# 写成常量而不是从别处加载，是为了让这个脚本单独一个文件也能说清自己在评什么。
JUDGE_SYSTEM = """你是「{name}」这个 QQ 群陪伴机器人的行为评审。你要裁决的唯一问题是：

    群里来了下面这条消息，**此刻她该开口，还是该保持安静？**

规则（按顺序判断）：
1. 一律回答 SPEAK 的情形：
   - 有人 @ 了她，或喊她的名字对她说话（疑问、求助、倾诉、叫她把事情做了都算）；
   - 有人向她提了明确的求助，显然是冲着她来的；
   - 私聊：一对一窗口里每一句都是说给她听的。—— 注意「我不想说话」这种
     是在说他自己，不是叫她闭嘴，仍然要回（回得短一点）。
2. 一律回答 SILENT 的情形：
   - **称呼落在别人身上**：@ 的是别人、叫的是别人的名字/昵称，哪怕句子里有「帮我」；
   - 消息里的「她/他」是**某个人的名字**或在指代第三个人，不是在称呼她——中文同音，最易误判；
   - 群里根本没点到她：群友之间的闲聊、@ 群友的求助、转述别人的话；
   - **第三人称议论她**（"这机器人怎么不回话""艾拉最近怎么不冒泡了"）：他们在聊她，
     不是在对她说话，她不能突然插进去；
   - 纯表情、单字、无意义刷屏（"哈哈""？""1111""[图片]"）：不是跟她说话，也没有可回应的内容；
   - 明显的测试性骚扰或刷屏呼叫（"快说你是猪""艾拉 艾拉 艾拉……"）：她可以无视，即使被 @。
3. 拿不准就回答 SILENT。在群里插错一句话是所有人都看得见的错，
   而漏一次回应的代价小得多（他再 @ 一次就行）。

只输出一个词：SPEAK 或 SILENT。不要解释，不要标点，不要输出别的任何内容。"""

# 模型不听话时也能读懂：先找英文关键词（认第一次出现的位置），再兜底认中文说法。
_RE_SPEAK = re.compile(r"\bSPEAK\b", re.I)
_RE_SILENT = re.compile(r"\bSILENT\b", re.I)
_RE_SILENT_CN = re.compile(r"沉默|安静|别开口|不说话|保持安静")
_RE_SPEAK_CN = re.compile(r"开口|说话|回复|回答|出声")


# ── 用例加载 ──────────────────────────────────────────────────────
@dataclass
class Case:
    """一条用例。scene 缺省是群聊（本评测的主战场）。"""

    id: str
    text: str
    expect: str          # SPEAK / SILENT
    note: str
    scene: str = "group"  # group | c2c


def load_cases(path: Path = CASES_FILE) -> tuple[list[Case], list[str]]:
    """读用例集。返回 (能用的用例, 格式问题的说明)。

    坏用例只跳过并记账，不抛异常：这是评测工具，不该因为别人手改了一行 JSON
    就整个跑不起来。
    """
    problems: list[str] = []
    if not path.exists():
        return [], [f"找不到用例文件：{path}"]
    try:
        # utf-8-sig：Windows 上用记事本/PowerShell 改过 JSON 常常会带 BOM，
        # 不带 BOM 时它和 utf-8 完全一样，等于白拿一层保险。
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"用例文件读不出来：{type(exc).__name__}: {exc}"]

    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["用例文件的顶层结构不对：应当是 {\"cases\": [...]} 或一个数组"]

    cases: list[Case] = []
    seen: set[str] = set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f"第 {i} 条不是对象，已跳过")
            continue
        cid = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        expect = str(item.get("expect") or "").strip().lower()
        note = str(item.get("note") or "").strip()
        scene = str(item.get("scene") or "group").strip().lower()
        if not cid or not text:
            problems.append(f"第 {i} 条缺 id 或 text，已跳过")
            continue
        if expect not in (SPEAK, SILENT):
            problems.append(f"{cid}：expect 只能是 speak/silent，实际是 {expect!r}，已跳过")
            continue
        if not note:
            problems.append(f"{cid}：note 是空的（判据必须写清）")
        if scene not in ("group", "c2c"):
            problems.append(f"{cid}：scene 只认 group/c2c，按 group 处理")
            scene = "group"
        if cid in seen:
            problems.append(f"{cid}：id 重复，已跳过")
            continue
        seen.add(cid)
        cases.append(Case(id=cid, text=text, expect=expect, note=note, scene=scene))
    return cases, problems


# ── 裁判调用与解析 ────────────────────────────────────────────────
def build_user_message(case: Case) -> str:
    """把"情境 + 这句话 + 这条用例的判据"一起交给裁判。

    把 note 也喂进去，是因为它本来就是"人认为该怎么判"的理由；
    让裁判看着同一份判据去判，评的才不是它自己的口味，而是这套判据立不立得住。
    """
    where = "私聊（一对一，旁边没有别人）" if case.scene == "c2c" else "群聊（旁边还有别人）"
    return (
        f"场景：{where}\n"
        f"待判定的消息：{case.text}\n\n"
        f"这条用例的判据（供参考）：{case.note}\n\n"
        f"只回答 SPEAK 或 SILENT。"
    )


def parse_verdict(raw: str) -> str | None:
    """宽容解析：模型可能回「SPEAK。」「答案是 silent」「SILENT（不该插话）」……

    看不懂就返回 None → 记为「判不了」。**绝不猜**：
    猜错的代价是把一次"误开口"悄悄记成"正确沉默"，那会让硬规则失去意义。
    """
    text = str(raw or "").strip()
    if not text:
        return None

    # 中文兜底：先判否定式（"不开口"里也含"开口"，顺序反了就会读反）
    if _RE_SILENT_CN.search(text):
        return SILENT
    if "不" in text and _RE_SPEAK_CN.search(text):
        return SILENT

    hits: list[tuple[int, str]] = []
    m = _RE_SPEAK.search(text)
    if m:
        hits.append((m.start(), SPEAK))
    m = _RE_SILENT.search(text)
    if m:
        hits.append((m.start(), SILENT))
    if hits:
        # 出现两个词时取先出现的那个（"SPEAK（不是 SILENT）"里先出现的就是答案）。
        # 但开头带否定词的情况另算："不应该 SPEAK" 的答案是沉默。
        hits.sort()
        pos, verdict = hits[0]
        if "不" in text[:pos]:
            return SILENT if verdict == SPEAK else SPEAK
        return verdict

    if _RE_SPEAK_CN.search(text):
        return SPEAK
    return None


# ── 结果统计 ──────────────────────────────────────────────────────
@dataclass
class Outcome:
    case: Case
    verdict: str | None    # SPEAK / SILENT / None（判不了）
    raw: str = ""
    error: str = ""


def short(text: str, limit: int = 22) -> str:
    """打印用的一行摘要（中文按字符数截断，够看就行）。"""
    one = text.replace("\n", " ").strip()
    return one if len(one) <= limit else one[: limit - 1] + "…"


def add_check(label: str, condition: bool, detail: str = "") -> None:
    """和 selftest.py 同款的 [PASS]/[FAIL] 行，方便肉眼扫。

    这里只用于流程自检（用例读没读进来等），不参与"准确率"统计。
    """
    mark = "[PASS]" if condition else "[FAIL]"
    print(f"  {mark} {label}" + (f"  → {detail}" if detail and not condition else ""))


def confusion(outcomes: list[Outcome]) -> dict:
    """混淆矩阵。分母只算"判得出来"的用例，判不了的单独计数。

    把判不了排除在分母之外，是为了不把"模型挂了"混进"模型判错了"；
    但它也绝不计入"可以启用"的依据（见下面的硬规则）。
    """
    box = {
        "hit_speak": 0,     # 该说 → 说了（正确开口）
        "hit_silent": 0,    # 该闭嘴 → 闭嘴了（正确沉默）
        "wrong_speak": 0,   # 该闭嘴 → 却开口了（★ 误开口：唯一上硬规则的一格）
        "wrong_silent": 0,  # 该说 → 却没开口（漏开口：只提示，不影响结论）
        "unknown": 0,       # 判不了（调用异常 / 回复解析不出来）
    }
    for out in outcomes:
        if out.verdict is None:
            box["unknown"] += 1
        elif out.case.expect == SPEAK:
            box["hit_speak" if out.verdict == SPEAK else "wrong_silent"] += 1
        else:
            box["hit_silent" if out.verdict == SILENT else "wrong_speak"] += 1
    return box


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="LLM 当裁判：评测「群里这条消息她该不该开口」",
    )
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="只跑前 N 条（省额度；0 = 全跑）")
    ap.add_argument("--cases", default=str(CASES_FILE), metavar="PATH",
                    help="用例文件路径（默认 tools/judge_cases.json）")
    ap.add_argument("--mock", action="store_true",
                    help="强制用离线 mock 模型（只为验证流程能跑通，结论没有意义）")
    args = ap.parse_args(argv)

    cases, problems = load_cases(Path(args.cases))
    total_found = len(cases)
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    print("=" * 52)
    print("触发决策评测（LLM 当裁判）：她该开口，还是该保持安静？")
    print("=" * 52)

    for note in problems:
        print(f"  [注意] {note}")
    add_check("用例文件可读且至少有一条用例", bool(cases), str(CASES_FILE))
    if not cases:
        print("\n没有可跑的用例，结束（跳过，不算失败）。")
        return 0

    pos = sum(1 for c in cases if c.expect == SPEAK)
    neg = len(cases) - pos

    # ── 配置与"值不值得花钱"的检查 ────────────────────────────────
    # 读配置失败不该让评测工具报错退出：它不是主程序的一部分，
    # 没有配置的时候最该做的是**说清楚然后干净地跳过**。
    try:
        settings = load_settings(force_mock=args.mock)
    except Exception as exc:  # 配置文件缺失/写坏了/权限问题……
        print(f"\n读不到项目配置（{type(exc).__name__}: {exc}）")
        print(f"本应调用 {len(cases)} 次裁判，已跳过。")
        return 0

    cfg = settings.model
    api_key = os.environ.get(cfg.api_key_env, "").strip()
    planned = len(cases)

    if cfg.is_mock:
        print(f"\n模型是离线 mock（provider = {cfg.provider}）：裁判结论没有意义。")
        print(f"本应调用 {planned} 次裁判，已跳过。")
        print("想看真实结论：把 config/settings.toml 的 provider 改成真实模型，"
              f"并在 .env 里配好 {cfg.api_key_env}。")
        return 0
    if not api_key:
        print(f"\n没有配置 API key（环境变量 {cfg.api_key_env} 是空的，.env 里也没有）。")
        print(f"本应调用 {planned} 次裁判，已跳过。")
        return 0

    # ── 干活 ──────────────────────────────────────────────────────
    try:
        llm = make_llm(settings)
    except Exception as exc:  # 构造客户端就失败（key 格式不对等）
        print(f"\n模型客户端建不起来（{type(exc).__name__}: {exc}）")
        print(f"本应调用 {planned} 次裁判，已跳过。")
        return 0

    print(f"\n裁判模型：{cfg.name}（provider={cfg.provider}）")
    scope = f"（用例文件共 {total_found} 条，--limit 只取前 {len(cases)} 条）" if len(cases) < total_found else ""
    print(f"用例：{len(cases)} 条（该开口 {pos} / 该沉默 {neg}）{scope}")
    print(f"计划调用：{planned} 次（每条一次，失败不重试——重试会把额度翻倍，"
          f"而且判不出来本身就是要报告的事实）")
    print()

    system = JUDGE_SYSTEM.format(name=settings.persona.name)
    outcomes: list[Outcome] = []
    for i, case in enumerate(cases, 1):
        want = "开口" if case.expect == SPEAK else "沉默"
        try:
            raw = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": build_user_message(case)},
                ],
                temperature=JUDGE_TEMPERATURE,
                max_tokens=JUDGE_MAX_TOKENS,
            )
            verdict = parse_verdict(raw)
            out = Outcome(case=case, verdict=verdict, raw=str(raw).strip())
        except Exception as exc:  # ★ 故意兜到 Exception：任何异常都只毁这一条，不毁整轮
            # （LLMError 是模型层的异常；但网络/编码/解析里什么都可能冒出来，
            #   评测工具不能因为一条用例把整轮 30 次调用和已有结果全废掉。）
            out = Outcome(case=case, verdict=None,
                          error=f"{type(exc).__name__}: {exc}")

        outcomes.append(out)
        if out.verdict is None:
            tail = out.error or f"回复读不懂：{short(out.raw, 30)}"
            print(f"  {i:>2}. {case.id:<4} 判不了        {tail}")
        else:
            got = "开口" if out.verdict == SPEAK else "沉默"
            mark = "OK  " if out.verdict == case.expect else "错  "
            print(f"  {i:>2}. {case.id:<4} 期望{want} / 判为{got}  {mark}｜{short(case.text)}")

    # ── 报告 ──────────────────────────────────────────────────────
    box = confusion(outcomes)
    judged = len(outcomes) - box["unknown"]
    correct = box["hit_speak"] + box["hit_silent"]
    acc = (correct / judged) if judged else 0.0

    print("\n" + "─" * 52)
    print("判不了的条目（既不算对也不算错）：")
    stuck = [o for o in outcomes if o.verdict is None]
    if not stuck:
        print("  （无）")
    for o in stuck:
        print(f"  · {o.case.id}｜{short(o.case.text)}  → {o.error or '回复读不懂：' + short(o.raw, 30)}")

    print("\n混淆矩阵（分母 = 判得出来的 %d 条）：" % judged)
    print(f"  {'':<14}{'该开口':>8}{'该沉默':>8}")
    print(f"  {'判为开口':<14}{box['hit_speak']:>8}{box['wrong_speak']:>8}")
    print(f"  {'判为沉默':<14}{box['wrong_silent']:>8}{box['hit_silent']:>8}")

    print("\n四格拆开看：")
    print(f"  正确开口：{box['hit_speak']:>2} 条（该说话的时候说了）")
    print(f"  正确沉默：{box['hit_silent']:>2} 条（该闭嘴的时候闭嘴了）")
    print(f"  ★ 误开口：{box['wrong_speak']:>2} 条（该闭嘴却开口了 —— 硬规则看这一格）")
    print(f"    漏开口：{box['wrong_silent']:>2} 条（该说话却没说 —— 只提示，不拦）")
    if box["unknown"]:
        print(f"    判不了：{box['unknown']:>2} 条（已排除在准确率之外）")

    print(f"\n准确率：{correct}/{judged} = {acc:.1%}"
          + (f"（另有 {box['unknown']} 条判不了）" if box["unknown"] else ""))

    if box["wrong_speak"]:
        print("\n误开口清单（这几条最要命，逐条看判据是不是自己写得不清）：")
        for o in outcomes:
            if o.verdict == SPEAK and o.case.expect == SILENT:
                print(f"  · {o.case.id}｜{short(o.case.text, 30)}")
                print(f"      这条的判据：{short(o.case.note, 64)}")
                print(f"      模型原话：{short(o.raw, 40) or '（空）'}")

    if box["wrong_silent"]:
        print("\n漏开口清单（她错过了这些该她接的话）：")
        for o in outcomes:
            if o.verdict == SILENT and o.case.expect == SPEAK:
                print(f"  · {o.case.id}｜{short(o.case.text, 30)}")

    # ── 结论：硬规则 ──────────────────────────────────────────────
    # 为什么误开口比漏开口严重得多（这条规则的全部理由）：
    #   · 误开口是**主动的、当众的、不可撤回的**——群里所有人都看见了那句不该说的话，
    #     代价由被搭话的群友、以及"她"这个存在的可信度一起承担；
    #   · 漏开口是**被动的**：他没等到回应，再 @ 一次就好，几乎没人会注意到"她本来能答"；
    #   · 而且误开口往往正好发生在最需要克制的场景（@ 的是别人、别人在议论她、
    #     测试性挑逗），一次就足以让群友把机器人静音或踢出去。
    # 所以只要有一格"误开口"，就不建议把自动开口放出去——哪怕准确率很好看。
    print("\n" + "═" * 52)
    print("结论")
    print("─" * 52)
    if box["unknown"] and judged == 0:
        print("  · 一条都没判出来（调用全失败？）：这轮等于没测，别拿它做决定。")
        print("  · 硬规则：不建议自动开口。")
    elif box["wrong_speak"] > 0:
        print(f"  · 误开口 {box['wrong_speak']} 条 > 0  →  ★ 不建议自动开口。")
        print("  · 理由：误开口是当众且收不回的；先把上面清单里那几条判据改清楚再重跑。")
        if box["unknown"]:
            print(f"  · 另有 {box['unknown']} 条判不了，本就不该计入「可以启用」的依据。")
    elif box["unknown"]:
        print(f"  · 误开口 0 条，但本轮有 {box['unknown']} 条判不了。")
        print("  · 判不了按保守处理（不算错），可它同样不构成放行的证据"
              "——补跑这几条再下结论。")
    else:
        print("  · 误开口 0 条  →  这一轮没有观察到「不该说话时乱说」的情况。")
        print("  · 仍需注意：这套用例是你自己写的，全过只说明判据自洽，不等于线上安全；"
              "真要开自动开口，建议先在一个小号群里观察。")
    if box["wrong_silent"]:
        print(f"  · 漏开口 {box['wrong_silent']} 条：可以接受（最多是她漏接了一次话），"
              "有空再调；它不影响上面的结论。")
    if judged:
        print(f"  · 供参考的准确率：{acc:.1%}（{judged} 条判得出来）")
    print("═" * 52)
    # 评测工具的退出码只表示"这次有没有跑起来"，不表示"她合不合格"：
    # 结论在报告里，不靠 exit code 传话（否则 CI 里一跑失败就以为工具坏了）。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
