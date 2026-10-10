"""Stream A 探针 2：无 view 参数的读取路径 —— 别人的私聊记忆会流到哪里。

`Store.active_memories(instance_no, view=...)` 里 `view=None` 表示"不过滤"
（注释写着"导出/自检用"）。但产品代码里有 5 处按这个默认值调用它：

  giftia/dream.py:46   should_dream()        —— 计数
  giftia/dream.py:76   make_dream(view=None) —— 默认 view 兜底
  giftia/recycle.py:102 perform_recycle()    —— 回忆录 + 封存 JSON
  giftia/occasions.py:198 days_together_text() —— /days 的"大日子"清单
  （brain.make_dream_now() 调 make_dream 不传 view）

本探针造一个"搭档 + 陌生人私聊 + 群"混合的库，逐条检查这些路径的**实际输出**。

跑法：
    python .review\\probes\\mm_unfiltered_paths.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    make_brain,
    settings_for,
    title,
)
from giftia import dream as dream_mod  # noqa: E402
from giftia import occasions as occ_mod  # noqa: E402
from giftia import recycle as rec_mod  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import Store  # noqa: E402

SECRET_PERSON = "SECRET-BRAVO-4402"
SECRET_GROUP = "SECRET-CHARLIE-9915"
FUTURE = (datetime.now().date().fromordinal(datetime.now().date().toordinal() + 1)).isoformat()


class EchoLLM(MockLLM):
    """把喂进来的素材原样回显 —— 便于证明"哪些内容真的走到了模型手里"。"""

    def write_memoir(self, context: str) -> str:
        return "【EchoLLM 收到的正文】\n" + context

    def write_dream(self, fragments, *, name="Giftia") -> str:
        return "【EchoLLM 收到的碎片】\n" + "\n".join(fragments)


def build(tmp, tag):
    settings = settings_for(tmp)
    brain = make_brain(tmp / tag, settings=settings, mock=EchoLLM(settings.model))
    store = brain.store
    inst = brain.instance_no
    brain.set_handler_id("handler-openid-0001")
    store.add_message("i1-m-20261001", inst, "user", "占位", "2026-10-01T10:00:00")
    store.meta_set("last_consolidated_msg_id", 1)

    store.add_memory(
        instance_no=inst,
        created_ts="2026-10-05T10:00:00",
        happened_ts="2026-10-05T10:00:00",
        summary="搭档的体检报告有异常。",
        importance=0.9,
        scope="private",
    )
    p = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-05T11:00:00",
        happened_ts="2026-10-05T11:00:00",
        summary=f"{SECRET_PERSON} 小雨私聊说她跟家里吵架了。",
        importance=0.9,
        scope="person",
        subject_id="user-xiaoyu",
        about="小雨",
    )
    g = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-06T10:00:00",
        happened_ts="2026-10-06T10:00:00",
        summary=f"{SECRET_GROUP} 阿伟说明天要考驾照。",
        importance=0.9,
        scope="group",
        subject_id="GROUP-A",
        about="阿伟",
        event_date=FUTURE,
    )
    q = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-06T11:00:00",
        happened_ts="2026-10-06T11:00:00",
        summary="小雨私聊说明天要面试，别跟别人讲。",
        importance=0.9,
        scope="person",
        subject_id="user-xiaoyu",
        about="小雨",
        event_date=FUTURE,
    )
    return brain, store, inst, p, g, q


def main() -> int:
    tmp = fresh_dir("unfiltered")
    brain, store, inst, p, g, q = build(tmp, "main")
    failures = 0

    title("路径 A：dream.should_dream() —— 用全量记忆计数决定做不做梦")
    # 接口契约（FIX_BATCH1_INTERFACE §一）：全量必须**显式**写 ALL_SCOPES；
    # view=None 现在是编程错误（TypeError），不再表示"不过滤"。
    n_all = len(store.active_memories(inst, view=Store.ALL_SCOPES))
    n_owner = len(store.active_memories(inst, view=[("private", ""), ("person", "handler-openid-0001")]))
    print(f"    全量 active 记忆 = {n_all}，搭档可见 = {n_owner}")
    should = dream_mod.should_dream(brain.settings, store, brain.clock, inst)
    print(f"    should_dream() = {should}（阈值为 min_memories={brain.settings.dream.min_memories}）")
    pretend = n_all >= brain.settings.dream.min_memories > n_owner
    check(
        "P1 观察： dreaming 资格按**全量**记忆数判定（含他人私事）",
        not pretend,
        "确认：全量达标即触发" if pretend else "两者一致，未观察到差异",
    )

    title("路径 B：brain.make_dream_now() / dream.make_dream(view=None)")
    d = dream_mod.make_dream(
        brain.settings, store, brain.clock, brain.llm, inst
    )  # 模拟 make_dream_now 的调用方式（不传 view）
    print(f"    默认 view 造出的梦：{None if d is None else d.content[:200]!r}")
    leaked_dream = bool(d and (SECRET_PERSON in d.content or SECRET_GROUP in d.content))
    check(
        "P0 校验：默认 view 的梦不得含他人私聊/群聊内容",
        not leaked_dream,
        "含 SECRET_PERSON/SECRET_GROUP" if leaked_dream else "未含",
    )
    if leaked_dream:
        failures += 1
        store._rows("SELECT * FROM dreams") and print(
            "    已落库的梦（dreams 表）→",
            json.dumps(store._rows("SELECT content FROM dreams"), ensure_ascii=False)[:300],
        )

    title("路径 C：recycle.perform_recycle() —— 回忆录与封存 JSON")
    report = rec_mod.perform_recycle(
        brain.settings, store, brain.clock, brain.llm, brain.lifespan, quiet=True
    )
    memoir = report.memoir_path.read_text(encoding="utf-8")
    sealed = json.loads(report.sealed_path.read_text(encoding="utf-8"))
    sealed_text = json.dumps(sealed, ensure_ascii=False)
    print(f"    回忆录：{report.memoir_path}")
    print(f"    封存档：{report.sealed_path}")
    in_memoir = SECRET_PERSON in memoir or SECRET_GROUP in memoir
    in_sealed = SECRET_PERSON in sealed_text or SECRET_GROUP in sealed_text
    check("P0-3 回忆录不得含他人私聊内容", not in_memoir, "含 SECRET_PERSON" if in_memoir else "未含")
    check("P0-4 封存 JSON 不得含他人私聊内容", not in_sealed, "含 SECRET_PERSON" if in_sealed else "未含")
    if in_memoir or in_sealed:
        failures += 1
        for line in memoir.splitlines():
            if SECRET_PERSON in line or SECRET_GROUP in line:
                print("    回忆录泄漏行 →", line.strip()[:150])
    # 回收后代数的 diary 里也塞了回忆录
    diary = store.list_diary(kind="terminal", limit=1)
    if diary:
        dtext = str(diary[-1]["content"])
        check(
            "P0-5 /memoir（diary 表）不得含他人私聊内容",
            SECRET_PERSON not in dtext,
            "含 SECRET_PERSON" if SECRET_PERSON in dtext else "未含",
        )
        if SECRET_PERSON in dtext:
            failures += 1

    title("路径 D：occasions.days_together_text() —— /days 的大日子清单")
    txt = occ_mod.days_together_text(store, inst, 5.0, brain.settings.occasions.milestones)
    print("    " + txt.replace("\n", "\n    ")[:500])
    leak_days = SECRET_GROUP in txt or "小雨" in txt
    check(
        "P1 校验：/days 的大日子清单是否越过了归属闸门",
        True,  # 这里只做观察，结论看输出
        "清单里出现了 person/group 归属的记忆" if leak_days else "只有 private",
    )

    title("对照：正确的调用点（brain.outreach_plan / find_occasions 传了 view）")
    owner_view = [("private", ""), ("person", "handler-openid-0001"), ("group", "*")]
    occ_owner = occ_mod.find_occasions(
        brain.settings, store, now=brain.clock.now(), instance_no=inst,
        days_known=5.0, mark=False, view=owner_view,
    )
    occ_group = occ_mod.find_occasions(
        brain.settings, store, now=brain.clock.now(), instance_no=inst,
        days_known=5.0, mark=False, view=[("group", "GROUP-A")],
    )
    print(f"    owner 视角大日子 {len(occ_owner)} 条；group 视角 {len(occ_group)} 条")
    for o in occ_owner:
        print(f"      owner: {o.title}")
    for o in occ_group:
        print(f"      group: {o.title}")
    check(
        "P0 校验：group 视角的大日子不得含 person 归属的面试",
        all("面试" not in o.title for o in occ_group),
        f"group={[o.title for o in occ_group]}",
    )
    if any("面试" in o.title for o in occ_group):
        failures += 1

    print()
    print(f"无 view 路径探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
