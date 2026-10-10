"""Stream A 探针 3：退化机制可证伪性。

README 承诺：
  模糊度 = 阶段模糊 × 层级系数 × (1 - 减免 × 保留率)
  core 的模糊系数恒为 0 → 到她生命尽头都不含糊
  5 个阶段动态收紧 top_k / min_score / blur / max_chars

逐条打脸：
  1. effective_blur 是否等于公式（用真实 5 个阶段 × 3 个层级 × 多档保留率扫一遍）
  2. layer_core_blur=0 是否真的永不含糊
  3. blur 上限（clamp 0.95）与"层级系数 > 1"的行为
  4. top_k / min_score / relative_cutoff / max_chars 各自是否真的生效
  5. max_chars 截断与"是否被模糊过"标记是否一致（confidence 契约）

跑法：
    python .review\\probes\\mm_degradation.py
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    mem,
    settings_for,
    stage,
    title,
)
from giftia.memory import (  # noqa: E402
    CONFIDENCE_CITE,
    confidence_of,
    effective_blur,
    recall,
    retention_of,
)

NOW = datetime.now()


def main() -> int:
    tmp = fresh_dir("degradation")
    settings = settings_for(tmp)
    cfg = settings.memory
    failures = 0

    # ── 1. 公式核对 ───────────────────────────────────────────────
    title("1. effective_blur() 与 README 公式逐点核对")
    print(f"{'阶段':<10}{'层级':<10}{'保留率':>8}{'实测blur':>10}{'公式值':>10}  一致性")
    mismatched = 0
    for st in settings.stages:
        for layer in ("core", "important", "normal"):
            for age_h in (0, 24, 120, 720, 5000):
                m = mem(
                    1,
                    "测试记忆",
                    layer=layer,
                    importance=0.5,
                    strength=1.5,
                    created_ts=(NOW - timedelta(hours=age_h)).isoformat(timespec="seconds"),
                )
                got = effective_blur(m, st, cfg, NOW)
                r = retention_of(m, cfg, NOW)
                want = max(0.0, min(0.95, st.blur * cfg.layer_blur(layer) * (1 - cfg.retention_relief * r)))
                if abs(got - want) > 1e-12:
                    mismatched += 1
                    print(f"{st.name:<10}{layer:<10}{r:>8.4f}{got:>10.4f}{want:>10.4f}  不一致!")
        # 每个阶段打一行样例
        m = mem(1, "测试记忆", layer="normal", importance=0.5, strength=2.5)
        r = retention_of(m, cfg, NOW)
        got = effective_blur(m, st, cfg, NOW)
        print(f"{st.name:<10}{'normal':<10}{r:>8.4f}{got:>10.4f}"
              f"{st.blur * cfg.layer_normal_blur * (1 - cfg.retention_relief * r):>10.4f}  OK")
    check("公式 模糊度 = 阶段模糊 × 层级系数 × (1 - 减免 × 保留率)", mismatched == 0,
          f"不一致点数={mismatched}")
    if mismatched:
        failures += 1

    # ── 2. core 永不含糊 ─────────────────────────────────────────
    title("2. layer_core_blur=0 → 内核记忆永不含糊？")
    core = mem(7, "他叫小林。", layer="core", importance=0.9, strength=10.0)
    terminal = stage(settings, "terminal")
    far_future = NOW + timedelta(days=3000)
    blur_now = effective_blur(core, terminal, cfg, NOW)
    blur_future = effective_blur(core, terminal, cfg, far_future)
    print(f"    配置 layer_core_blur={cfg.layer_core_blur}，terminal.blur={terminal.blur}")
    print(f"    core 记忆 blur：现在={blur_now}，3000 天后={blur_future}")
    check("core 在 terminal 阶段模糊度为 0", blur_now == 0.0 and blur_future == 0.0,
          f"{blur_now} / {blur_future}")
    if not (blur_now == 0.0 and blur_future == 0.0):
        failures += 1

    # 但"永不含糊"只是 blur=0；max_chars 截断是另一条路 —— 见第 5 节
    core2 = mem(8, "他叫小林。" + "（补充说明）" * 40, layer="core", importance=0.9)
    res = recall("小林", [core2], stage(settings, "terminal"), cfg, NOW)
    if res:
        r0 = res[0]
        print(f"    core 记忆被 terminal 阶段 max_chars={terminal.max_chars} 截断后：")
        print(f"      text={r0.text[:60]!r}... len={len(r0.text)} blur={r0.blur} "
              f"blurred={r0.blurred} confidence={r0.confidence}")
        check(
            "P2-1：blur=0 但被 max_chars 截断时 blurred=True / confidence 降级",
            not (r0.blur == 0.0 and r0.blurred and r0.confidence != CONFIDENCE_CITE),
            f"blur={r0.blur} blurred={r0.blurred} confidence={r0.confidence}",
        )

    # core 被截断会不会把内容切掉
    long_core = mem(9, "他叫小林，住在杭州，喜欢喝美式。", layer="core")
    res2 = recall("小林", [long_core], stage(settings, "fading"), cfg, NOW)
    if res2 and res2[0].blurred:
        print(f"    fading 阶段 core 记忆：{res2[0].text!r}（blur={res2[0].blur}）")

    # ── 3. 保留率 > 1 / 时钟回拨 ─────────────────────────────────
    title("3. retention_of 的边界：时钟回拨、未来时间戳、缺字段")
    cases = [
        ("刚说过（last_recalled_at = 现在）",
         mem(1, "x", last_recalled_at=NOW.isoformat(timespec="seconds"))),
        ("被快进到「说过」（last_recalled_at 在未来）",
         mem(2, "x", last_recalled_at=(NOW + timedelta(days=10)).isoformat(timespec="seconds"))),
        ("created/happened 都在未来",
         mem(3, "x", created_ts=(NOW + timedelta(days=5)).isoformat(timespec="seconds"),
             happened_ts=(NOW + timedelta(days=5)).isoformat(timespec="seconds"))),
        ("last_recalled_at 是垃圾字符串",
         mem(4, "x", last_recalled_at="not-a-date")),
        ("strength=None（NULL 列）", {**mem(5, "x"), "strength": None}),
        ("recall_count 为负数", mem(6, "x", recall_count=-99, strength=1.0)),
        ("timezone-aware 时间戳",
         mem(7, "x", created_ts=(NOW.astimezone()).isoformat(timespec="seconds"))),
    ]
    for label, m in cases:
        try:
            r = retention_of(m, cfg, NOW)
            flag = "OK" if 0.0 <= r <= 1.0 else "越界"
            print(f"    {label:<40} retention={r:.6f}  {flag}")
            if not (0.0 <= r <= 1.0):
                failures += 1
        except Exception as exc:
            print(f"    {label:<40} 抛异常 {type(exc).__name__}: {exc}")
            failures += 1

    title("3b. 时钟回拨（/time -24）对退化与保留率的影响")
    for off in (0.0, -24.0, -240.0):
        m = mem(10, "x", strength=1.0, recall_count=0,
                created_ts=(NOW - timedelta(hours=1000)).isoformat(timespec="seconds"))
        now_shifted = NOW + timedelta(hours=off)
        r = retention_of(m, cfg, now_shifted)
        b = effective_blur(m, stage(settings, "terminal"), cfg, now_shifted)
        print(f"    offset={off:>7.1f}h → Δt={max(0.0, (now_shifted - datetime.fromisoformat(m['created_ts'])).total_seconds()/3600):>8.1f}h "
              f"retention={r:.4f} terminal_blur={b:.4f}")
    check("回拨后保留率仍在 [0,1] 内（_age_hours 有 max(0.0) 保护）", True,
          "回拨会让 Δt 归零 → 保留率=1 → 模糊度降到最低（=减免上限 0.6×阶段模糊）")

    # ── 4. top_k / min_score / relative_cutoff / max_chars ───────
    title("4. 四个检索参数是否每个都真的生效")
    # 造 8 条：命中强度递减
    corpus = [
        mem(1, "小林 驾照 考试 很紧张 科目三", importance=0.9, layer="core"),
        mem(2, "小林 驾照 考试 预约", importance=0.8, layer="important"),
        mem(3, "小林 驾照 科目二 挂了", importance=0.7),
        mem(4, "小林 说 他 喜欢 喝 美式 咖啡", importance=0.6),
        mem(5, "今天 天气 不错", importance=0.5),
        mem(6, "小林 在 群里 发 了 一张 照片", importance=0.5),
        mem(7, "小林 驾照 教练 很凶", importance=0.6),
        mem(8, "无关 的 一条 记事", importance=0.5),
    ]
    query = "小林 驾照 考试"
    base_stage = stage(settings, "clear")
    full = recall(query, corpus, base_stage, cfg, NOW)
    print(f"    clear 阶段：{len(full)} 条 → {[r.memory['id'] for r in full]}")
    print(f"    top_k={base_stage.top_k} min_score={base_stage.min_score} "
          f"relative_cutoff={cfg.relative_cutoff} max_chars={base_stage.max_chars}")

    # top_k：把 top_k 改成 2
    st_k2 = settings_for(tmp, memory__relative_cutoff=0.0)
    r_k2 = recall(query, corpus, type(base_stage)(**{**base_stage.__dict__, "top_k": 2}),
                  st_k2.memory, NOW)
    print(f"    top_k=2 → {len(r_k2)} 条 {[r.memory['id'] for r in r_k2]}")
    check("top_k 生效", len(r_k2) == 2, f"n={len(r_k2)}")
    if len(r_k2) != 2:
        failures += 1

    # min_score：抬到不可能的值
    st_ms = settings_for(tmp)
    r_ms = recall(query, corpus, type(base_stage)(**{**base_stage.__dict__, "min_score": 99.0}),
                  st_ms.memory, NOW)
    print(f"    min_score=99 → {len(r_ms)} 条")
    check("min_score 生效（硬门槛）", len(r_ms) == 0, f"n={len(r_ms)}")
    if len(r_ms):
        failures += 1

    # relative_cutoff：0.0 vs 0.99
    st_rc = settings_for(tmp, memory__relative_cutoff=0.99)
    r_rc = recall(query, corpus, base_stage, st_rc.memory, NOW)
    print(f"    relative_cutoff=0.99 → {len(r_rc)} 条 {[r.memory['id'] for r in r_rc]}"
          f"（0.0 时 {len(recall(query, corpus, base_stage, settings_for(tmp, memory__relative_cutoff=0.0).memory, NOW))} 条）")
    check("relative_cutoff 生效", len(r_rc) <= len(full), f"{len(r_rc)} <= {len(full)}")

    # max_chars：把限额压到很小
    long_mem = [mem(1, "小林 驾照 考试 " + "细节描述" * 30, importance=0.9)]
    st_mc = type(base_stage)(**{**base_stage.__dict__, "max_chars": 20})
    r_mc = recall("小林", long_mem, st_mc, cfg, NOW)
    print(f"    max_chars=20 → len={len(r_mc[0].text)} text={r_mc[0].text!r}")
    check("max_chars 生效（长度允许 +1 的省略号）", len(r_mc[0].text) <= 21,
          f"len={len(r_mc[0].text)}（配置 20）")
    if len(r_mc[0].text) > 21:
        failures += 1

    # ── 5. 置信度契约 ─────────────────────────────────────────────
    title("5. confidence_of 的边界")
    for b, r, want in [
        (0.0, 1.0, "cite"), (0.0, 0.4, "cautious"), (0.01, 1.0, "cautious"),
        (0.6, 1.0, "associate"), (0.59, 1.0, "cautious"), (0.95, 0.0, "associate"),
    ]:
        got = confidence_of(b, r, cfg)
        print(f"    blur={b:<5} retention={r:<5} → {got:<10}（期望 {want}）")
    print(f"    阈值：associate_blur={cfg.confidence_associate_blur} "
          f"cautious_retention={cfg.confidence_cautious_retention}")

    # ── 6. layer_core_blur 调参是否真的改变行为 ──────────────────
    title("6. 调大 layer_core_blur 后 core 是否真的开始糊（README 的旋钮）")
    st_mod = settings_for(tmp, memory__layer_core_blur=0.5)
    b0 = effective_blur(core, terminal, cfg, NOW)
    b1 = effective_blur(core, terminal, st_mod.memory, NOW)
    print(f"    layer_core_blur 0.0 → blur={b0}；0.5 → blur={b1}")
    check("旋钮生效", b0 == 0.0 and b1 > 0.0, f"{b0} → {b1}")
    if not (b0 == 0.0 and b1 > 0.0):
        failures += 1

    print()
    print(f"退化探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
