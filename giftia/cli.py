"""终端界面：聊天 + 调试指令。

/status 看寿命，/recall 看"她到底想起了什么、模糊成什么样"，
/time 快进虚拟时间，/recycle 提前经历回收日。
"""

from __future__ import annotations

import sys

from .brain import Giftia

HELP = """\
指令一览：
  /status            寿命、记忆、时钟的完整状态
  /memories          她的长期记忆（你看到的原始版）
  /recall <文本>      演示检索：她拿到什么、被模糊成什么样
  /remember          立刻把未整理的对话压成记忆条目
  /diary [内容]       写/看你的搭档日志（她读不到）
  /memoir            看历次回收记录与档案目录
  /time +24          快进虚拟时间 24 小时（/time -24 回拨，/time reset 归零）
  /recycle           提前触发回收日（需要二次确认）
  /help              这份帮助
  /quit              退出（会自动整理一次记忆）\
"""

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _supports_color() else text


def run_cli(brain: Giftia, *, banner: bool = True) -> int:
    persona = brain.settings.persona
    if banner:
        print(_c("─" * 60, DIM))
        print(_c(brain.banner(), CYAN))
        print(_c("─" * 60, DIM))
        print(_c("输入 /help 看指令，直接说话就是聊天，/quit 退出。", DIM))
        print()
        greeted = brain.store.meta_get_int("greeted_instance", 0)
        if greeted != brain.instance_no:
            brain.store.meta_set("greeted_instance", brain.instance_no)
            print(f"{_c(persona.name, MAGENTA)}　{persona.line('opening_line')}")
            print()

    while True:
        try:
            raw = input(_c("你 > ", CYAN))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = raw.strip()
        if not text:
            continue

        if text.startswith("/"):
            if _handle_command(brain, text):
                break
            continue

        try:
            reply = brain.handle(text)
        except Exception as exc:  # CLI 不该因为一次异常整个崩掉
            print(_c(f"[内部错误] {type(exc).__name__}: {exc}", YELLOW))
            continue

        for note in brain.notices:
            print(_c("  " + note, DIM))
        print(f"{_c(persona.name, MAGENTA)}　{reply}")
        if brain.last_recalled:
            blurred = sum(1 for r in brain.last_recalled if r.blurred)
            cited = len(brain.last_cited)
            extra = f"，说出口 {cited} 条（会记得更久）" if cited else ""
            print(
                _c(
                    f"  （这一轮她想起了 {len(brain.last_recalled)} 条记忆，"
                    f"其中 {blurred} 条已模糊{extra}）",
                    DIM,
                )
            )
        print()

    brain.close()
    print(_c("（对话已整理，记忆已保存。）", DIM))
    return 0


def _handle_command(brain: Giftia, text: str) -> bool:
    """返回 True 表示要退出。"""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    arg = arg.strip()

    if cmd in ("/quit", "/exit", "/q"):
        return True

    if cmd == "/help":
        print(HELP)

    elif cmd == "/status":
        print(brain.status_text())

    elif cmd == "/memories":
        print(brain.memories_text())

    elif cmd == "/recall":
        print(brain.recall_text(arg))

    elif cmd == "/remember":
        added = brain.consolidate(force=True)
        print(f"整理完成，新增 {added} 条长期记忆。")

    elif cmd == "/diary":
        print(brain.diary_add(arg) if arg else brain.diary_list())

    elif cmd == "/memoir":
        print(brain.memoir_text())

    elif cmd == "/time":
        _handle_time(brain, arg)

    elif cmd == "/recycle":
        _handle_recycle(brain, arg)

    else:
        print(f"未知指令：{cmd}（/help 看全部）")

    print()
    return False


def _handle_time(brain: Giftia, arg: str) -> None:
    if not arg or arg in ("show", "?"):
        st = brain.status()
        print(f"{brain.clock.describe_offset()}")
        print(f"当前虚拟时间：{brain.clock.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"剩余寿命：{st.human_remaining()}　阶段：{st.stage.label}")
        return

    if arg in ("reset", "0"):
        brain.clock.reset()
        print("虚拟时钟已归零（与真实时间同步）。")
        return

    try:
        hours = float(arg.replace("+", "").replace("h", "").replace("小时", ""))
    except ValueError:
        print("用法：/time +24　/time -24　/time reset")
        return

    brain.clock.advance(hours)
    st = brain.status()
    print(f"时间推进 {hours:+.1f} 小时 → {brain.clock.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"剩余寿命 {st.human_remaining()}　记忆阶段：{st.stage.label}")
    if st.expired:
        print("!! 已到期。下一条消息将触发回收日。")


def _handle_recycle(brain: Giftia, arg: str) -> None:
    st = brain.status()
    if arg != "yes":
        print("这会立刻结束她这一任的生命：记忆封存、实例换代，新的她不认识你。")
        print(f"当前实例 #{st.instance_no}，已服务 {st.human_service()}，"
              f"存有 {brain.store.count_memories(brain.instance_no)} 条记忆。")
        print("确认请输入：/recycle yes")
        return
    print(brain.run_recycle("搭档手动执行"))
    for note in brain.notices:
        print(_c("  " + note, DIM))
