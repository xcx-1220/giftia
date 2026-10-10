"""Stream D 探针 1：outreach 主动联系 —— 理由优先级、额度、共享额度、target=both。

    python .review/run_tests.py .review/probes/beh_outreach.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/outreach-*。
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import (  # noqa: E402
    base_settings, check, note, report,
)

from giftia import brain as brain_mod  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.config import Settings, load_settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402
from giftia.outreach import _log, can_send, quota_state, record_sent  # noqa: E402


def with_outreach(name: str, hours: float = 81920.0, **kw) -> Settings:
    """独立目录 + 覆盖 [outreach] 的一组设置。"""
    s = base_settings(name, hours)
    return dataclasses.replace(s, outreach=dataclasses.replace(s.outreach, **kw))


def show_mood(brain: Giftia, tag: str) -> None:
    m = brain.current_mood()
    until = m.until or "(空)"
    now = brain.clock.now().isoformat(timespec="seconds")
    expired = (not until) or until < now
    print(f"    {tag} 心情 key={m.key!r} label={m.label} source={m.source} "
          f"since={m.since} until={until} now={now} 已过期={expired}")


def main() -> int:
    print("=" * 78)
    print("A. 复现 selftest 第 24 节 3 项 FAIL（shipped config/settings.toml）")
    print("=" * 78)
    shipped = load_settings(force_mock=True)
    print(f"    config/settings.toml → [outreach] enabled={shipped.outreach.enabled} "
          f"idle_days={shipped.outreach.idle_days} max_per_30_days={shipped.outreach.max_per_30_days} "
          f"max_per_day={shipped.outreach.max_per_day} min_gap_hours={shipped.outreach.min_gap_hours} "
          f"target={shipped.outreach.target!r}")
    print(f"    config.py 代码默认 [outreach] enabled="
          f"{dataclasses.fields(shipped.outreach)[0].default}（被 settings.toml 覆盖）")
    print(f"    [mood.weights]={dict(shipped.mood.weights)} duration_hours={shipped.mood.duration_hours}")

    base = base_settings("outreach-a", 81920.0)   # 与 selftest 第 24 节完全同构
    out = Giftia(base, llm=MockLLM(base.model))
    out.handle("在吗")
    show_mood(out, "handle(在吗) 之后")
    p0 = out.outreach_plan()
    check("A1 复现《默认关闭时她不会主动找你》(selftest:791)",
          p0 is None, f"实际 outreach_plan() -> {p0.reason if p0 else None}")
    print(f"      → 根因：selftest 以为「出厂默认关闭」，但 config/settings.toml 里 "
          f"enabled={base.outreach.enabled}，而 mood_share 给了她一个理由；"
          f"enabled=False 并没有失守")

    # 直接验证 enabled=False 时 plan() 的开关是好的（反驳「enabled 失守」）
    off = dataclasses.replace(base, outreach=dataclasses.replace(base.outreach, enabled=False))
    off_brain = Giftia(off, llm=MockLLM(off.model))     # 必须用新设置构造，不能复用 out
    off_plan = off_brain.outreach_plan()                 # force=False
    off_forced = off_brain.outreach_plan(force=True)     # force=True
    print(f"    enabled=False 的新 brain（同一个库） + force=False → {off_plan}（应为 None）")
    print(f"    enabled=False + force=True  → {off_forced.reason if off_forced else None}"
          f"（/outreach 预览用的就是这条）")
    check("A1b enabled 开关本身没有失守：force=False 时返回 None，force=True 才给预览",
          off_plan is None and off_forced is not None)

    on = dataclasses.replace(base, outreach=dataclasses.replace(
        base.outreach, enabled=True, idle_days=3))
    out2 = Giftia(on, llm=MockLLM(on.model))
    out2.handle("在吗")
    p1 = out2.outreach_plan(force=True)
    check("A2 复现《刚说过话时没有主动理由》(selftest:797)",
          p1 is None, f"实际 reason={p1.reason if p1 else None} title={p1.title if p1 else ''}")

    out2.clock.advance(24 * 4)
    show_mood(out2, "闲 4 天之后")
    p2 = out2.outreach_plan(force=True)
    check("A3 复现《闲了 4 天 → 她想你了》(selftest:801)",
          p2 is not None and p2.reason == "missing_you",
          f"实际 reason={p2.reason if p2 else None}（剩余寿命 81920h，不会是 near_end）")

    out2.force_mood("calm")
    p3 = out2.outreach_plan(force=True)
    check("A4 心情=calm 时，闲 4 天确实返回 missing_you（缺的只是「心情不是 calm」）",
          p3 is not None and p3.reason == "missing_you",
          f"实际 reason={p3.reason if p3 else None}")

    print("\n    —— 这三项测试的绿/红与「当天日期」绑定（掷骰种子 = crc32(日期|实例号)）——")
    mt = out2.mood
    calm_days, other_days = [], []
    d0 = datetime(2026, 10, 10)
    for i in range(40):
        day = (d0 + timedelta(days=i)).date()
        key = mt._roll(datetime.combine(day, time(12, 0)), 1)
        (calm_days if key == "calm" else other_days).append(f"{day.isoformat()}:{key}")
    print(f"    2026-10-10 起 40 天：calm {len(calm_days)} 天 / 非 calm {len(other_days)} 天")
    print(f"    今天(2026-10-10) 实例#1 掷骰 = {mt._roll(datetime(2026, 10, 10, 12, 0), 1)!r}"
          f"（= 今天的 selftest 为何是红的）")
    print(f"    非 calm 的日子样例：{other_days[:5]}")
    check("A5 同一份 selftest 在 calm 的日子会全绿、非 calm 的日子红 3 项（契约分叉 + 日期相关）",
          len(calm_days) > 0 and len(other_days) > 0,
          f"calm={len(calm_days)} 非calm={len(other_days)}")

    print()
    print("=" * 78)
    print("B. 过期的心情仍被当作「她今天心情X」拿去主动分享")
    print("=" * 78)
    sb = with_outreach("outreach-b", enabled=True, idle_days=3)
    stale = Giftia(sb, llm=MockLLM(sb.model))
    stale.handle("在吗")
    stale.force_mood("low")          # 断言式：不依赖当天掷骰
    show_mood(stale, "force_mood(low) 之后")
    stale.clock.advance(24 * 5)
    show_mood(stale, "5 天之后")
    m = stale.current_mood()
    now_iso = stale.clock.now().isoformat(timespec="seconds")
    check("B1 心情早已过期（duration_hours=10，now 超过 until 120 小时）",
          bool(m.until) and m.until < now_iso, f"until={m.until} now={now_iso}")
    pb = stale.outreach_plan(force=True)
    check("B2 过期的心情依然触发 mood_share，标题仍是「她今天心情…」",
          pb is not None and pb.reason == "mood_share" and "今天" in pb.title,
          f"reason={pb.reason if pb else None} title={pb.title if pb else ''}")

    print()
    print("=" * 78)
    print("C. mood_share 吃额度：4 条/30 天的额度花光后，missing_you 永远轮不到")
    print("=" * 78)
    cy = with_outreach("outreach-c", 2000.0, enabled=True, idle_days=3,
                       max_per_30_days=4, max_per_day=3, min_gap_hours=6, target="owner")
    c = Giftia(cy, llm=MockLLM(cy.model))
    c.handle("在吗")
    c.force_mood("low")              # 非 calm 的心情（出厂权重下约 50% 的日子会掷到）
    ctx = c.settings.outreach
    print(f"    额度 {ctx.max_per_30_days} 条/30 天，日上限 {ctx.max_per_day}，间隔 {ctx.min_gap_hours}h")
    reasons: list[str] = []
    for day in range(1, 9):
        plan_ = c.outreach_plan(force=True)
        r = plan_.reason if plan_ else None
        ok = can_send(c.settings, c.store, c.clock.now())
        sent = False
        if ok and r:
            _, text = c.compose_outreach(record=True, force=True)
            sent = bool(text.strip())
        used, left, next_ok = quota_state(c.settings, c.store, c.clock.now())
        print(f"    第 {day} 天 idle={day}天 reason={r:<11} can_send={ok} 真发={sent} "
              f"已用={used}/{ctx.max_per_30_days} 下次={next_ok[:16]}")
        if r:
            reasons.append(r)
        c.clock.advance(24)
    used, left, _ = quota_state(c.settings, c.store, c.clock.now())
    check("C1 mood_share 与 missing_you/dream/milestone 共用同一份 30 天额度",
          reasons.count("mood_share") >= 1 and used >= 1,
          f"8 天理由序列={reasons} used={used}")

    c.clock.advance(24 * 15)          # 再闲 15 天（总共 23 天没说话）
    print("\n    再闲 15 天（共 23 天没说话）后：")
    print(f"      last_message_ts={c.store.last_message_ts(c.instance_no)!r}")
    plan_late = c.outreach_plan(force=True)
    ok_late = can_send(c.settings, c.store, c.clock.now())
    used_l, left_l, nxt = quota_state(c.settings, c.store, c.clock.now())
    print(f"      outreach_plan -> reason={plan_late.reason if plan_late else None} "
          f"can_send={ok_late} used={used_l} left={left_l}")
    check("C2 额度被心情分享花光后，即使真的「太久没见」也一条都发不出去",
          plan_late is not None and plan_late.reason == "mood_share" and not ok_late,
          f"reason={plan_late.reason if plan_late else None} can_send={ok_late} left={left_l}")

    # 出厂配置（settings.toml: max_per_30_days=60, max_per_day=3）下连跑 30 天
    c3 = Giftia(with_outreach("outreach-c3", 4000.0, enabled=True, idle_days=2,
                              max_per_30_days=60, max_per_day=3, min_gap_hours=3),
                llm=MockLLM(cy.model))
    c3.handle("在吗", "handler")
    c3.force_mood("tired")
    seq: list[str] = []
    sent_n = 0
    for _ in range(30):
        p = c3.outreach_plan(force=True)
        r = p.reason if p else None
        seq.append(r or "-")
        if r and can_send(c3.settings, c3.store, c3.clock.now()):
            _, t = c3.compose_outreach(record=True, force=True)
            sent_n += bool(t.strip())
        c3.clock.advance(24)
    used3, left3, _ = quota_state(c3.settings, c3.store, c3.clock.now())
    print(f"\n    出厂额度 60/30 天、日上限 3：30 天里理由序列 = "
          f"{ {r: seq.count(r) for r in set(seq)} }，真发出 {sent_n} 条，used={used3} left={left3}")
    check("C3 出厂配置下心情分享会吃掉一半的 30 天额度（30 条全是 mood_share）",
          sent_n == 30 and used3 >= 29, f"发出 {sent_n}，used={used3}")

    print()
    print("=" * 78)
    print("D. target=both：README 说「两边都发、每次占 2 条额度」")
    print("=" * 78)
    both_s = with_outreach("outreach-d", enabled=True, idle_days=3, target="both")
    dbrain = Giftia(both_s, llm=MockLLM(both_s.model))
    dbrain.handle("在吗")
    dbrain.set_handler_id("OWNER-1")
    dbrain.set_last_group("GROUP-1")
    dbrain.clock.advance(24 * 5)      # 闲 5 天
    tomorrow = (dbrain.clock.now().date() + timedelta(days=1)).isoformat()
    # 群里公开说过的一件大事（群里的人也看得见）—— 保证「群」这个目标确实有理由可发
    dbrain.store.add_memory(
        instance_no=dbrain.instance_no,
        created_ts=dbrain.clock.iso(), happened_ts=dbrain.clock.iso(),
        summary="阿伟明天要考驾照", scope="group", subject_id="GROUP-1",
        speaker_id="U-1", event_date=tomorrow, importance=0.7,
    )
    brain_mod._default = dbrain       # 适配器内部用进程内单例

    from adapters.qq_official import QQBot  # noqa: E402

    bot = QQBot("app", "secret", owner_openid="OWNER-1", settings=both_s, mock_llm=True)
    calls: list[dict] = []

    def fake_request(method, path, payload=None, *, auth=True):  # 不发真请求
        calls.append({"method": method, "path": path, "payload": dict(payload or {})})
        return {}

    bot._request = fake_request  # type: ignore[assignment]
    print(f"    adapter._outreach_targets() = {bot._outreach_targets()}")
    plan_c2c = dbrain.outreach_plan(force=True, for_group=False)
    plan_grp = dbrain.outreach_plan(force=True, for_group=True)
    print(f"    私聊可用理由={plan_c2c.reason if plan_c2c else None}　"
          f"群里可用理由={plan_grp.reason if plan_grp else None}")
    bot._maybe_outreach()
    used_b, left_b, _ = quota_state(both_s, dbrain.store, dbrain.clock.now())
    print(f"    实际发出的请求：{[c['path'] for c in calls]}")
    print(f"    实际扣额度：used={used_b} left={left_b}（README：both 每次占 2 条）")
    check("D1 target=both 时私聊发得出去", any("users" in c["path"] for c in calls), str(calls))
    check("D2 target=both 时群里也发得出去（README/配置承诺『两边都发』）",
          any("groups" in c["path"] for c in calls),
          f"只发出 {len(calls)} 条：{[c['path'] for c in calls]}；"
          f"而群里明明有可发理由 big_day（阿伟明天要考驾照）")
    check("D3 target=both 每次占 2 条额度（README 承诺）", used_b == 2, f"实际 used={used_b}")
    check("D4 群里那条被丢掉的原因：第一条发完立刻记录，第二次 can_send 被 min_gap 挡回",
          not can_send(both_s, dbrain.store, dbrain.clock.now()),
          f"min_gap_hours={both_s.outreach.min_gap_hours}，"
          f"next_ok={quota_state(both_s, dbrain.store, dbrain.clock.now())[2]}")

    print("\n    —— use_wakeup=true 时额度并没有收紧（官方：30 天 4 条）——")
    wk = with_outreach("outreach-d2", enabled=True, idle_days=2, target="owner",
                       use_wakeup=True)   # max_per_30_days 保持 shipped 的 60
    wbrain = Giftia(wk, llm=MockLLM(wk.model))
    wnow = datetime(2026, 10, 10, 9, 0)
    for i in range(10):
        record_sent(wbrain.store, wnow + timedelta(days=i))
    wused, wleft, _ = quota_state(wk, wbrain.store, wnow + timedelta(days=10))
    print(f"    use_wakeup={wk.outreach.use_wakeup} max_per_30_days={wk.outreach.max_per_30_days} "
          f"→ 30 天内已发 {wused} 条，还剩 {wleft}，can_send="
          f"{can_send(wk, wbrain.store, wnow + timedelta(days=10, hours=3))}")
    check("D5 use_wakeup=true 时官方 4 条/30 天的额度没有被本地账本约束（会超发）",
          can_send(wk, wbrain.store, wnow + timedelta(days=10, hours=3)) and wused > 4,
          f"已发 {wused} 条仍 can_send=True")

    print()
    print("=" * 78)
    print("E. 账本截断 / 时钟回拨 / record=True 提前扣额度")
    print("=" * 78)
    ls = with_outreach("outreach-e", max_per_30_days=500)
    lstore = Giftia(ls, llm=MockLLM(ls.model)).store
    now = datetime(2026, 10, 10, 12, 0)
    for i in range(460):
        record_sent(lstore, now - timedelta(hours=i))
    used_e, left_e, _ = quota_state(ls, lstore, now)
    print(f"    record_sent 460 条（30 天内），max_per_30_days=500 → used={used_e} left={left_e} "
          f"账本实际长度={len(_log(lstore))}")
    check("E1 账本 keep=400 截断后 30 天用量永远算不到 500（额度静默失效）",
          left_e > 0, f"used={used_e} left={left_e}（按语义应为 0 条可用）")

    fs = base_settings("outreach-f", 81920.0)
    fbrain = Giftia(fs, llm=MockLLM(fs.model))
    fbrain.clock.advance(24 * 4)
    record_sent(fbrain.store, fbrain.clock.now())
    print(f"\n    记录一条主动消息后：now={fbrain.clock.now().isoformat(timespec='seconds')} "
          f"can_send={can_send(fbrain.settings, fbrain.store, fbrain.clock.now())}")
    fbrain.clock.advance(-100)        # 相当于 /time -100
    used_f, left_f, next_f = quota_state(fbrain.settings, fbrain.store, fbrain.clock.now())
    print(f"    时钟回拨 100 小时后：now={fbrain.clock.now().isoformat(timespec='seconds')} "
          f"used={used_f} next_ok={next_f}")
    check("E2 时钟回拨后未来时间戳仍计入 used 并挡住发送",
          used_f == 1 and not can_send(fbrain.settings, fbrain.store, fbrain.clock.now()),
          f"used={used_f} can_send={can_send(fbrain.settings, fbrain.store, fbrain.clock.now())}")

    gs = with_outreach("outreach-g", enabled=True, idle_days=3, target="owner")
    g = Giftia(gs, llm=MockLLM(gs.model))
    g.handle("在吗")
    g.clock.advance(24 * 5)
    _, text = g.compose_outreach(record=True, force=True)   # 只生成，不发送
    used_g, left_g, _ = quota_state(gs, g.store, g.clock.now())
    print(f"\n    compose_outreach(record=True) 只生成文本、没有任何发送 → used={used_g} left={left_g}")
    check("E3 record=True 在未发送时就扣额度（适配器走 record=False，语义不一致）",
          used_g == 1, f"used={used_g}")

    print()
    print("=" * 78)
    print("F. 发送失败会不会扣额度（适配器：成功后才 note_outreach_sent）")
    print("=" * 78)
    fs2 = with_outreach("outreach-h", enabled=True, idle_days=1, target="owner")
    fbrain2 = Giftia(fs2, llm=MockLLM(fs2.model))
    fbrain2.set_handler_id("OWNER-1")
    fbrain2.handle("在吗", "OWNER-1")
    fbrain2.force_mood("calm")
    fbrain2.clock.advance(24 * 2)
    brain_mod._default = fbrain2
    bot2 = QQBot("app", "secret", owner_openid="OWNER-1", settings=fs2, mock_llm=True)
    from adapters.qq_official import QQError  # noqa: E402

    def failing_request(method, path, payload=None, *, auth=True):
        raise QQError("HTTP 500：模拟平台发送失败")

    bot2._request = failing_request  # type: ignore[assignment]
    used_before = quota_state(fs2, fbrain2.store, fbrain2.clock.now())[0]
    out = []
    import contextlib as _ctx, io as _io
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf), _ctx.redirect_stderr(buf):
        bot2._maybe_outreach()
    used_after = quota_state(fs2, fbrain2.store, fbrain2.clock.now())[0]
    print(f"    发送抛 QQError → 已用额度 {used_before} → {used_after}；输出：{buf.getvalue().strip()[:60]}")
    check("F1 发送失败不扣额度（不会白白烧掉稀缺的主动额度）", used_after == used_before,
          f"{used_before} → {used_after}")

    return report("beh_outreach")


if __name__ == "__main__":
    raise SystemExit(main())
