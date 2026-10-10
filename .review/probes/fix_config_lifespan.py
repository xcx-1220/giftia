"""team-store 修复探针 3：配置与寿命（P1-9 + §8 配置校验 / .env 解析）。

跑法（在 D:\\ai chatbot 下）：
    python .review\\probes\\fix_config_lifespan.py
"""

from __future__ import annotations

import dataclasses
import io
import contextlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia import config as cfg  # noqa: E402
from giftia.clock import Clock  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.lifespan import Lifespan  # noqa: E402
from giftia.store import Store  # noqa: E402

TMP = ROOT / ".review" / "tmp" / "fix-config"
PASS = FAIL = 0

STAGES = """
[[degradation.stages]]
name = "clear"
label = "记忆清晰"
min_ratio = 0.0
top_k = 6
min_score = 0.15
blur = 0.0
max_chars = 400
"""


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def title(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def fresh(tag: str) -> Path:
    d = TMP / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_cfg(tag: str, body: str, *, with_stages: bool = True) -> Path:
    d = fresh(tag)
    p = d / "settings.toml"
    p.write_text(body + (STAGES if with_stages else ""), encoding="utf-8")
    (d / "persona.toml").write_text('name = "测试"\n', encoding="utf-8")
    return p


def load_or_error(tag: str, body: str, *, with_stages: bool = True):
    """返回 (settings, error_text)；两者必有一个为 None。"""
    p = write_cfg(tag, body, with_stages=with_stages)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            st = load_settings(settings_path=p, persona_path=p.parent / "persona.toml")
        return st, None, buf.getvalue()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", buf.getvalue()


def main() -> int:
    # ── 1. 真实配置仍然可用 ──────────────────────────────────────
    title("0. 出厂配置仍然照常加载（改动没有把用户配置搞挂）")
    real = load_settings(force_mock=True)
    check("stages 5 档，按 min_ratio 降序",
          [s.name for s in real.stages] == ["clear", "hazy", "blurred", "fading", "terminal"],
          str([s.name for s in real.stages]))
    check('keywords = ["艾拉", "机器人", "小艾"] 继续可用',
          real.runtime.keywords == ["艾拉", "机器人", "小艾"], str(real.runtime.keywords))
    check("response_tier=45 原样保留", real.runtime.response_tier == 45, str(real.runtime.response_tier))
    check("lifespan 81920 小时原样保留", real.lifespan.total_hours == 81920.0)
    check("新增字段有默认值（min_raw_score=0 / recall_prefilter=2000 / outreach 两项）",
          real.memory.min_raw_score == 0.0 and real.memory.recall_prefilter == 2000
          and real.outreach.mood_share_min_idle_hours == 6.0
          and real.outreach.wakeup_max_per_30_days == 4)

    # ── 2. P1-9 lifespan 配置校验 ────────────────────────────────
    title("1. P1-9 [lifespan] 范围校验：越界报错，0 当成没填并大声提示")
    st, err, warn = load_or_error("life_zero", "[lifespan]\ntotal_hours = 0\n")
    check("total_hours=0 → 当成'没填'，回落到默认 81920（不再'出生即到期'）",
          st is not None and st.lifespan.total_hours == 81920.0 and "total_hours = 0" in warn,
          err or f"total={st.lifespan.total_hours} warn={warn.strip()[:60]!r}")
    st, err, _ = load_or_error("life_neg", "[lifespan]\ntotal_hours = -5\n")
    check("total_hours=-5 → 启动即报错（人话：超出允许范围…回收换代）",
          err is not None and "配置错误" in err and "超出允许范围" in err, err or "居然通过了")
    st, err, _ = load_or_error("life_huge", "[lifespan]\ntotal_hours = 1000000000\n")
    check("total_hours=1e9 → 启动即报错", err is not None and "超出允许范围" in err, err or "居然通过了")
    st, err, _ = load_or_error("life_nan", "[lifespan]\ntotal_hours = nan\n")
    check("total_hours=nan → 报错（不是有限数）", err is not None and "有限" in (err or ""), err or "居然通过了")
    st, err, _ = load_or_error("life_demo_neg", "[lifespan]\ndemo_total_hours = -1\n")
    check("demo_total_hours=-1 → 报错", err is not None and "demo_total_hours" in (err or ""),
          err or "居然通过了")
    st, err, _ = load_or_error("life_warn_neg", "[lifespan]\nterminal_warning_hours = -1\n")
    check("terminal_warning_hours=-1 → 报错", err is not None and "terminal_warning_hours" in (err or ""),
          err or "居然通过了")
    st, err, _ = load_or_error("life_ok", "[lifespan]\ntotal_hours = 48\ndemo_total_hours = 0\n")
    check("正常值（48 小时演示）照常通过", st is not None and st.lifespan.total_hours == 48.0, err or "")

    # ── 3. Lifespan.status() 的边界（库里的坏值） ─────────────────
    title("2. P1-9 Lifespan.status()：库里的坏值不再让 /status、/prompt 崩")
    base = dataclasses.replace(load_settings(force_mock=True), root=None)  # type: ignore[arg-type]
    for hours in (0.0, -1.0, 1e9, 1e300):
        d = fresh(f"status_{hours}")
        st_settings = dataclasses.replace(
            base, root=d, lifespan=dataclasses.replace(base.lifespan, total_hours=hours)
        )
        store = Store(st_settings.db_path)
        life = Lifespan(st_settings, store, Clock(store))
        life.ensure_activated()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                s = life.status()
                brief = life.brief()
                bar = life.progress_bar()
            ok = (not s.expired) and s.remaining_ratio > 0.99
            check(f"total_hours={hours:g}：status() 不崩、不'出生即到期'（brief={brief[:24]!r}）",
                  ok, f"expired={s.expired} ratio={s.remaining_ratio}")
        except Exception as exc:
            check(f"total_hours={hours:g}：status() 不得抛异常", False, f"{type(exc).__name__}: {exc}")
        store.close()

    # 短寿命演示（0.5 小时）不该被"兜底"吃掉
    d = fresh("status_short")
    st_settings = dataclasses.replace(
        base, root=d, lifespan=dataclasses.replace(base.lifespan, total_hours=0.5)
    )
    store = Store(st_settings.db_path)
    clock = Clock(store)
    life = Lifespan(st_settings, store, clock)
    life.ensure_activated()
    s0 = life.status()
    clock.advance(0.75)
    s1 = life.status()
    print(f"    0.5 小时演示：t0 ratio={s0.remaining_ratio:.3f} stage={s0.stage.name} → "
          f"t+45min ratio={s1.remaining_ratio:.3f} stage={s1.stage.name} expired={s1.expired}")
    check("短寿命演示仍然按真实剩余比例推进（没被兜底成 81920）",
          s0.remaining_ratio > 0.99 and s1.expired, f"{s0.remaining_ratio} → {s1.remaining_ratio}")
    store.close()

    # ── 4. degradation.stages fail-loud ─────────────────────────
    title("3. §8 [[degradation.stages]] 缺失/缺字段 = 报错（不再静默'永不模糊'）")
    st, err, _ = load_or_error("stage_missing_all", '[lifespan]\ntotal_hours = 100\n', with_stages=False)
    check("整段缺失 → 报错，并说清后果（她将永远不会变模糊）",
          err is not None and "配置错误" in err and "模糊" in err, (err or "居然通过了")[:120])
    st, err, _ = load_or_error("stage_partial", "[[degradation.stages]]\nname='x'\nlabel='y'\n",
                               with_stages=False)
    check("缺字段 → 报错并列出缺哪些字段",
          err is not None and "缺字段" in err and "top_k" in err, (err or "居然通过了")[:140])
    st, err, _ = load_or_error(
        "stage_bad",
        "[[degradation.stages]]\n" + "".join(
            f"{k} = {v}\n" for k, v in
            (("name", '"x"'), ("label", '"y"'), ("min_ratio", "0.0"), ("top_k", "6"),
             ("min_score", "0.15"), ("blur", "1.5"), ("max_chars", "400"))
        ),
        with_stages=False,
    )
    check("blur=1.5 越界 → 报错", err is not None and "blur" in (err or ""), (err or "居然通过了")[:100])
    p = write_cfg("stage_typo", "[runtime]\nresponse_tierr = 100\nwake_delay_second = 9\n")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        st = load_settings(settings_path=p, persona_path=p.parent / "persona.toml")
    printed = buf.getvalue()
    check("拼错的键打印警告（而不是静默忽略）",
          "response_tierr" in printed and "wake_delay_second" in printed, printed.strip()[:90])
    check("拼错的键确实没生效（tier 保持代码默认 0，而不是 100）",
          st.runtime.response_tier == 0, str(st.runtime.response_tier))

    # ── 5. §8 runtime / memory / 其它数值 ────────────────────────
    title("4. §8 response_tier 夹到 0~100；各类上限不得为负；keywords 必须是字符串列表")
    st, err, warn = load_or_error("tier_neg", "[runtime]\nresponse_tier = -5\n")
    check("response_tier=-5 → 夹到 0 并提示",
          st is not None and st.runtime.response_tier == 0 and "夹到" in warn, err or warn.strip()[:60])
    st, err, warn = load_or_error("tier_huge", "[runtime]\nresponse_tier = 500\n")
    check("response_tier=500 → 夹到 100", st is not None and st.runtime.response_tier == 100,
          err or str(getattr(st, "runtime", None)))
    st, err, _ = load_or_error("speak_neg", "[runtime]\nspeak_max_per_day = -1\n")
    check("speak_max_per_day=-1 → 报错", err is not None and "speak_max_per_day" in (err or ""),
          err or "居然通过了")
    st, err, _ = load_or_error("kw_str", '[runtime]\nkeywords = "晚安"\n')
    check('keywords = "晚安"（字符串）→ 报错并给正确写法',
          err is not None and "字符串列表" in err and "keywords" in err, (err or "居然通过了")[:120])
    st, err, _ = load_or_error("kw_bad_item", "[runtime]\nkeywords = [1, 2]\n")
    check("keywords = [1, 2] → 报错", err is not None and "全" in (err or ""), (err or "居然通过了")[:80])
    st, err, _ = load_or_error("kw_ok", '[runtime]\nkeywords = ["艾拉", "小艾"]\n')
    check('keywords = ["艾拉", "小艾"] → 正常', st is not None and st.runtime.keywords == ["艾拉", "小艾"],
          err or "")
    st, err, _ = load_or_error("every_zero", "[memory]\nconsolidate_every = 0\n")
    check("memory.consolidate_every=0 → 报错（永远不会整理）",
          err is not None and "consolidate_every" in (err or ""), (err or "居然通过了")[:80])
    st, err, _ = load_or_error("min_raw_neg", "[memory]\nmin_raw_score = -1\n")
    check("memory.min_raw_score=-1 → 报错", err is not None and "min_raw_score" in (err or ""),
          (err or "居然通过了")[:80])
    st, err, _ = load_or_error("dream_blur", "[dream]\nblur = 1.5\n")
    check("dream.blur=1.5 → 报错", err is not None and "blur" in (err or ""), (err or "居然通过了")[:80])
    st, err, _ = load_or_error("out_neg", "[outreach]\nmax_per_day = -1\n")
    check("outreach.max_per_day=-1 → 报错", err is not None and "max_per_day" in (err or ""),
          (err or "居然通过了")[:80])
    st, err, _ = load_or_error("temp_bad", "[model]\ntemperature = 3\n")
    check("model.temperature=3 → 报错（API 上限 2）", err is not None and "temperature" in (err or ""),
          (err or "居然通过了")[:80])
    st, err, _ = load_or_error("new_fields", "[outreach]\nwakeup_max_per_30_days = 4\n"
                                            "mood_share_min_idle_hours = 6\n"
                                            "[memory]\nrecall_prefilter = 500\nmin_raw_score = 0.1\n")
    check("新增字段能从 TOML 读进来（波浪 2 的行为修复可直接用）",
          st is not None and st.outreach.wakeup_max_per_30_days == 4
          and st.memory.recall_prefilter == 500 and st.memory.min_raw_score == 0.1, err or "")

    # ── 6. .env 解析 ─────────────────────────────────────────────
    title("5. §8 .env 解析：BOM / export / 行内注释 / 成对引号")
    env_dir = fresh("env")
    cases = [
        ("普通", "TS_PLAIN=value", "TS_PLAIN", "value"),
        ("两侧空格", "   TS_SPACED   =   value   ", "TS_SPACED", "value"),
        ("双引号", 'TS_DQ="hello world"', "TS_DQ", "hello world"),
        ("单引号", "TS_SQ='hello world'", "TS_SQ", "hello world"),
        ("值里带 =", "TS_EQ=a=b=c", "TS_EQ", "a=b=c"),
        ("行内 # 注释", "TS_INLINE=value # 注释", "TS_INLINE", "value"),
        ("引号后带注释", 'TS_QC="value" # 注释', "TS_QC", "value"),
        ("引号里的 # 不算注释", 'TS_HASH="p#ssword"', "TS_HASH", "p#ssword"),
        ("export 前缀", "export TS_EXPORT=value", "TS_EXPORT", "value"),
        ("BOM 开头的第一行", "\ufeffTS_BOM=value", "TS_BOM", "value"),
        ("CRLF 行尾", "TS_CRLF=value\r", "TS_CRLF", "value"),
        ("空值", "TS_EMPTY=", "TS_EMPTY", ""),
        ("引号不成对时原样保留", 'TS_OPEN="value', "TS_OPEN", '"value'),
    ]
    for label, line, key, want in cases:
        p = env_dir / "case.env"
        p.write_text(line + "\n", encoding="utf-8")
        os.environ.pop(key, None)
        cfg.load_env_file(p, override=True)
        got = os.environ.get(key)
        check(f"{label}：{key} == {want!r}", got == want, f"实际 {got!r}")
        os.environ.pop(key, None)
    p = env_dir / "comment.env"
    p.write_text("# TS_SKIP=value\n", encoding="utf-8")
    os.environ.pop("TS_SKIP", None)
    cfg.load_env_file(p, override=True)
    check("整行注释不生效", "TS_SKIP" not in os.environ)
    # 已存在的环境变量不被覆盖 + 返回值只数新键
    p = env_dir / "prio.env"
    p.write_text("TS_KEEP=from_file\nTS_NEW=from_file\n", encoding="utf-8")
    os.environ["TS_KEEP"] = "from_env"
    os.environ.pop("TS_NEW", None)
    loaded = cfg.load_env_file(p)
    check("已存在的环境变量优先，返回值只数新增",
          os.environ["TS_KEEP"] == "from_env" and os.environ.get("TS_NEW") == "from_file" and loaded == 1,
          f"loaded={loaded}")
    for k in ("TS_KEEP", "TS_NEW"):
        os.environ.pop(k, None)

    print()
    print(f"config/lifespan 探针：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
