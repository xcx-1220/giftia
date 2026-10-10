"""临时：旁听 8 条之后，模型调用到底发生在哪一步（P1-14 修复后的真实语义）。

问题：qa_claims 那条断言该写成什么才算「README 的承诺 + 修复后的行为」？
  · 旁听逐条不调 chat？
  · 旁听当轮连 extract 也不调（延迟整理）？
  · 攒下的旁听消息最终还会不会变成群的记忆（要有一个正向对照）？
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".review" / "probes"))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from qa_harness import RecordingLLM, make_brain, make_dir, make_settings, rm_dir  # noqa: E402

tmp = make_dir("listen-scratch")
try:
    settings = make_settings(tmp)
    llm = RecordingLLM(settings.model)
    b = make_brain(dataclasses.replace(settings, root=tmp), llm)
    b.set_handler_id("u_owner")
    print(f"consolidate_every={settings.memory.consolidate_every}")

    for i in range(8):
        b.handle(f"群里第 {i} 句闲聊，没点到她，说的是今天下午打球的事", "u_aw",
                 scene="group", group_id="gA", speaker_name="阿伟", log_only=True)
    print(f"8 条旁听后： chat={llm.chat_count} extract={llm.extract_count} "
          f"listen_pending={b.store.state_get('listen_pending')!r}")
    print(f"  群记忆条数={len(b.store.active_memories(b.instance_no, view=[('group', 'gA')]))}")

    # 搭档说话：_post_turn → flush_listen_pending
    b.handle("我在呢", "u_owner", scene="c2c")
    print(f"搭档说一句后： chat={llm.chat_count} extract={llm.extract_count} "
          f"listen_pending={b.store.state_get('listen_pending')!r}")
    gmems = b.store.active_memories(b.instance_no, view=[("group", "gA")])
    print(f"  群记忆条数={len(gmems)}：{[str(m['summary'])[:34] for m in gmems]}")

    # 对照：逼一次整理，确认旁听来的话真的还在待整理集合里
    n_before = llm.extract_count
    made = b.consolidate(force=True)
    print(f"consolidate(force=True) → 新增记忆 {made} 条，extract {n_before} → {llm.extract_count}")
    gmems2 = b.store.active_memories(b.instance_no, view=[("group", "gA")])
    print(f"  群记忆条数={len(gmems2)}：{[str(m['summary'])[:34] for m in gmems2]}")
    b.close()
finally:
    rm_dir(tmp)
