r"""Stream C 探针 3：giftia/config.py 的 .env 解析、校验缺失、路径与敏感信息落盘。

只读 .env 里的值用于「有没有泄漏」的比对，**不打印任何密钥内容**。
运行： python .review\run_tests.py .review\probes\pa_config_probe.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from giftia import config as cfg  # noqa: E402
from giftia.clock import Clock  # noqa: E402
from giftia.lifespan import Lifespan  # noqa: E402
from giftia.store import Store  # noqa: E402

TMP = ROOT / ".review" / "tmp"
PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -> {detail}" if detail else ""))


def workdir(name: str) -> Path:
    p = TMP / f"protocol-cfg-{name}"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ══ 1. .env 解析矩阵 ══════════════════════════════════════════════
ENV_CASES = [
    # (标签, 原始行, 期望的 key, 期望的值)
    ("普通 KEY=VALUE", "PA_PLAIN=value", "PA_PLAIN", "value"),
    ("两侧空格", "   PA_SPACED   =   value   ", "PA_SPACED", "value"),
    ("双引号", 'PA_DQ="hello world"', "PA_DQ", "hello world"),
    ("单引号", "PA_SQ='hello world'", "PA_SQ", "hello world"),
    ("值里带 =", "PA_EQ=a=b=c", "PA_EQ", "a=b=c"),
    ("行内 # 注释", "PA_INLINE=value # 这是注释", "PA_INLINE", "value"),
    ("引号后带注释", 'PA_QC="value" # 注释', "PA_QC", "value"),
    ("export 前缀", "export PA_EXPORT=value", "PA_EXPORT", "value"),
    ("BOM 开头的第一行", "\ufeffPA_BOM=value", "PA_BOM", "value"),
    ("CRLF 行尾", "PA_CRLF=value\r", "PA_CRLF", "value"),
    ("空值", "PA_EMPTY=", "PA_EMPTY", ""),
    ("整行注释不生效", "# PA_COMMENT=value", "PA_COMMENT", None),
]


def env_matrix() -> None:
    print("1. .env 解析（逐条构造真实文件）")
    for label, line, key, want in ENV_CASES:
        d = workdir("env")
        path = d / "case.env"
        path.write_bytes((line + "\n").encode("utf-8"))
        os.environ.pop(key, None)
        for k in list(os.environ):
            if k.strip() in (key, "export " + key) or k.endswith(key):
                if k != key:
                    os.environ.pop(k, None)
        cfg.load_env_file(path, override=True)
        got = os.environ.get(key, None)
        ok = (got == want) if want is not None else (key not in os.environ)
        # 顺手找出「本该是这个 key、却写成了别的名字」的脏变量
        stray = [k for k in os.environ if k != key and key in k]
        check(f"{label}：{key} == {want!r}", ok, f"实际 {got!r}" + (f"；脏变量 {stray}" if stray else ""))
        for k in stray:
            os.environ.pop(k, None)


# ══ 2. settings.toml 校验 ════════════════════════════════════════
# §8 契约（2026-10-10）：settings.toml 必须带 [[degradation.stages]]，缺了会 fail-loud
# （否则她会永远不模糊）。所以下面那些"只测某一个字段"的用例要带上最小合法 stage，
# 否则报错会停在"缺 stages"上、盖住真正被测的字段。已经自带 stages 的用例（2a/2f）不动。
STAGE_MIN = """
[[degradation.stages]]
name = "clear"
label = "记忆清晰"
min_ratio = 0.0
top_k = 6
min_score = 0.15
blur = 0.0
max_chars = 400
"""


def toml_case(name: str, body: str) -> Path:
    d = workdir("toml")
    p = d / f"{name}.toml"
    text = body
    if "[[degradation.stages]]" not in text:
        text += "\n" + STAGE_MIN
    p.write_text(text, encoding="utf-8")
    return p


def toml_cases() -> None:
    print("\n2. settings.toml 的字段缺失 / 越界 / 拼错")
    base = cfg.CONFIG_DIR / "settings.toml"

    # 2a 退化阶段缺字段
    p = toml_case("stage_missing", '[[degradation.stages]]\nname = "x"\nlabel = "y"\n')
    try:
        st = cfg.load_settings(settings_path=p)
        print(f"  [INFO] 缺字段的 stage 被接受：{st.stages}")
        check("退化阶段缺字段时给出可读的配置错误", False, "居然没报错")
    except Exception as exc:
        print(f"  [INFO] 缺字段的 stage 抛出：{type(exc).__name__}: {exc}")
        check("退化阶段缺字段时给出可读的配置错误",
              isinstance(exc, SystemExit) or "配置" in str(exc), f"{type(exc).__name__}: {exc}")

    # 2b 越界值
    p = toml_case("tier_neg", "[runtime]\nresponse_tier = -5\nspeak_max_per_day = -1\n")
    try:
        st = cfg.load_settings(settings_path=p)
        print(f"  [INFO] response_tier = -5 被接受，settings 里就是 {st.runtime.response_tier}；"
              f"speak_max_per_day = {st.runtime.speak_max_per_day}")
        check("越界的 response_tier / speak_max_per_day 被拒绝或夹到合法范围",
              st.runtime.response_tier >= 0 and st.runtime.speak_max_per_day >= 0,
              f"tier={st.runtime.response_tier} speak={st.runtime.speak_max_per_day}")
    except Exception as exc:
        check("越界值被拒绝", True, f"{type(exc).__name__}: {exc}")

    # 2c total_hours = 0
    p = toml_case("total_zero", "[lifespan]\ntotal_hours = 0.0\n")
    st = cfg.load_settings(settings_path=p, force_mock=True)
    root = workdir("lifespan")
    st = dataclasses.replace(st, root=root)
    st.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(st.db_path)
    try:
        clock = Clock(store)
        life = Lifespan(st, store, clock)
        life.ensure_activated()
        status = life.status()
        print(f"  [INFO] total_hours=0 → effective_hours={st.lifespan.effective_hours()}，"
              f"出生第一秒 status：expired={status.expired} ratio={status.remaining_ratio} "
              f"stage={status.stage.name}")
        check("total_hours=0 被拦下（不许出生即到期）", not status.expired,
              f"expired={status.expired}")
    finally:
        store.close()

    # 2d 拼错的键
    p = toml_case("typo", "[runtime]\nresponse_tierr = 100\nwake_delay_second = 9\n")
    st = cfg.load_settings(settings_path=p)
    print(f"  [INFO] 拼错的键被静默忽略：response_tier={st.runtime.response_tier}，"
          f"wake_delay_seconds={st.runtime.wake_delay_seconds}")
    check("拼错的配置键会报错（而不是静默不生效）",
          st.runtime.response_tier == 100, "静默忽略")

    # 2e keywords 类型
    # 判据修订（team-store 2026-10-10）：§8 配置校验的契约是"keywords 必须是**字符串列表**"，
    # 写成字符串现在直接报"配置错误…"（fail-loud），而不是悄悄变成逐字匹配。
    # 所以两种结果都算正确：① 抛配置错误；② 读成字符串列表。唯独 `tuple("晚安")`
    # 那种逐字匹配（('晚','安')）必须判失败 —— 检查的意图没变。
    p = toml_case("kw", '[runtime]\nkeywords = "晚安"\n')
    try:
        st = cfg.load_settings(settings_path=p)
        kw = st.runtime.keywords
        print(f"  [INFO] keywords = \"晚安\" → settings 里是 {kw!r}（{type(kw).__name__}），"
              f"适配器 tuple(它) = {tuple(kw)!r}")
        check("keywords 写错类型（字符串）不能悄悄变成逐字匹配",
              isinstance(kw, (list, tuple)) and tuple(kw) != tuple("晚安"),
              f"tuple()={tuple(kw)!r}")
    except Exception as exc:
        print(f"  [INFO] keywords = \"晚安\" 被配置校验拒绝：{type(exc).__name__}: {exc}")
        check("keywords 写错类型（字符串）不能悄悄变成逐字匹配",
              "keywords" in str(exc), f"{type(exc).__name__}: {exc}")

    # 2f 顺序与空 stages
    p = toml_case("stage_order", """
[[degradation.stages]]
name = "low"
label = "低"
min_ratio = 0.1
top_k = 4
min_score = 0.1
blur = 0.5
max_chars = 200

[[degradation.stages]]
name = "high"
label = "高"
min_ratio = 0.9
top_k = 8
min_score = 0.2
blur = 0.0
max_chars = 400
""")
    st = cfg.load_settings(settings_path=p)
    print(f"  [INFO] 故意乱序的 stages → {[s.name for s in st.stages]}（已按 min_ratio 降序）")
    check("乱序的退化阶段被重排", [s.name for s in st.stages] == ["high", "low"],
          str([s.name for s in st.stages]))


# ══ 3. 路径解析 ══════════════════════════════════════════════════
def path_cases() -> None:
    print("\n3. 路径解析（相对项目根还是相对 cwd）")
    before = os.getcwd()
    try:
        os.chdir(r"C:\ ")
    except OSError:
        try:
            os.chdir("C:\\")
        except OSError:
            pass
    st = cfg.load_settings(force_mock=True)
    print(f"  [INFO] cwd={os.getcwd()!r} → ROOT={cfg.ROOT} db_path={st.db_path}")
    check("数据路径跟着项目走，不跟着 cwd",
          str(st.db_path).lower().startswith(r"d:\ai chatbot\data"),
          str(st.db_path))
    check("配置目录也是项目内的绝对路径",
          cfg.CONFIG_DIR.is_absolute() and (cfg.CONFIG_DIR / "settings.toml").exists(),
          str(cfg.CONFIG_DIR))
    os.chdir(before)


# ══ 4. 敏感值会不会落盘 ═════════════════════════════════════════
def secret_cases() -> None:
    print("\n4. .env 里的密钥会不会进 data/ 或 runs.jsonl")
    secrets: list[tuple[str, str]] = []
    env_path = ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and any(t in k.upper() for t in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                secrets.append((k, v))
    print(f"  [INFO] .env 里取到 {len(secrets)} 个凭据字段（{', '.join(k for k, _ in secrets)}），"
          f"长度 {[len(v) for _, v in secrets]}——只用于比对，不打印内容")

    root = workdir("leak")
    st = dataclasses.replace(cfg.load_settings(force_mock=True), root=root)
    st.data_dir.mkdir(parents=True, exist_ok=True)
    from giftia.brain import handle_message, shutdown_default  # noqa: E402
    reply = handle_message("我今天加班到很晚，回来路上下了点雨", "U-LEAK",
                           settings=st, scene="c2c", is_handler=True)
    shutdown_default()

    runs = st.data_dir / "runs.jsonl"
    if runs.exists():
        print(f"  [INFO] runs.jsonl 第一行：{runs.read_text(encoding='utf-8').splitlines()[0]}")
    else:
        print("  [INFO] 没有生成 runs.jsonl")
    check("跑了至少一轮对话（runs.jsonl 有记录）", runs.exists() and runs.stat().st_size > 0)

    hits = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        for name, val in secrets:
            if val.encode("utf-8") in blob:
                hits.append((str(p.relative_to(root)), name))
    print(f"  [INFO] 在 {root.name} 下扫描 {sum(1 for _ in root.rglob('*') if _.is_file())} 个文件，"
          f"命中凭据的文件：{hits}")
    check("落盘文件里不含任何 .env 凭据", not hits, str(hits))
    check("回复本身正常产生（说明上面不是空跑）", bool(reply), repr(reply))


def main() -> int:
    print("Stream C 探针 3：config.py\n")
    env_matrix()
    toml_cases()
    path_cases()
    secret_cases()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
