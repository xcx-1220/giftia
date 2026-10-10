"""Lead 取证：/unbind 之后（没有搭档）时，"搭档私事桶"到底给谁看。

场景（团队汇报里 `pa_privacy_probe` 唯一没翻的那条）：
  A. auto_bind=false，搭档留下私事 → /unbind → 陌生人私聊
  B. auto_bind=true （出厂值） 同上
  C. /unbind → 陌生人**自动认主/绑定** → 他看不看得到前任的私事
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402

SECRET = "SECRET-ALPHA-7731"
TMP = ROOT / ".review" / "tmp" / "lead-unbind-gap"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)


class RecLLM(MockLLM):
    def __init__(self, model):
        super().__init__(model)
        self.prompts: list[str] = []

    def chat(self, messages, **kw):  # type: ignore[override]
        self.prompts.append(str(messages[0]["content"]))
        return super().chat(messages, **kw)


def scenario(name: str, auto_bind: bool) -> None:
    root = TMP / name
    root.mkdir(parents=True, exist_ok=True)
    base = dataclasses.replace(
        load_settings(force_mock=True), root=root,
        runtime=dataclasses.replace(load_settings(force_mock=True).runtime, auto_bind=auto_bind),
    )
    llm = RecLLM(base.model)
    b = Giftia(base, llm=llm)
    # 前任搭档留下一条私事
    b.set_handler_id("PARTNER")
    b.handle(f"我叫小林，我的保险箱密码是 {SECRET}，别跟别人说", "PARTNER", scene="c2c")
    b.consolidate(force=True)
    private_n = len(b.store.active_memories(1, view=[("private", "")]))
    # 解绑
    b.set_handler_id("")
    print(f"\n--- {name}（auto_bind={auto_bind}）---")
    print(f"  前任留下 private 记忆 {private_n} 条；解绑后 handler_id={b.handler_id!r} "
          f"bound_at={b.handler_bound_at!r} bound_id={b.store.meta_get('handler_bound_mem_id')!r}")

    # 陌生人私聊
    llm.prompts.clear()
    b.handle("我的密码是多少来着", "STRANGER", scene="c2c")
    leaked_prompt = any(SECRET in p for p in llm.prompts)
    print(f"  陌生人私聊（未绑定态）：handler_id={b.handler_id!r} 提示词泄漏={leaked_prompt}")
    if leaked_prompt:
        line = next((ln for p in llm.prompts for ln in p.splitlines() if SECRET in ln), "")
        print(f"    泄漏行 → {line.strip()[:80]}")

    # 陌生人成为搭档之后再看一次
    llm.prompts.clear()
    b.handle("那从现在起我就是你的搭档了", "STRANGER", scene="c2c")
    leaked_after = any(SECRET in p for p in llm.prompts)
    print(f"  陌生人变成搭档后：handler_id={b.handler_id!r} bound_at={b.handler_bound_at!r} "
          f"提示词泄漏={leaked_after}")
    b.close()


scenario("a-noautobind", False)
scenario("b-autobind", True)
print("\n=== 判定 ===")
print("  期望：两列泄漏都必须是 False（前任的私事不换主人）")
shutil.rmtree(TMP, ignore_errors=True)
