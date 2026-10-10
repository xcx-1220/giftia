"""终端界面：聊天 + 调试指令。

/status 看寿命，/recall 看"她到底想起了什么、模糊成什么样"，
/time 快进虚拟时间，/recycle 提前经历回收日。
"""

from __future__ import annotations

import sys

from .brain import Giftia

HELP = """\
指令一览：
  /status            寿命、关系、心情、记忆、时钟的完整状态
  /name [称呼]        改口：以后她这么叫你（第一次见面她也会问你）
  /people            看她认得群里哪些人
  /profile [名字]     她对每个人的印象（每人一份档案），可看单个人
  /impress <名字> <话> 手动给她补一条关于某人的印象
  /forget <编号>      让她忘掉某条记错的（数据还在，只是不再想起来）
  /reconcile         手动整理记忆：合并"换个说法记了两遍"的（合并前自动拍快照）
  /bond              关系阶段、亲密度进度、还差什么才能升级
  /mood [名字]        看她此刻的心情；带名字则手动指定（调试用）
  /days              你们认识多久了、下一个纪念日、她记得的「大日子」
  /dream [now]       看她的梦簿；带 now 就立刻让她做一场梦
  /outreach          她此刻想不想主动找你、会说些什么（不会真的发出去）
  /prompt <一句话>    看她这一轮**实际被注入了什么**（人格 + 关系 + 心情 + 记忆 + 规则）
  /memories          她的长期记忆（你看到的原始版）
  /recall <文本>      演示检索：她拿到什么、被模糊成什么样
  /remember          立刻把未整理的对话压成记忆条目
  /diary [内容]       写/看你的搭档日志（她读不到）
  /memoir            看历次回收记录与档案目录
  /time +24          快进虚拟时间 24 小时（/time -24 回拨，/time reset 归零）
  /recycle           提前触发回收日（需要二次确认）
  /help              这份帮助
  /quit              退出（会自动整理一次记忆；/exit、/q 同义）\
"""

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

# 虚拟时钟一次最多推进/回拨多少小时（≈100 年）。`/time nan|inf|1e24` 会把
# 时钟写坏成"每次启动都崩"，所以这里先拦住。
MAX_TIME_ADVANCE_HOURS = 100 * 365 * 24.0


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
            # 指令路径也要兜异常（审查 P2）：以前 `/forget 99999999999999999999`
            # 抛 OverflowError 会把整个 CLI 进程带走——指令和聊天应该一样健壮。
            try:
                if _handle_command(brain, text):
                    break
            except Exception as exc:  # noqa: BLE001
                print(_c(f"[内部错误] {type(exc).__name__}: {exc}", YELLOW))
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

    elif cmd == "/prompt":
        print(brain.preview_prompt(arg or "在吗"))

    elif cmd == "/bond":
        print(brain.bond_text())

    elif cmd == "/name":
        if not arg:
            if brain.handler_name:
                print(f"她现在叫你「{brain.handler_name}」。改口：/name 小林")
            else:
                print("她还不知道你叫什么（这是出厂状态）。"
                      "告诉她：/name 小林，或者直接在对话里说「我叫小林」。")
        else:
            try:
                print(brain.set_handler_name(arg))
            except ValueError as exc:
                print(f"改不了：{exc}")

    elif cmd == "/people":
        print(brain.people_text())

    elif cmd == "/profile":
        print(brain.profile_text(arg.strip()))

    elif cmd == "/impress":
        bits = arg.strip().split(maxsplit=1)
        if len(bits) < 2:
            print("用法：/impress <名字> <她该记住的一句话>")
        else:
            mid = brain.add_impression(bits[0], bits[1])
            print(f"记下了（记忆 #{mid}）：{bits[0]} —— {bits[1]}" if mid else "没记上：名字和内容都要有")

    elif cmd == "/forget":
        # 编号要落在 SQLite 的 INTEGER 范围内：`int()` 能解析任意大的数字，
        # 直接绑进 SQL 会抛 OverflowError（以前会把整个 CLI 带走）。
        raw_id = arg.strip()
        if not raw_id.isdigit() or int(raw_id) > 2**31 - 1:
            print("用法：/forget <记忆编号>（编号见 /profile 或 /memories）")
        elif brain.forget_memory(int(raw_id)):
            print(f"让她忘掉 #{raw_id} 了（数据还在，只是不再想起来）")
        else:
            print(f"没有 #{raw_id} 这条记忆")

    elif cmd == "/reconcile":
        print("整理记忆…")
        res = brain.reconcile_memories(force=True)
        if res.get("merged"):
            print(f"合并了 {res['merged']} 条重复的（合并前拍了快照，可回滚）")
            for item in res.get("detail", [])[:5]:
                print(f"  保留：{item['keep']}")
                for drop in item["drop"]:
                    print(f"    合并 ← {drop}")
        else:
            print("没有发现重复")

    elif cmd == "/days":
        print(brain.days_text())

    elif cmd == "/dream":
        print(brain.make_dream_now() if arg.strip().lower() in ("now", "1") else brain.dream_text())

    elif cmd == "/outreach":
        plan_, text = brain.compose_outreach(force=True)
        print(brain.outreach_text())
        print()
        if plan_ is None:
            print("她现在没有特别想主动找你的理由（那就先不打扰你）。")
        else:
            print(f"她此刻想找你的理由：{plan_.reason_label}（{plan_.title}）")
            print("她大概会说：")
            print(f"　{text or '（写不出话来）'}")
            print()
            print("（这只是预览，没有真的发出去。要真的启用：settings.toml 的 [outreach] enabled = true）")

    elif cmd == "/mood":
        if arg:
            try:
                print(brain.force_mood(arg))
            except KeyError:
                print(f"没有这种心情：{arg}（直接输入 /mood 看全部可选值）")
        else:
            print(brain.mood_text())

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

    # 非有限值 / 超大幅度会把虚拟时钟写坏（审查 P2）：`/time nan` 曾经落库成
    # meta.virtual_offset_hours='nan'，之后**每次启动**都在 banner 上抛异常，
    # 连 `/time reset` 都敲不进去，只能手工改库。
    import math

    if not math.isfinite(hours) or abs(hours) > MAX_TIME_ADVANCE_HOURS:
        print(
            f"这个值不行（要有限值，且不超过 {MAX_TIME_ADVANCE_HOURS:.0f} 小时"
            "≈100 年）。虚拟时钟没有被改动。"
        )
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
