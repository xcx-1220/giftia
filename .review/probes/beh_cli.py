"""Stream D 探针 7：cli.py / run.py —— 指令参数越界、异常是否崩掉主循环、EOF/Ctrl-C。

    python .review/run_tests.py .review/probes/beh_cli.py

只读产品代码；临时数据在 .review/tmp/behavior-checker/cli-*。
注意：run.py 的 --reset 默认会删 data/ 下的正式库，这里把 run.load_settings / run.DATA_DIR
换成隔离目录再测（不碰真实数据）。
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beh_common import base_settings, check, note, report  # noqa: E402

import run as run_mod  # noqa: E402
from giftia.brain import Giftia  # noqa: E402
from giftia.cli import HELP, run_cli  # noqa: E402
from giftia.config import Settings  # noqa: E402
from giftia.llm import MockLLM  # noqa: E402


class ScriptedInput:
    """替掉 builtins.input：按脚本喂行；喂完就抛 EOFError（模拟 Ctrl-D）。"""

    def __init__(self, lines):
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


class ProbeBrain(Giftia):
    closed = False

    def close(self) -> None:  # 真实的 close 会关掉 sqlite 连接，探针里要接着读库
        self.closed = True


def drive(settings, lines, brain_cls=None):
    """跑一遍 run_cli，返回 (退出码, 输出, 异常, brain)。"""
    brain = (brain_cls or ProbeBrain)(settings, llm=MockLLM(settings.model))
    buf = io.StringIO()
    old_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    import builtins
    builtins.input = ScriptedInput(lines)
    err = None
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            code = run_cli(brain, banner=False)
    except BaseException as exc:  # noqa: BLE001
        err = exc
    finally:
        builtins.input = old_input
    return code, buf.getvalue(), err, brain


def main() -> int:
    print("=" * 78)
    print("A. 各指令 + 越界参数：谁会崩掉主循环")
    print("=" * 78)
    cases = [
        "/help", "/status", "/bond", "/days", "/people", "/profile", "/profile 阿伟",
        "/memories", "/recall", "/recall 驾照", "/prompt", "/prompt 在吗",
        "/mood", "/mood low", "/mood 不存在的名字", "/mood low extra",
        "/name", "/name 小林", "/name " + "长" * 100,
        "/impress", "/impress 阿伟", "/impress 阿伟 他喜欢喝冰美式",
        "/forget", "/forget abc", "/forget -1", "/forget 0", "/forget 999",
        "/forget 99999999999999999999",
        "/dream", "/dream now", "/dream 1", "/dream NOW",
        "/outreach", "/reconcile", "/remember", "/diary", "/diary 今天有点累",
        "/memoir", "/recycle", "/recycle yes", "/time", "/time show",
        "/time +24", "/time -24", "/time reset", "/time abc", "/time 24h",
        "/time +24小时", "/未知指令", "随便说句话",
    ]
    crashed = []
    for i, cmd in enumerate(cases):
        s = base_settings(f"cli-a{i}", 500.0)
        code, out, err, brain = drive(s, [cmd, "/quit"])
        head = " ⏎ ".join(out.strip().splitlines()[:2])[:88]
        if err is not None:
            crashed.append((cmd, type(err).__name__, str(err)))
            print(f"  [CRASH] {cmd!r} → {type(err).__name__}: {err}")
        else:
            print(f"  [ok]    {cmd!r} → {head}")
    check("A1 常见指令 + 越界参数都不会崩掉主循环", not crashed, str(crashed))

    print()
    print("=" * 78)
    print("B. /time 的非有限数值会写坏虚拟时钟（并且当场崩进程）")
    print("=" * 78)
    for bad in ("nan", "inf", "1e400", "1e24", "-inf"):
        s = base_settings("cli-bad-" + bad.replace("-", "m").replace(".", "_"), 500.0)
        b = ProbeBrain(s, llm=MockLLM(s.model))
        from giftia.cli import _handle_command
        err = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                _handle_command(b, f"/time {bad}")
        except BaseException as exc:  # noqa: BLE001
            err = exc
        persisted = b.store.meta_get("virtual_offset_hours")
        print(f"    /time {bad:<6} → {type(err).__name__ if err else '正常'}"
              f"{': ' + str(err) if err else ''}；落库的 offset={persisted!r}")
    s = base_settings("cli-bad-restart", 500.0)
    b = ProbeBrain(s, llm=MockLLM(s.model))
    from giftia.cli import _handle_command
    first = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _handle_command(b, "/time nan")
    except BaseException as exc:
        first = exc
    offset = b.store.meta_get("virtual_offset_hours")
    print(f"\n    /time nan 之后：offset={offset!r}，异常={type(first).__name__ if first else '无'}: {first}")
    # 期望（修复后）：非有限值被**当场拦下**、不写库、不抛异常——时钟仍然可用。
    check("★ /time nan 被拦下，虚拟时钟没有被写坏",
          first is None and offset not in ("nan", "inf", "-inf"),
          f"offset={offset!r} 异常={first!r}")
    b.store.close()
    # 重启：同一个库再构造一次
    err2 = None
    try:
        b2 = Giftia(s, llm=MockLLM(s.model))
        err2 = None
        with contextlib.redirect_stdout(io.StringIO()):
            run_cli(b2, banner=True)     # banner 里就会调 clock.now()
    except BaseException as exc:
        err2 = exc
    print(f"    重启后 run_cli(banner=True) → {type(err2).__name__ if err2 else '正常'}: {err2}")
    print("    【期望为何变了】B1/B2 原来记录的是 P2 的缺陷：")
    print("    · `/time nan|inf` 会把非有限值写进 meta.virtual_offset_hours；")
    print("    · 重启后 `Clock.__init__` 用 float('nan') 读回，`now()` 里")
    print("      `datetime.now()+timedelta(hours=nan)` → ValueError，**连 /time reset 都打不进去**。")
    print("    修复后非有限值被当场拦下（上面的「★ /time nan 被拦下」），")
    print("    落库的 offset 是有限数、重启后 banner 正常打出 → 期望翻转为「不写坏、不崩」。")
    offset_f = None
    try:
        offset_f = float(str(offset or 0) or 0)
    except (TypeError, ValueError):
        offset_f = None
    check("B1 非有限值不会写进 meta.virtual_offset_hours（落库的是有限数）",
          offset_f is not None and offset_f == offset_f and abs(offset_f) != float("inf"),
          f"{offset!r}")
    check("B2 重启后能正常进 CLI（banner 里就会调 clock.now()，不再崩）",
          err2 is None, f"{type(err2).__name__ if err2 else 'None'}")
    # 正向对照：合法的 /time 仍然生效（不是把 /time 关掉）
    s_ok = base_settings("cli-time-ok", 500.0)
    b_ok = ProbeBrain(s_ok, llm=MockLLM(s_ok.model))
    with contextlib.redirect_stdout(io.StringIO()):
        _handle_command(b_ok, "/time +6")
    ok_offset = b_ok.store.meta_get("virtual_offset_hours")
    print(f"    正向对照：/time +6 → 落库 offset={ok_offset!r}")
    check("B1b 正向对照：合法的 /time +6 照常写库（不是把 /time 关掉）",
          str(ok_offset) not in ("", "None") and abs(float(ok_offset) - 6.0) < 1e-9,
          f"{ok_offset!r}")
    b_ok.store.close()

    print()
    print("=" * 78)
    print("C. EOF / Ctrl-C 与 close()")
    print("=" * 78)
    s = base_settings("cli-c", 500.0)
    code, out, err, brain = drive(s, [])
    print(f"    立刻 EOF → 返回 {code}，异常={err}，brain.close() 被调用={brain.closed}")
    check("C1 EOF 正常退出且 close()", code == 0 and err is None and brain.closed)

    class CtrlCBrain(ProbeBrain):
        def banner(self):
            return "banner"

    brain2 = ProbeBrain(s, llm=MockLLM(s.model))
    import builtins
    calls = {"n": 0}

    def ctrl_c(prompt=""):
        calls["n"] += 1
        raise KeyboardInterrupt

    old = builtins.input
    builtins.input = ctrl_c
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code2 = run_cli(brain2, banner=False)
    finally:
        builtins.input = old
    print(f"    Ctrl-C → 返回 {code2}，close()={brain2.closed}")
    check("C2 Ctrl-C 正常退出", code2 == 0 and brain2.closed)

    # 聊天路径的异常被兜住；指令路径没有兜
    class BoomBrain(ProbeBrain):
        def handle(self, *a, **kw):
            raise RuntimeError("模拟内部错误")

    s2 = base_settings("cli-c2", 500.0)
    code3, out3, err3, brain3 = drive(s2, ["在吗", "/status", "/quit"], brain_cls=BoomBrain)
    print(f"    聊天里抛异常 → 异常={err3!r}；输出含「内部错误」={'内部错误' in out3}；"
          f"后面 /status 也打出来了={'实例编号' in out3}")
    check("C3 聊天路径的异常被 run_cli 兜住（不会退出，后面的指令还接着跑）",
          err3 is None and "内部错误" in out3 and "实例编号" in out3)

    # 对照：指令路径现在**也有**兜底（审查 P2 的第二半）
    class BoomCmdBrain(ProbeBrain):
        def status_text(self):
            raise RuntimeError("模拟指令内部错误")

    s3 = base_settings("cli-c3", 500.0)
    code4, out4, err4, brain4 = drive(s3, ["/status", "/quit"], brain_cls=BoomCmdBrain)
    print(f"    指令里抛异常 → 异常={type(err4).__name__ if err4 else None}: {err4}；"
          f"输出含「内部错误」={'内部错误' in out4}；退出码={code4}")
    print("    【期望为何变了】C4 原来记录的是 P2 缺陷：指令路径没有 try/except，")
    print("    `/forget 99999999999999999999` 这类异常会把整个 CLI 进程带走。")
    print("    修复后指令与聊天一样被兜住（cli.py:86-91 打印 [内部错误] 后继续循环），")
    print("    所以期望翻转为「不崩、有提示、后面的指令照跑」。")
    check("C4 指令路径的异常被兜住：不终止 CLI，打印 [内部错误]，后续指令照跑",
          err4 is None and "内部错误" in out4, f"err={err4!r} out={out4[-120:]!r}")

    print()
    print("=" * 78)
    print("D. run.py：--once / --reset --yes / 异常入参（隔离目录，不碰 data/）")
    print("=" * 78)
    real_db = Path(r"D:\ai chatbot\data\giftia.db")
    before = real_db.stat().st_mtime_ns if real_db.exists() else None
    root = base_settings("cli-run", 500.0).root
    isolated = dataclasses.replace(base_settings("cli-run", 500.0), root=root)
    run_mod.load_settings = lambda *a, **kw: isolated      # 只改探针进程里的名字绑定
    run_mod.DATA_DIR = isolated.data_dir
    run_mod.make_llm = lambda settings: MockLLM(settings.model)

    rc = run_mod.main(["--mock", "--once", "在吗"])
    print(f"    --mock --once '在吗' → 退出码 {rc}")
    check("D1 --once 正常返回 0", rc == 0, str(rc))

    # 先攒点数据，再 --reset --yes
    g = Giftia(isolated, llm=MockLLM(isolated.model))
    g.store.add_memory(instance_no=g.instance_no, created_ts=g.clock.iso(),
                       happened_ts=g.clock.iso(), summary="重置前的一条记忆")
    print(f"    --reset 之前：记忆 {g.store.count_memories(g.instance_no, only_active=False)} 条")
    g.store.close()
    rc2 = run_mod.main(["--mock", "--reset", "--yes", "--once", "在吗"])
    g2 = Giftia(isolated, llm=MockLLM(isolated.model))
    left = g2.store.count_memories(g2.instance_no, only_active=False)
    print(f"    --reset --yes 之后：退出码 {rc2}，记忆 {left} 条，实例 #{g2.instance_no}")
    check("D2 --reset --yes 清空数据并重新开始", rc2 == 0 and left == 0)
    after = real_db.stat().st_mtime_ns if real_db.exists() else None
    check("D3 真实 data/giftia.db 没有被这次测试碰到", before == after, f"{before} vs {after}")

    # --reset 不带 --yes：非交互环境下 EOF 会怎样
    old = builtins.input
    builtins.input = ScriptedInput([])
    err4 = None
    try:
        run_mod.main(["--mock", "--reset"])
    except BaseException as exc:
        err4 = exc
    finally:
        builtins.input = old
    print(f"    --reset（无 --yes）在非交互 shell 里 → {type(err4).__name__ if err4 else '正常退出（已取消）'}: {err4}")
    # 期望（修复后）：EOFError 被当成"没人能回答确认"→ 取消并正常退出，
    # 而不是把 traceback 打给用户。原断言记录的是未捕获 EOF 的缺陷（审查 P3）。
    check("D4 --reset 无 --yes 时非交互环境优雅取消（不 traceback）",
          err4 is None, type(err4).__name__ if err4 else "正常退出")

    # --lifespan-hours 负数
    st = isolated.lifespan.effective_hours(-5)
    print(f"    --lifespan-hours -5 → effective_hours={st}（负数被静默忽略，退回配置值 "
          f"{isolated.lifespan.effective_hours(None)}）")
    check("D5 --lifespan-hours 负数被静默忽略", st == isolated.lifespan.effective_hours(None))

    # --once 遇到模型故障
    from giftia.llm import LLMError

    class Flaky(MockLLM):
        def chat(self, messages, **kw):
            raise LLMError("模拟故障：连接被重置")

    run_mod.make_llm = lambda settings: Flaky(settings.model)
    err5 = None
    try:
        rc3 = run_mod.main(["--mock", "--once", "在吗"])
    except BaseException as exc:
        err5 = exc
        rc3 = None
    print(f"    --once 遇到模型故障 → 退出码 {rc3}，异常={type(err5).__name__ if err5 else None}")
    check("D6 --once 在模型故障时给出降级回复而不是 traceback",
          err5 is None and rc3 == 0, f"rc={rc3} err={err5}")

    return report("beh_cli")


if __name__ == "__main__":
    raise SystemExit(main())
