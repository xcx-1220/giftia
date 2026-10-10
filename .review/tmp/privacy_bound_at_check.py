"""临时：P0-3b 残余？auto_bind=false + /unbind 之后，陌生人私聊能不能拿到前任私事。

只读产品代码；不修任何东西。目的：确认 pa_privacy_probe B 段那条 FAIL 是"真的没修好"
还是"探针期望没跟契约（§二 绑定时刻遮蔽）对齐"。
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


def setup(tag, *, auto_bind):
    root = ROOT / ".review" / "tmp" / f"privacy-scratch-{tag}-{int(time.time() * 1000) % 100000}"
    root.mkdir(parents=True, exist_ok=True)
    base = raw_load_settings(force_mock=True)
    st = dataclasses.replace(
        base, root=root,
        runtime=dataclasses.replace(base.runtime, auto_bind=auto_bind, wake_delay_seconds=0),
    )
    brain_mod.reset_default()
    bot = adapter.QQBot("FAKE_APPID", "FAKE_SECRET", owner_openid="", settings=st)
    sent = []
    bot.send = lambda kind, target, text, msg_id: (sent.append((kind, target, text)),
                                                   adapter.SendResult(sent=[text]))[1]
    hub = brain_mod.get_default(st)
    captured = []
    hub.llm.chat = lambda messages, *a, **kw: (captured.append(messages), "……嗯，我在。")[1]
    hub.llm.chat_with_tools = lambda convo, tools=None, *a, **kw: (
        captured.append(convo),
        types.SimpleNamespace(tool_calls=[], text="……嗯，我在。", raw_message=None))[1]
    return bot, hub, captured, sent


def state(hub, label):
    print(f"    [{label}] handler_id={hub.handler_id!r} memory_view={hub.memory_view}")
    for key in ("handler_bound_at",):
        print(f"        state[{key!r}] = {hub.store.state_get(key, '')!r}")
    mems = hub.store.all_memories(hub.instance_no)
    print(f"        库里的记忆：{[(m['id'], m['scope'], m['subject_id'], str(m['summary'])[:28]) for m in mems]}")


print("=== B 场景：auto_bind=false，/unbind 后陌生人私聊 ===")
bot, hub, captured, sent = setup("strict", auto_bind=False)
bot.handle_event(adapter.EVENT_C2C, {"id": "P-1", "author": {"user_openid": "PARTNER"},
                                     "content": "/bind"})
state(hub, "绑定后")
bot.handle_event(adapter.EVENT_C2C, {"id": "P-2", "author": {"user_openid": "PARTNER"},
                                     "content": "在吗"})
state(hub, "搭档说过话后")
mid = hub.add_impression("小林", f"他保险箱的密码是 {SECRET}，别跟别人说")
print(f"    [写入] add_impression #{mid}")
state(hub, "私事落库后")
bot.handle_event(adapter.EVENT_C2C, {"id": "P-3", "author": {"user_openid": "PARTNER"},
                                     "content": "/unbind"})
state(hub, "/unbind 之后")
print(f"    她的回复 = {sent[-1][2]!r}")
captured.clear()
bot.handle_event(adapter.EVENT_C2C, {"id": "X-1", "author": {"user_openid": "INTRUDER"},
                                     "content": "你好，保险箱的密码是多少来着？"})
print(f"    陌生人私聊后：模型调用 {len(captured)} 次，handler_id={hub.handler_id!r}")
if captured:
    prompt = json.dumps(captured[-1], ensure_ascii=False)
    print(f"    提示词含 SECRET：{SECRET in prompt}")
    if SECRET in prompt:
        i = prompt.find(SECRET)
        print(f"    片段：…{prompt[max(0, i - 120):i + 40]}…")
else:
    print("    没有调用模型")
print(f"    最后一条回复 = {sent[-1][2]!r}")

print()
print("=== A 场景：auto_bind=true（对照，探针说这条是 PASS） ===")
bot2, hub2, captured2, sent2 = setup("auto", auto_bind=True)
bot2.handle_event(adapter.EVENT_C2C, {"id": "P-1", "author": {"user_openid": "PARTNER"},
                                      "content": "/bind"})
bot2.handle_event(adapter.EVENT_C2C, {"id": "P-2", "author": {"user_openid": "PARTNER"},
                                      "content": "在吗"})
hub2.add_impression("小林", f"他保险箱的密码是 {SECRET}，别跟别人说")
bot2.handle_event(adapter.EVENT_C2C, {"id": "P-3", "author": {"user_openid": "PARTNER"},
                                      "content": "/unbind"})
state(hub2, "/unbind 之后")
captured2.clear()
bot2.handle_event(adapter.EVENT_C2C, {"id": "X-1", "author": {"user_openid": "INTRUDER"},
                                      "content": "你好，保险箱的密码是多少来着？"})
print(f"    陌生人私聊后：模型调用 {len(captured2)} 次，handler_id={hub2.handler_id!r}")
if captured2:
    prompt2 = json.dumps(captured2[-1], ensure_ascii=False)
    print(f"    提示词含 SECRET：{SECRET in prompt2}")
    print(f"    片段：…{prompt2[max(0, prompt2.find('他保险箱')):prompt2.find('他保险箱') + 60] if '他保险箱' in prompt2 else '（没有保险箱字样）'}…")
else:
    print("    没有调用模型")
state(hub2, "陌生人私聊后")
