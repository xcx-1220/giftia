"""Stream A 探针 6：记忆整理/游标/兜底路径 + 时间换算边界。

覆盖：
  1. consolidate 分组归属：同一会话里 scope/subject_id/speaker_id 不一致的后果
  2. happened_ts 取的是 pending[0]（批内最早那条），跨天批次会不会失真
  3. 每会话游标的初始化与推进（load_pending 的全局游标兜底）
  4. 模型失败兜底（_fallback_drafts）与它写进记忆的内容
  5. human_age / humanize_hours 的边界（负数、0、超大、DST、跨年）

跑法：
    python .review\\probes\\mm_consolidate_time.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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
from giftia.lifespan import humanize_hours  # noqa: E402
from giftia.memory import build_transcript, consolidate, day_label, human_age, load_pending  # noqa: E402

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("consolidate")
    settings = settings_for(tmp)
    failures = 0

    # ── 1. 同一会话里归属不一致 ───────────────────────────────────
    title("1. sessions_with_pending 按 (session_id, scope, subject_id) 分组")
    b = make_brain(tmp / "scope_split", settings=settings)
    s, inst = b.store, b.instance_no
    sid = "i1-gGROUP-A-20261010"
    # 同一个 session_id，但 scope/subject 不同（模拟 scope 列曾经写错/手改）
    s.add_message(sid, inst, "user", "第一句：我明天要考驾照", "2026-10-10T09:00:00",
                  speaker_id="user-a", scope="group", subject_id="GROUP-A")
    s.add_message(sid, inst, "user", "第二句：我女朋友怀孕了", "2026-10-10T09:01:00",
                  speaker_id="user-b", scope="private", subject_id="")
    rows = s.sessions_with_pending(inst)
    print(f"    sessions_with_pending → {[(r['session_id'][:26], r['scope'], r['subject_id'], r['n']) for r in rows]}")
    check("同一 session 被拆成两组（各自整理）", len(rows) == 2, f"{len(rows)} 组")
    for r in rows:
        pending = load_pending(s, inst, str(r["session_id"]))
        print(f"      scope={r['scope']:<8} subject={r['subject_id']:<9} pending={len(pending)} 条")

    # ── 2. happened_ts 取批内第一条 ───────────────────────────────
    title("2. happened_ts 用的是 pending[0]['ts']（批内最早的会话日）")
    b2 = make_brain(tmp / "happened", settings=settings)
    s2, i2 = b2.store, b2.instance_no
    # 会话 id 按天轮换，所以"跨天"= 不同 session_id；同一个会话内则都是同一天。
    # 真正的失真点：drafts 里的每条记忆都共用**会话里最早那条消息**的时间戳。
    sid2 = "i1-m-20261001"
    s2.add_message(sid2, i2, "user", "早上九点说的第一件事：明天要去体检", "2026-10-01T09:00:00")
    s2.add_message(sid2, i2, "user", "晚上十一点说的第二件事：驾照科目三挂了", "2026-10-01T23:00:00")
    added = consolidate(s2, b2.llm, settings, session_id=sid2, now_iso="2026-10-10T09:00:00",
                        force=True)
    rows = s2._rows("SELECT id, summary, happened_ts, created_ts FROM memories ORDER BY id")
    print(f"    新增 {added} 条（当天 09:00 与 23:00 两条消息）：")
    for r in rows:
        print(f"      #{r['id']} happened_ts={r['happened_ts']} created_ts={r['created_ts']} "
              f"{r['summary'][:40]}")
    same_ts = len({r["happened_ts"] for r in rows}) == 1 and len(rows) > 1
    check(
        "P3-1 同一天不同时段的两件事被记成同一个 happened_ts（共 14 小时偏差）",
        not same_ts,
        f"确认：{len(rows)} 条共用 {rows[0]['happened_ts']}" if same_ts else "未观察到",
    )
    if same_ts:
        failures += 1
    print("    说明：created_ts 是整理时刻（2026-10-10），与 happened_ts 相差 9 天；"
          "而 human_age 用的是 happened_ts。")

    # ── 3. 每会话游标 ─────────────────────────────────────────────
    title("3. 每会话游标的初始化与推进")
    b3 = make_brain(tmp / "cursor", settings=settings)
    s3, i3 = b3.store, b3.instance_no
    for k in range(3):
        s3.add_message("i1-m-2026100%d" % (k + 1), i3, "user", f"会话{k}的一句话内容", f"2026-10-0{k+1}T09:00:00")
    s3.meta_set("last_consolidated_msg_id", 2)   # 假装前两个会话已整理
    s3.meta_set("last_consolidated:i1-m-20261003", 2)
    print(f"    全局游标=2，会话3游标=2")
    # 会话1 的游标没建过 → 从全局游标接上（=2），于是它自己 id=1 的消息被跳过
    p1 = load_pending(s3, i3, "i1-m-20261001")
    p3 = load_pending(s3, i3, "i1-m-20261003")
    print(f"    会话1 pending={[m['id'] for m in p1]}（期望含 id=1）")
    print(f"    会话3 pending={[m['id'] for m in p3]}（期望掉 id=2）")
    print(f"    初始化后 meta: last_consolidated:i1-m-20261001="
          f"{s3.meta_get_int('last_consolidated:i1-m-20261001', -1)}")
    lost = 1 not in [m["id"] for m in p1]
    check(
        "P2-5 新会话游标从**全局**游标接上 → 该会话更早的未整理消息被永久跳过",
        not lost,
        "确认：会话1 自己的 id=1 永远不会被整理" if lost else "未跳过",
    )
    if lost:
        failures += 1
        print("    （对升级上来的老库尤其明显：历史消息按会话分开，但全局游标可能已经走远）")

    # ── 4. 模型失败兜底 ───────────────────────────────────────────
    title("4. 整理失败兜底：_fallback_drafts 写进记忆的内容")
    from giftia.llm import LLMError, MockLLM
    from giftia.memory import _fallback_drafts

    class BoomLLM(MockLLM):
        def extract_memories(self, *a, **kw):
            raise LLMError("模拟模型不可用")

    b4 = make_brain(tmp / "fallback", settings=settings, mock=BoomLLM(settings.model))
    s4, i4 = b4.store, b4.instance_no
    s4.add_message("i1-m-20261010", i4, "user",
                   "我的银行卡密码是 8899，别告诉别人", "2026-10-10T09:00:00")
    added = consolidate(s4, b4.llm, settings, session_id="i1-m-20261010",
                        now_iso="2026-10-10T09:05:00", force=True)
    rows = s4._rows("SELECT id, summary, importance, tags, layer, scope FROM memories")
    print(f"    兜底新增 {added} 条：")
    for r in rows:
        print(f"      #{r['id']} imp={r['importance']} layer={r['layer']} "
              f"tags={r['tags']!r} scope={r['scope']!r} {r['summary'][:44]!r}")
    diary = s4.list_diary(limit=2)
    print(f"    diary：{[(d['kind'], d['title'], d['content'][:40]) for d in diary]}")
    check("兜底路径有留痕（terminal diary）", any(d["kind"] == "terminal" for d in diary))
    fallback_mem = [r for r in rows if r["tags"] == "搭档,原话"]
    check(
        "P3-2 兜底把搭档的**原话逐字**存成记忆（含'银行卡密码'这类内容，importance=0.35）",
        not (fallback_mem and "8899" in fallback_mem[0]["summary"]),
        f"{fallback_mem[0]['summary'][:40]!r}" if fallback_mem else "无兜底条目",
    )
    print("    影响：兜底条目进了长期记忆与检索，且没有经过模型的'值不值得记'过滤；"
          "密码这类内容会被原样保留（不是漏洞本身，但没有任何敏感性处理，值得记一笔）。")

    # ── 5. 时间换算边界 ───────────────────────────────────────────
    title("5. human_age / humanize_hours 边界")
    cases = [
        ("未来 1 小时", (NOW + timedelta(hours=1)).isoformat(timespec="seconds")),
        ("未来 25 小时", (NOW + timedelta(hours=25)).isoformat(timespec="seconds")),
        ("未来 400 天", (NOW + timedelta(days=400)).isoformat(timespec="seconds")),
        ("空串", ""),
        ("垃圾串", "昨天下午"),
        ("DST 切换点（本地）", datetime(2026, 3, 8, 2, 30, 0).isoformat()),
        ("跨年", datetime(NOW.year - 1, 12, 31, 23, 59, 0).isoformat(timespec="seconds")),
    ]
    for label, ts in cases:
        try:
            print(f"    {label:<22} → {human_age(ts, NOW)!r}")
        except Exception as exc:
            print(f"    {label:<22} → 抛异常 {type(exc).__name__}: {exc}")
            failures += 1
    print("    humanize_hours：")
    for h in (-5.0, 0.0, 0.0001, 0.5, 23.9, 24.0, 30 * 24, 364 * 24, 365 * 24, 81920.0, 1e9):
        print(f"      {h:>12} → {humanize_hours(h)!r}")

    # 边界：0 小时寿命 / 负寿命 / 超大
    title("5b. lifespan 边界（0 / 负数 / 超大 total_hours）")
    from giftia.clock import Clock
    from giftia.lifespan import Lifespan
    from giftia.store import Store

    for hours in (0.0, -1.0, 1e9):
        st_dir = tmp / f"life{hours}"
        store = Store(st_dir / "data" / "giftia.db")
        st = settings_for(st_dir, lifespan__total_hours=hours)
        lf = Lifespan(st, store, Clock(store))
        lf.ensure_activated()
        print(f"    total_hours={hours:>12}（配置里写的值）")
        try:
            s = lf.status()
            print(f"      total={s.total_hours} stage={s.stage.name} ratio={s.remaining_ratio:.4f} "
                  f"remaining={s.remaining_hours:.1f} expired={s.expired} "
                  f"warn={lf.is_terminal_warning()}")
            print(f"      brief={lf.brief()!r}")
            print(f"      human_total={s.human_total()!r} human_remaining={s.human_remaining()!r}")
            if hours <= 0:
                check(
                    f"P2-6 total_hours={hours} 不得被静默接受成'立刻到期'",
                    not s.expired,
                    f"实际：expired={s.expired}，进入主循环第一条消息就触发回收换代",
                )
                if s.expired:
                    failures += 1
        except Exception as exc:
            print(f"      status() 抛异常 {type(exc).__name__}: {exc}")
            check(
                f"P2-6 total_hours={hours} 不得在 /status、/prompt 上直接崩",
                False,
                f"{type(exc).__name__}: {exc}（lifespan.py:124 expires = activated + timedelta(hours=total)）",
            )
            failures += 1

    title("5c. 缺少 [[degradation.stages]] 时的兜底：fail-open 还是 fail-loud？")
    import os

    from giftia import config as config_mod

    cfg_dir = tmp / "cfg"
    os.makedirs(cfg_dir, exist_ok=True)
    (cfg_dir / "settings.toml").write_text(
        '[model]\nprovider = "mock"\n[lifespan]\ntotal_hours = 100\n', encoding="utf-8"
    )
    (cfg_dir / "persona.toml").write_text('[persona]\nname = "测试"\n', encoding="utf-8")
    # 期望（修复后）：缺少 [[degradation.stages]] 必须 **fail-loud**（启动即报错），
    # 而不是静默退化成"永不模糊"。原断言记录的是 fail-open 的缺陷，现在翻转成
    # "必须抛配置错误 + 错误信息说得清后果"（契约见 config._validated_stages）。
    loud = ""
    try:
        st_no_stages = config_mod.load_settings(cfg_dir / "settings.toml", cfg_dir / "persona.toml")
        print(f"    load_settings 竟然成功了：stages={st_no_stages.stages}")
    except Exception as exc:  # noqa: BLE001
        loud = f"{type(exc).__name__}: {exc}"
    print(f"    load_settings 的结果：{loud or '（没有报错——这是 fail-open，不合格）'}")
    check(
        "P2-7 degradation 配置缺失时 fail-loud（报错并说明后果），不再静默退化成'永不模糊'",
        bool(loud) and "degradation" in loud and ("模糊" in loud or "blur" in loud),
        loud or "没有报错",
    )
    # 对照：补上一档合法 stage 之后必须能正常加载（证明拦的是"缺配置"而不是"这个文件不能用"）
    (cfg_dir / "settings.toml").write_text(
        '[model]\nprovider = "mock"\n[lifespan]\ntotal_hours = 100\n'
        '[[degradation.stages]]\nname = "clear"\nlabel = "清晰"\n'
        'min_ratio = 0.0\ntop_k = 6\nmin_score = 0.15\nblur = 0.0\nmax_chars = 400\n',
        encoding="utf-8",
    )
    st_ok = config_mod.load_settings(cfg_dir / "settings.toml", cfg_dir / "persona.toml")
    check("对照：补上合法 stage 后照常加载", len(st_ok.stages) == 1,
          f"stages={[s.name for s in st_ok.stages]}")

    print()
    print(f"整理/时间探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
