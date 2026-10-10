#!/usr/bin/env python
"""Stream B 探针 2：trigger.py 纯函数边界 + "没命中零 token" 宣称核验。

    python .review\\probes\\bc_trigger.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.trigger import decide, speak_probability, tier_of  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


class Count(MockLLM):
    """数所有会真的打到 API 的入口。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.chat_calls = 0
        self.tool_calls = 0
        self.extract_calls = 0
        self.name_calls = 0
        self.rewrite_calls = 0

    def chat(self, messages, **kw):
        self.chat_calls += 1
        return super().chat(messages, **kw)

    def chat_with_tools(self, messages, *, tools=None, **kw):
        self.tool_calls += 1
        return super().chat_with_tools(messages, tools=tools, **kw)

    def extract_memories(self, transcript, **kw):
        self.extract_calls += 1
        return super().extract_memories(transcript, **kw)

    def extract_self_name(self, text):
        self.name_calls += 1
        return super().extract_self_name(text)

    def rewrite_query(self, text):
        self.rewrite_calls += 1
        return super().rewrite_query(text)

    @property
    def total(self) -> int:
        return self.chat_calls + self.tool_calls + self.extract_calls + self.name_calls + self.rewrite_calls


def env(name: str, hours: float = 81920.0, **mem_over):
    tag = f"{os.getpid()}-{int(time.time())}"
    d = ROOT / ".review" / "tmp" / f"brain-checker-{name}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    base = load_settings(lifespan_override=hours, force_mock=True)
    mem = dataclasses.replace(base.memory, **mem_over) if mem_over else base.memory
    return dataclasses.replace(base, root=d, memory=mem)


# ══ 1. 档位与概率的边界值 ══════════════════════════════════════
print("== 1. tier_of 边界（README 表：0~19=1 / 20~44=2 / 45~69=3 / 70~100=4）==")
expect = {0: 1, 19: 1, 20: 2, 44: 2, 45: 3, 69: 3, 70: 4, 100: 4}
for pos, want in expect.items():
    got = tier_of(pos)
    check(f"tier_of({pos}) == {want}", got == want, f"got {got}")
check("越界被夹住（-5 → 1，150 → 4）", (tier_of(-5), tier_of(150)) == (1, 4),
      f"{tier_of(-5)} {tier_of(150)}")

print("\n== 2. speak_probability = (pos-20)/70 ==")
for pos, want in ((20, 0.0), (45, 25 / 70), (69, 49 / 70), (70, 50 / 70), (100, 80 / 70)):
    got = speak_probability(pos)
    check(f"speak_probability({pos}) ≈ {min(want, 1.0):.4f}", abs(got - min(want, 1.0)) < 1e-9,
          f"got {got}")
print(f"      实测：45 → {speak_probability(45):.4f}（文档写 36%）、70 → {speak_probability(70):.4f}（文档写 71%）")
check("概率在 0~1 内（100 档夹到 1.0）", speak_probability(100) == 1.0, str(speak_probability(100)))

print("\n== 3. decide() 决策 ==")
check("tier1 被 @ → 开口", decide(0, at_bot=True, text="在吗")[0] is True)
check("tier1 没被 @ → 不开口", decide(0, at_bot=False, text="在吗")[0] is False)
check("tier2 没关键词 → 不开口", decide(30, at_bot=False, text="今天天气不错")[0] is False)
check("tier2 命中关键词 → 开口", decide(30, at_bot=False, text="她今天心情不好", keywords=("心情",))[0] is True)
check("tier3 roll=0.35 < p(45)=0.357 → 开口", decide(45, at_bot=False, text="嗯", roll=0.35)[0] is True)
check("tier3 roll=0.36 > p(45) → 不开口", decide(45, at_bot=False, text="嗯", roll=0.36)[0] is False)
check("tier3 roll 恰好等于 p → 不开口（严格小于）",
      decide(45, at_bot=False, text="嗯", roll=speak_probability(45))[0] is False)
check("tier4 任何消息都开口（roll 被忽略）",
      decide(70, at_bot=False, text="随便一句", roll=1.0)[0] is True
      and decide(100, at_bot=False, text="随便一句", roll=0.999999)[0] is True)
check("关键词表里的空串不会命中一切",
      decide(30, at_bot=False, text="随便一句话", keywords=("",))[0] is False)
print(f"      tier4 下 speak_probability 不参与决策 → 文档里的 '70 → 71%' 实际无意义")

# ══ 4. "没命中零 token" ═══════════════════════════════════════
print("\n== 4. 旁听（log_only）到底花不花 token ==")
h = Giftia(env("zero"), llm=Count(env("zero").model))
h.set_handler_id("OWNER")
ck: Count = h.llm  # type: ignore[assignment]

# 4a. 普通闲聊
h.handle("今天群里好热闹啊", user_id="STRANGER", scene="group", group_id="GZ",
         speaker_name="阿伟", log_only=True, mid="z1")
check("4a 普通旁听一句：零模型调用", ck.total == 0, f"total={ck.total}")
print(f"      计数：chat={ck.chat_calls} tools={ck.tool_calls} extract={ck.extract_calls} "
      f"name={ck.name_calls} rewrite={ck.rewrite_calls}")

# 4b. 短句里带自我介绍标志词 → learn_name 会调模型（在 log_only 判断之前！）
before = ck.name_calls
h.handle("我是来看热闹的", user_id="STRANGER", scene="group", group_id="GZ",
         speaker_name="阿伟", log_only=True, mid="z2")
check("4b ★ 旁听一句「我是来看热闹的」也调了模型（名字判定）", ck.name_calls > before,
      f"name_calls {before} -> {ck.name_calls}")
print(f"      详情：chat={ck.chat_calls} extract={ck.extract_calls} name={ck.name_calls}")

# 4c. 攒够 consolidate_every(=8) 条旁听 → consolidate 调模型
h2 = Giftia(env("zero2"), llm=Count(env("zero2").model))
h2.set_handler_id("OWNER")
ck2: Count = h2.llm  # type: ignore[assignment]
curve = []
for i in range(10):
    h2.handle(f"群里闲聊第{i}句，聊的是周末怎么过", user_id="STRANGER", scene="group",
              group_id="G2", speaker_name="阿伟", log_only=True, mid=f"y{i}")
    curve.append(ck2.extract_calls)
check("4c ★ 旁听到第 8 条触发一次记忆整理（extract_memories）", curve[7] > curve[0],
      f"extract_calls 曲线={curve}")
print(f"      每收一条旁听后的 extract_calls：{curve}")
print(f"      （配置 consolidate_every={h2.settings.memory.consolidate_every}）")

# ══ 5. 日上限（适配器侧，离线 dry-run） ═══════════════════════
print("\n== 5. speak_max_per_day（自己插嘴的日上限）==")
from giftia import brain as brain_mod  # noqa: E402
from adapters.qq_official import QQBot  # noqa: E402
import random  # noqa: E402

st = env("speakcap")
st = dataclasses.replace(st, runtime=dataclasses.replace(st.runtime, speak_max_per_day=3,
                                                        response_tier=100, wake_delay_seconds=0.0))
brain_mod.reset_default()
hub = brain_mod.get_default(st)
hub.set_handler_id("OWNER")
hub.llm = Count(st.model)
bot = QQBot("appid", "secret", dry_run=True, settings=st, owner_openid="OWNER")
bot.llm = hub.llm

payload = lambda i: {
    "id": f"msg{i}", "content": f"群里随便说第{i}句，天气不错",
    "group_openid": "GS", "author": {"user_openid": "STRANGER", "username": "阿伟"},
    "mentions": [],
}
results = []
for i in range(4):
    before_total = hub.llm.total  # type: ignore[union-attr]
    bot.handle_event("GROUP_MESSAGE_CREATE", payload(i))
    results.append((i + 1, hub.llm.total - before_total))
print(f"      每条第 1~4 条的模型调用增量：{results}")
check("5a 前 3 条没被 @ 也会开口（tier=100）", all(d > 0 for _i, d in results[:3]), str(results))
check("5b ★ 第 4 条被日上限拦下（不再调模型）", results[3][1] == 0, str(results[3]))
used = hub.store.state_get(f"speak_up:GS:{hub.clock.now().date().isoformat()}", 0)
check("5c 日上限计数=3", int(used or 0) == 3, str(used))

# @ 她不受日上限限制
before_total = hub.llm.total  # type: ignore[union-attr]
at = payload(9)
at["id"] = "msgat"
at["mentions"] = [{"bot": True}]
bot.handle_event("GROUP_AT_MESSAGE_CREATE", at)
check("5d 被 @ 时即使额度用完也回（@ 不受 speak_max_per_day 限制）",
      hub.llm.total - before_total > 0, "no call")

print(f"\n结果：PASS={PASS} FAIL={FAIL}")
brain_mod.reset_default()
sys.exit(1 if FAIL else 0)
