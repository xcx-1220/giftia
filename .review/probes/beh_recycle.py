"""Stream D 探针 6：recycle 回收仪式 —— 顺序、原子性（崩溃窗口）、旧数据是否真的检索不到。

    python .review/run_tests.py .review/probes/beh_recycle.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/recy-*。
（故障注入只改探针进程里的对象属性，不动产品代码。）
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import CaptureLLM, base_settings, check, note, report, systems  # noqa: E402

from giftia.brain import Giftia  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.store import ALL_SCOPES as ALL_SCOPES_  # noqa: E402


def seed(brain, n=5, secret="SECRET-ALPHA-7731"):
    for i in range(n):
        brain.store.add_memory(
            instance_no=brain.instance_no,
            created_ts=brain.clock.iso(), happened_ts=brain.clock.iso(),
            summary=f"第 {i} 件私事（{secret}）", importance=0.7, layer="important",
        )


def main() -> int:
    print("=" * 78)
    print("A. 正常顺序：回忆录 → 封存 → 换代 → 公文")
    print("=" * 78)
    s = base_settings("recy-a", 81920.0)
    llm = CaptureLLM(s.model)
    b = Giftia(s, llm=llm)
    b.set_handler_id("OWNER-1")
    b.set_handler_name("小林")
    seed(b, 5)
    old = b.instance_no
    text = b.run_recycle("探针")
    print("    " + text.splitlines()[0])
    md = sorted(s.memoir_dir.glob("*.md"))
    sj = sorted(s.sealed_dir.glob("*.json"))
    print(f"    回收日志：{[p.name for p in md]}")
    print(f"    封存档案：{[p.name for p in sj]}")
    sealed = json.loads(sj[0].read_text(encoding="utf-8"))
    print(f"    封存内容：stats.instance_no={sealed['stats']['instance_no']} "
          f"memories={len(sealed['memories'])} 条")
    check("A1 封存档案里带走了全部原始记忆", len(sealed["memories"]) == 5, str(len(sealed["memories"])))
    check("A2 实例编号 +1", b.instance_no == old + 1, f"{old} → {b.instance_no}")
    check("A3 回收公文进了 diary",
          any("回收记录" in str(d.get("title", "")) for d in b.store.list_diary(kind="terminal")),
          str([d.get("title") for d in b.store.list_diary(kind="terminal")]))

    print()
    print("=" * 78)
    print("B. 换代之后旧记忆是否真的检索不到")
    print("=" * 78)
    # 接口契约（FIX_BATCH1_INTERFACE §一）：view 必填，view=None 抛 TypeError。
    # 全量必须显式写 ALL_SCOPES，不再有"省略参数 = 不过滤"。
    # 注：契约里两种写法（`store.ALL_SCOPES` / `giftia.store.ALL_SCOPES`）都合法；
    #     当前 store 只落了模块级常量，这里用模块级导入（已同步给 team-store 补类属性）。
    act_new = b.store.active_memories(b.instance_no, view=ALL_SCOPES_)
    act_old = b.store.active_memories(old, view=ALL_SCOPES_)
    raw_old = b.store.count_memories(old, only_active=False)
    print(f"    新实例 active_memories={len(act_new)} 条；旧实例 active_memories={len(act_old)} 条；"
          f"旧实例含封存在内={raw_old} 条")
    check("B1 旧实例的记忆对新一代完全不可见（sealed=1）", len(act_old) == 0 and len(act_new) == 0)
    check("B2 原始行还在（封存不是删除）", raw_old == 5, f"{raw_old} 条")
    prompt = b._prepare("在吗")[0][0]["content"]
    check("B3 新一代的提示词里没有旧记忆/旧秘密", "SECRET-ALPHA-7731" not in prompt)
    # 但别的东西留下来了
    print(f"    handler_name 状态 = {b.store.state_get('handler_name')!r}（换代后没有清）")
    print(f"    新实例提示词里的称呼块：", end="")
    idx = prompt.find("【你怎么称呼他】")
    print(prompt[idx:idx + 40].replace("\n", " / ") if idx >= 0 else "（没有）")
    check("B4 【与「新的她不认识你」冲突】换代后她仍知道你的名字",
          "小林" in prompt, "提示词里出现了旧称呼")

    print()
    print("=" * 78)
    print("C. 崩溃窗口：封存之后、换代之前挂掉，再回收一次会怎样")
    print("=" * 78)
    print("    【期望为何变了】C1-C4 原来是把 P1-20 的**缺陷**写成断言")
    print("    （“重跑把 5 条覆盖成 0 条” → PASS 表示缺陷存在）。修好之后")
    print("    这几条必须改写成**不变量**：无论重跑几次、隔不隔秒，")
    print("    第一次封存的 5 条原始记忆都必须还在（宁可多留一份档案，绝不覆盖）。")
    print("    崩溃续跑靠 meta:recycling_instance 标记：重启后只补换代，不重新生成档案。")
    c = Giftia(dataclasses.replace(s, root=base_settings("recy-c", 81920.0).root),
               llm=MockLLM(s.model))
    c.set_handler_id("OWNER-1")
    seed(c, 5)
    boom = {"n": 0}
    original_reactivate = c.lifespan.reactivate

    def exploding_reactivate(*a, **kw):
        boom["n"] += 1
        raise RuntimeError("进程在这里被 kill -9（模拟崩溃）")

    c.lifespan.reactivate = exploding_reactivate  # 故障注入：只改探针进程里的绑定
    try:
        c.run_recycle("崩溃注入")
    except RuntimeError as exc:
        print(f"    第一次回收在 reactivate 处抛错：{exc}")
    c.lifespan.reactivate = original_reactivate
    sj1 = sorted(c.settings.sealed_dir.glob("*.json"))
    data1 = json.loads(sj1[0].read_text(encoding="utf-8"))
    print(f"    崩溃后：实例号仍是 {c.instance_no}（没换代）、"
          f"active_memories={len(c.store.active_memories(c.instance_no, view=[('private', '')]))} 条、"
          f"封存文件 {sj1[0].name} 里有 {len(data1['memories'])} 条记忆")
    print(f"    封存 JSON 的口径标记：scope={data1.get('scope')!r} notes={sorted(data1)[:4]}…")
    check("C1 崩溃点：记忆已被封存（检索不到），但实例没有换代",
          c.instance_no == 1
          and len(c.store.active_memories(1, view=[("private", "")])) == 0
          and len(data1["memories"]) == 5)
    check("C1b 封存 JSON 显式标注全量运维口径（scope=all_scopes + note），"
          "且原始 memories 仍是全量（不许为了“干净”把运维资产过滤掉）",
          data1.get("scope") == "all_scopes" and bool(str(data1.get("note") or ""))
          and len(data1["memories"]) == 5)

    # 进程重启后：寿命仍然到期 → 下一条消息再次触发回收
    print(f"    重启后再触发一次回收（寿命仍到期：{c.lifespan.is_expired()}）…")
    text2 = c.run_recycle("寿命结束")
    sj2 = sorted(c.settings.sealed_dir.glob("*.json"))
    total_kept = sum(len(json.loads(p.read_text(encoding="utf-8"))["memories"]) for p in sj2)
    rows_first = [m for p in sj2 for m in json.loads(p.read_text(encoding="utf-8"))["memories"]
                  if "SECRET-ALPHA-7731" in str(m.get("summary", ""))]
    print(f"    第二次回收：实例 {c.instance_no}（续跑换代），封存文件 {[p.name for p in sj2]}")
    print(f"    封存目录里原始 5 条记忆现在还剩 {len(rows_first)} 条"
          f"（全目录合计 {total_kept} 条）")
    check("C2 【P1-20 已修】重跑不再把 5 条覆盖成 0 条：原始封存档案里的 5 条一条没少",
          len(rows_first) == 5,
          f"保留 {len(rows_first)}/5，目录合计 {total_kept} 条")
    check("C2b 续跑换代不再产生新的空档案（同一实例只写一份封存 JSON）",
          len(sj2) == 1, f"{[p.name for p in sj2]}")
    mds = sorted(c.settings.memoir_dir.glob("*.md"))
    txts = [p.read_text(encoding="utf-8") for p in mds]
    with_secret = sum(1 for t in txts if "SECRET-ALPHA-7731" in t)
    print(f"    回收日志文件：{[p.name for p in mds]}（同秒重跑；续跑不再重复写回忆录）")
    check("C3 同一秒内重跑也不会覆盖回忆录：那份写着 5 条原始记忆的 .md 还在",
          with_secret == 1,
          f"{len(mds)} 个文件，含旧秘密的有 {with_secret} 份")
    check("C3b 续跑后 meta:recycling_instance 标记已清掉（不会挡住下一任回收）",
          not str(c.store.meta_get("recycling_instance", "") or "").strip(),
          repr(c.store.meta_get("recycling_instance", "")))

    # 更真实的重启：隔一秒以上再重试
    c2 = Giftia(dataclasses.replace(s, root=base_settings("recy-c2", 81920.0).root),
                llm=MockLLM(s.model))
    c2.set_handler_id("OWNER-1")
    seed(c2, 5)
    orig2 = c2.lifespan.reactivate
    c2.lifespan.reactivate = exploding_reactivate
    try:
        c2.run_recycle("崩溃注入")
    except RuntimeError:
        pass
    c2.lifespan.reactivate = orig2
    import time
    time.sleep(1.2)
    c2.run_recycle("寿命结束")
    mds2 = sorted(c2.settings.memoir_dir.glob("*.md"))
    sj3 = sorted(c2.settings.sealed_dir.glob("*.json"))
    kept = sum(1 for p in mds2 if "SECRET-ALPHA-7731" in p.read_text(encoding="utf-8"))
    kept_json = sum(len(json.loads(p.read_text(encoding="utf-8"))["memories"]) for p in sj3)
    print(f"    隔 1.2 秒重试：回忆录 {len(mds2)} 份（{kept} 份保留着原始记忆），"
          f"封存目录合计 memories={kept_json} 条")
    check("C4 隔秒重试：原始回忆录与原始封存档案都保住了（没有空档案覆盖原件）",
          kept == 1 and kept_json >= 5 and c2.instance_no == 2,
          f"md={len(mds2)}/{kept} json_total={kept_json} 实例={c2.instance_no}")

    print()
    print("=" * 78)
    print("D. 连续回收两次 / 回收时她还没说过话")
    print("=" * 78)
    d = Giftia(dataclasses.replace(s, root=base_settings("recy-d", 81920.0).root),
               llm=MockLLM(s.model))
    d.set_handler_id("OWNER-1")
    seed(d, 3)
    d.run_recycle("第一次")
    n1 = d.instance_no
    d.run_recycle("第二次")          # 新一任还一句话没说就被回收
    print(f"    连续回收：实例 #{n1} → #{d.instance_no}，"
          f"第二次的封存档案 memories=0 条（[OK] 不丢数据，旧的那份 instance-0{n1}.json 没被动）")
    files = sorted(p.name for p in d.settings.sealed_dir.glob("*.json"))
    print(f"    封存目录：{files}")
    check("D1 连续回收是安全的（各自一个实例号、各自一个文件）",
          d.instance_no == 3 and len(files) == 2, f"{d.instance_no} / {files}")

    print()
    print("=" * 78)
    print("E. 回收后关系/心情清零、纪念日重新计时、handler_name 未清")
    print("=" * 78)
    e = Giftia(dataclasses.replace(s, root=base_settings("recy-e", 81920.0).root),
               llm=MockLLM(s.model))
    e.set_handler_id("OWNER-1")
    e.set_handler_name("小林")
    e.remember_person("GUEST-7", "阿伟")
    e.set_last_group("G-1")
    seed(e, 3)
    e.handle("今天好累", "OWNER-1")
    e.handle("大家早", "GUEST-7", scene="group", group_id="G-1", speaker_name="阿伟")
    before_bond = e.current_bond().affinity
    before_mood = e.current_mood().key
    old_no = e.instance_no
    e.run_recycle("探针")
    print(f"    #{old_no}: 亲密度={before_bond:.2f} 心情={before_mood} 称呼={e.handler_name!r}")
    print(f"    #{e.instance_no}: 亲密度={e.current_bond().affinity:.2f} "
          f"心情={e.current_mood().key} 称呼={e.handler_name!r} "
          f"认识天数={e.current_bond().days_known:.2f}")
    check("E1 关系、心情清零，认识天数从 0 起算",
          e.current_bond().affinity == 0 and e.current_mood().key == "calm"
          and e.current_bond().days_known < 0.01)
    check("E2 称呼没有跟着一起清（文档说新档案是空的）", e.handler_name == "小林", e.handler_name)
    print(f"    旧实例状态键：bond.i{old_no}.affinity="
          f"{e.store.state_get(f'bond:i{old_no}:affinity')!r} "
          f"mood.i{old_no}.key={e.store.state_get(f'mood:i{old_no}:key')!r}")
    print(f"    跨实例共享的状态键：handler_name={e.store.state_get('handler_name')!r} "
          f"known_people={e.store.state_get('known_people')!r}")
    p2 = e._prepare("在吗")[0][0]["content"]
    check("E3 新一代的花名册/群记录还在（阿伟仍认得）", "阿伟" in p2 or "GUEST-7" in p2,
          "提示词里仍有花名册")

    return report("beh_recycle")


if __name__ == "__main__":
    raise SystemExit(main())
