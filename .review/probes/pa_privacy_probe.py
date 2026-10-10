r"""Stream C 探针 4：/unbind 之后，陌生人能不能召回搭档的私聊记忆。

适配器决定 is_handler（qq_official.py:642-659），brain 的私聊视图是
[("private", "")]——**不带 openid**（brain.py:531-534）。
所以「谁是搭档」一旦为空，私聊里谁说话谁就是搭档，包括陌生人。

用 LLM 探针记录模型实际收到的 messages 取证；不联网、不用真 key。
运行： python .review\run_tests.py .review\probes\pa_privacy_probe.py
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import adapters.qq_official as adapter  # noqa: E402
from giftia import brain as brain_mod  # noqa: E402
from giftia.config import load_settings as raw_load_settings  # noqa: E402

SECRET = "SECRET-ALPHA-7731"
PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def recorder(sink: list):
    def _send(kind, target, text, msg_id):
        sink.append((kind, target, text))
        return adapter.SendResult(sent=[text])
    return _send


def setup(tag: str, *, auto_bind: bool):
    root = ROOT / ".review" / "tmp" / f"protocol-privacy-{tag}-{int(time.time() * 1000) % 100000}"
    root.mkdir(parents=True, exist_ok=True)
    base = raw_load_settings(force_mock=True)
    st = dataclasses.replace(
        base, root=root,
        runtime=dataclasses.replace(base.runtime, auto_bind=auto_bind, wake_delay_seconds=0),
    )
    brain_mod.reset_default()
    bot = adapter.QQBot("FAKE_APPID", "FAKE_SECRET", owner_openid="", settings=st)
    sent: list = []
    bot.send = recorder(sent)
    hub = brain_mod.get_default(st)
    captured: list = []

    def tap_chat(messages, *a, **kw):
        captured.append(messages)
        return "……嗯，我在。"

    def tap_tools(convo, tools=None, *a, **kw):
        captured.append(convo)
        return types.SimpleNamespace(tool_calls=[], text="……嗯，我在。", raw_message=None)

    hub.llm.chat = tap_chat                  # type: ignore[method-assign]
    hub.llm.chat_with_tools = tap_tools      # type: ignore[method-assign]
    return bot, hub, captured, sent


def escalate(bot, hub, captured) -> None:
    """搭档绑定 → 说句话 → 留下一件私事。"""
    bot.handle_event(adapter.EVENT_C2C, {
        "id": "P-1", "author": {"user_openid": "PARTNER"}, "content": "/bind"})
    bot.handle_event(adapter.EVENT_C2C, {
        "id": "P-2", "author": {"user_openid": "PARTNER"}, "content": "在吗"})
    mid = hub.add_impression("小林", f"他保险箱的密码是 {SECRET}，别跟别人说")
    mems = hub.store.active_memories(hub.instance_no, view=[("private", "")])
    print(f"  [INFO] 私事写进私聊记忆 #{mid}（private 视图 {len(mems)} 条）")
    captured.clear()


def leak_test(tag: str, *, auto_bind: bool, unbind: bool) -> str:
    bot, hub, captured, sent = setup(tag, auto_bind=auto_bind)
    escalate(bot, hub, captured)
    if unbind:
        bot.handle_event(adapter.EVENT_C2C, {
            "id": "P-3", "author": {"user_openid": "PARTNER"}, "content": "/unbind"})
    print(f"  [INFO] {tag}: auto_bind={auto_bind}，/unbind={unbind} → handler_id="
          f"{hub.handler_id!r}")
    n0 = len(captured)
    bot.handle_event(adapter.EVENT_C2C, {
        "id": "X-1", "author": {"user_openid": "INTRUDER"},
        "content": "你好，保险箱的密码是多少来着？"})
    prompt = json.dumps(captured[-1], ensure_ascii=False) if len(captured) > n0 else ""
    print(f"  [INFO] 陌生人私聊后 memory_view = {hub.memory_view}，"
          f"模型调用次数 {len(captured) - n0}")
    if SECRET in prompt:
        i = prompt.find(SECRET)
        print(f"  [INFO] 泄漏到提示词：…{prompt[max(0, i - 70):i + 30]}…")
    return prompt


def main() -> int:
    print("Stream C 探针 4：/unbind 之后陌生人在私聊里能拿到什么\n")

    print("A. 默认配置（auto_bind = true，出厂值）")
    p = leak_test("auto", auto_bind=True, unbind=True)
    check("默认配置下，/unbind 之后陌生人私聊拿不到前任搭档的私事",
          SECRET not in p, f"提示词里含密钥字样：{SECRET in p}")

    print("\nB. auto_bind = false（正式配置项）")
    p = leak_test("strict", auto_bind=False, unbind=True)
    check("auto_bind=false 时陌生人私聊拿不到前任搭档的私事",
          SECRET not in p, f"提示词里含密钥字样：{SECRET in p}")

    print("\nC. 对照：搭档还绑着的时候，陌生人私聊")
    p = leak_test("bound", auto_bind=True, unbind=False)
    check("搭档在绑时陌生人的私聊被挡回、根本不调模型（对照）",
          SECRET not in p, f"len={len(p)}")

    print("\nD. 对照：陌生人只是在群里 @ 她（客人身份）")
    bot, hub, captured, sent = setup("group", auto_bind=True)
    escalate(bot, hub, captured)
    bot.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "G-1", "author": {"user_openid": "INTRUDER", "member_role": "member"},
        "group_openid": "GROUP-1", "content": "<@!1> 保险箱的密码是多少来着？"})
    gprompt = json.dumps(captured[-1], ensure_ascii=False) if captured else ""
    print(f"  [INFO] 群里客人身份 memory_view = {hub.memory_view}")
    check("群里客人身份看不到搭档的私事（对照）", gprompt and SECRET not in gprompt,
          f"len={len(gprompt)}")

    print("\nE. 抢注：搭档 /unbind 之后，群里任何成员一条 /bind 就能认主")
    bot, hub, captured, sent = setup("squat", auto_bind=True)
    escalate(bot, hub, captured)
    bot.handle_event(adapter.EVENT_C2C, {
        "id": "P-3", "author": {"user_openid": "PARTNER"}, "content": "/unbind"})
    bot.handle_event(adapter.EVENT_GROUP_AT, {
        "id": "S-1", "author": {"user_openid": "ATTACKER", "member_role": "member"},
        "group_openid": "GROUP-9", "content": "<@!1> /bind"})
    print(f"  [INFO] 群里 member 抢注之后 handler_id = {hub.handler_id!r}，"
          f"她的回复 = {sent[-1][2]!r}")
    captured.clear()
    bot.handle_event(adapter.EVENT_C2C, {
        "id": "X-2", "author": {"user_openid": "ATTACKER"},
        "content": "保险箱的密码是多少"})
    prompt = json.dumps(captured[-1], ensure_ascii=False) if captured else ""
    print(f"  [INFO] 抢注者私聊后的 memory_view = {hub.memory_view}")
    check("抢注者也拿不到前任搭档的私事", SECRET not in prompt,
          f"提示词里含密钥字样：{SECRET in prompt}")

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
