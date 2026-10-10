#!/usr/bin/env python
"""真实 API 连通性检查（会消耗极少量 token）。

    python tools/live_check.py

验证四件事，全程在**临时目录**里跑，不碰 data/ 里的正式数据：
  1. key 能否通过鉴权
  2. 她的闲聊回复（真实模型下的人格到底像不像）
  3. 真实模型的 JSON 记忆整理 —— 最容易踩坑的一环（mock 验证不了它）
  4. 回收日的回忆录生成
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia.brain import Giftia, make_llm  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.llm import LLMError  # noqa: E402
from giftia.memory import retention_of  # noqa: E402
from giftia.store import Store  # noqa: E402

TURNS = [
    "你好呀，我叫小林。今天加班到很晚，回来路上下了点雨",
    "我不太喜欢下雨天，会很闷。对了，我下周三要考驾照，有点紧张",
    "你以后别叫我搭档了，叫我小林吧",
]


def main() -> int:
    settings = load_settings()
    print(f"模型：{settings.model.name}　base_url：{settings.model.base_url}")
    print(f"思考模式：{'开' if settings.model.thinking else '关'}　temperature：{settings.model.temperature}")

    try:
        llm = make_llm(settings)
    except LLMError as exc:
        print(f"\n[失败] {exc}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="giftia-live-"))
    live = dataclasses.replace(settings, root=tmp)
    brain = Giftia(live, llm=llm)

    print("\n── 1. 鉴权与对话 ──────────────────────────────")
    for text in TURNS:
        started = time.time()
        try:
            reply = brain.handle(text)
        except LLMError as exc:
            print(f"\n[失败] 请求出错：{exc}")
            print("      → 检查 key 是否有效、账户是否有余额。")
            return 1
        elapsed = time.time() - started
        print(f"\n你　　{text}")
        print(f"{live.persona.name}　{reply}")
        print(f"      （{elapsed:.1f}s）")

    print("\n── 2. 记忆整理（真实模型的 JSON 输出，含分层判定）──")
    started = time.time()
    added = brain.consolidate(force=True)
    print(f"新增 {added} 条，用时 {time.time() - started:.1f}s\n")
    mems = brain.store.active_memories(brain.instance_no, view=Store.ALL_SCOPES)  # 自检：看全量
    if not mems:
        print("[警告] 一条记忆都没整理出来——检查 CONSOLIDATE_SYSTEM 的 JSON 约束。")
    layer_label = {"core": "内核", "important": "重要", "normal": "常规"}
    for m in mems:
        ret = retention_of(m, live.memory, brain.clock.now())
        when = f"　大日子 {m['event_date']}" if m.get("event_date") else ""
        print(
            f"  #{m['id']}　[{layer_label.get(str(m['layer']), '?')}]　"
            f"重要性 {float(m['importance']):.2f}　保留率 {ret:.2f}　"
            f"[{m['emotion']}]　标签 {m['tags']}{when}"
        )
        print(f"      {m['summary']}")
    if any(m.get("event_date") for m in mems):
        print("\n  ✓ 模型把「下周三」这类相对时间算成了具体日期（大日子功能的基础）")
    else:
        print("\n  （这次没提取到大日子；只有出现'下周三'这类可推算的日期才会填）")

    print("\n── 2b. 查询改写（真实模型的 JSON 输出）─────────")
    started = time.time()
    variants = llm.rewrite_query("我下周那个证件的事情有点紧张")
    print(f"用时 {time.time() - started:.1f}s　改写结果：{variants}")
    if not variants:
        print("[警告] 没改写出来——检查 REWRITE_SYSTEM 的 JSON 约束。")

    print("\n── 3. 检索（她在对话中实际拿到的东西）──────────")
    query = "下雨 驾照"
    from giftia.memory import recall  # 延迟导入，保持启动快

    hits = recall(query, mems, brain.status().stage, live.memory, brain.clock.now())
    print(f"检索「{query}」命中 {len(hits)} 条")
    for h in hits:
        print(f"  分数 {h.score:.3f}　{'已模糊' if h.blurred else '清晰'}：{h.text}")
    if not hits:
        print("  [警告] 召不回——检查检索分词或阈值。")

    print("\n── 4. 做梦（记忆碎片 → 一场梦）──────────────────")
    started = time.time()
    dream_text_ = brain.make_dream_now()
    print(f"（用时 {time.time() - started:.1f}s）\n{dream_text_}")
    brain.clock.advance(1)
    dream_prompt = brain._prepare("在吗")[0][0]["content"]
    print("\n  梦已进入提示词：" + ("是" if "【你昨晚做的梦】" in dream_prompt else "否"))

    print("\n── 5. 主动联系（预览，不会真的发出去）─────────────")
    plan_, said = brain.compose_outreach(force=True)
    if plan_ is None:
        print("她现在没有想主动找你的理由（那就先不打扰）。")
    else:
        print(f"理由：{plan_.reason_label}（{plan_.title}）")
        print(f"她会说：{said}")
    print("\n" + brain.outreach_text())

    print("\n── 6. 回收日与回忆录 ──────────────────────────")
    started = time.time()
    scene = brain.run_recycle("连通性检查")
    print(f"（用时 {time.time() - started:.1f}s）\n")
    files = sorted(live.memoir_dir.glob("*.md"))
    if files:
        body = files[0].read_text(encoding="utf-8")
        memoir = body.split("## 回忆录", 1)[-1].split("## 原始记忆条目", 1)[0].strip()
        print(memoir)
        print(f"\n档案：{files[0]}")
    else:
        print("[警告] 回忆录文件没生成。")
        print(scene[:200])

    brain.store.close()
    print("\n" + "═" * 52)
    print("真实链路全部跑通。临时数据已隔离，正式 data/ 未被改动。")
    print("═" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
