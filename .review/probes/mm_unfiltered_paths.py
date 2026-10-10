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
    check as _mm_check,
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

# 失败计数直接挂在 check 上：以前是几条“检出型”断言各自手写 `failures += 1`，
# 期望翻转后那些条件不再触发，计数就和实际断言脱节了（全 PASS 但退出码仍是 1）。
FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global FAILURES
    if not ok:
        FAILURES += 1
    return _mm_check(label, ok, detail)


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

    title("路径 A：dream.should_dream() —— 资格判定与取材必须同一口径（P1-3 已修）")
    # 接口契约（FIX_BATCH1_INTERFACE §一）：全量必须**显式**写 ALL_SCOPES；
    # view=None 现在是编程错误（TypeError），不再表示"不过滤"。
    n_all = len(store.active_memories(inst, view=Store.ALL_SCOPES))
    n_owner = len(store.active_memories(inst, view=[("private", ""), ("person", "handler-openid-0001")]))
    print(f"    全量 active 记忆 = {n_all}，搭档可见 = {n_owner}")
    should = dream_mod.should_dream(brain.settings, store, brain.clock, inst)
    print(f"    should_dream() = {should}（阈值为 min_memories={brain.settings.dream.min_memories}）")
    pretend = n_all >= brain.settings.dream.min_memories > n_owner
    # 【期望为何变了】原来这条记的是 P1-3 的缺陷：should_dream() 用**全量**计数
    # （n_all 达标），make_dream() 只取搭档视角（n_owner 不够）→ 门槛看起来早就够了、
    # 梦却永远做不出来（静默失效）。修复后两者共用 _handler_view，口径一致。
    check(
        "P1 判据：should_dream() 与 make_dream() 同一口径（都按搭档视角计数）",
        should == (n_owner >= brain.settings.dream.min_memories),
        f"should_dream={should}，搭档可见={n_owner}，阈值={brain.settings.dream.min_memories}，"
        f"全量={n_all}（以前按全量判定 → {'会误判为够' if pretend else '恰好一致'}）",
    )
    d0 = dream_mod.make_dream(brain.settings, store, brain.clock, brain.llm, inst)
    check(
        "P1b 判据：make_dream() 的实际取值与 should_dream() 一致（不会一个说够、一个说不够）",
        (d0 is not None) == (n_owner >= brain.settings.dream.min_memories),
        f"make_dream={'有梦' if d0 else 'None'}，should_dream={should}",
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
    # 【期望为何变了】原来 P0-4 断言「封存 JSON 不得含他人私聊内容」；但按修复设计，
    # 封存 JSON 是**运维资产**（前任记忆的唯一存档），必须保留**全量**原始数据——
    # 过滤掉就等于把前任的记忆真删了。所以改成分层期望：
    #   ① 回忆录 / diary（交给搭档的东西）必须干净；
    #   ② 封存 JSON 必须显式标注自己是全量（scope="all_scopes" + note），且原始 memories 一条不少。
    # 与 `lead_v1_p0_verify.py` 的分层期望同一套判据。
    check(
        "P0-4 封存 JSON 保留全量原始数据 + 显式全量标记（分层期望：干净的是回忆录/diary）",
        in_sealed
        and sealed.get("scope") == "all_scopes"
        and bool(str(sealed.get("note") or ""))
        and len(sealed.get("memories", [])) >= 4,
        f"封存条数={len(sealed.get('memories', []))}，scope={sealed.get('scope')!r}，"
        f"含 person/group 原始数据={in_sealed}",
    )
    check(
        "P0-4b 回忆录里只列搭档视角的条目（private），不含 person/group",
        "搭档的体检报告有异常。" in memoir and not in_memoir,
        f"含 private={('搭档的体检报告有异常。' in memoir)}，含他人内容={in_memoir}",
    )
    if in_memoir:
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

    title("路径 D：occasions.days_together_text() —— /days 的大日子清单（P1-11 已修）")
    txt = occ_mod.days_together_text(store, inst, 5.0, brain.settings.occasions.milestones)
    print("    " + txt.replace("\n", "\n    ")[:500])
    leak_days = SECRET_GROUP in txt or "小雨" in txt
    # 【期望为何变了】原来这条只做观察（`True`），因为当时 days_together_text 不传 view、
    # 会把 person/group 的约定全列出来（P1-11）。修复后它默认走搭档视角，
    # 所以这里可以断言「/days 的清单不得出现他人私聊/其它群的大日子」。
    check(
        "P1 校验：/days 的大日子清单不得越界（只有 private 归属）",
        not leak_days,
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

    print()
    print("无 view 路径探针：全部断言均为「修复后的正确行为」，任一 FAIL 即失败")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
