"""她能主动调用的记忆工具。

**为什么需要这个**：以前她只有一条写记忆的路——对话结束后后台整理。
于是她说"我记住了"的时候，其实什么都没发生：那句话是她嘴上说的，
记忆是几分钟后（甚至跨天）才由另一个流程补上的。她想记一笔、想查一下，
自己一点办法都没有。

现在给她三个工具（借鉴 QQ-agent 的 `memory_append/query/remove`）：
  · remember   —— 现在就记一笔
  · search     —— 现在就想起来（比被动检索准，因为是她自己提的问）
  · forget     —— 她想把某条放掉

设计上刻意克制的地方：
  · **可用性只有一个判据**（`available`）。对方搞了七层开关，还需要一个
    审计脚本兜住"UI 说能用、运行时空工具列表"的割裂——我们不给自己挖这个坑：
    能不能用只取决于"这一轮她是不是在跟搭档说话"。
  · **客人场景一律不给工具**。群里其他人说话时，"客人身份"的她不写记忆、
    不碰私聊内容——工具是能力，不能绕过这条边界。
  · **参数校验失败要把可用值喂回去**。模型猜错名字是常事，
    回一句"没有这个人，你认得的是：阿伟、小雨"比"参数错误"有用得多。
  · 一轮最多写 `max_writes` 条：防止她把一句话拆成十条记忆灌进来。
"""

from __future__ import annotations

import json
from typing import Any

# 工具协议：**必须写进系统提示词**。
# 实测教训：只把 tools 传给 API、不告诉她"你有这些能力"，她会把"我记住了"
# 当成一句台词说出来，一个工具都不调（真机跑过，三轮里零调用）。
# 对方（QQ-agent）的系统提示词第 3 段就是"工具协议"，这不是可选项。
TOOL_RULES = """【你手上有的几个动作】
下面这些是你"心里"就能做的动作，需要的时候直接做，不要说出来、也不要汇报：
· remember —— 听到值得长期记住的事（他的喜好、习惯、重要日子、承诺、正在经历的难事，
  或者你自己答应过什么），当场记一笔。
  **说了"我记住了"就必须真的记**——只说是空的，记下来才算数。
· search —— 觉得"这件事我好像知道"但想不起细节时，先查一下，别硬编、也别装作记得。
· forget —— 他说"那个别提了"，或者你发现自己记错了，就把那一条放掉。

记的时候要写清是**谁的事**，一句话说清。寒暄、玩笑、一次性的琐事不用记。
这些都是你私下的动作，做完不要向对方复述你记了什么。"""

# 一轮对话里最多主动写几条（防止刷屏式记记忆）
MAX_WRITES_PER_TURN = 2

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "把一件值得长期记住的事写进记忆。"
                "当你听到对方的喜好、习惯、重要日子、承诺、正在经历的难事，"
                "或者你自己答应过什么的时候用它——**写了才算真的记住**。"
                "不要用它记寒暄、玩笑和一次性的琐事。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的事，一句话说清，不超过 60 字，写明是谁的事。",
                    },
                    "about": {
                        "type": "string",
                        "description": "这件事关于谁（用名字）。关于你自己的可以留空。",
                    },
                    "importance": {
                        "type": "number",
                        "description": "0~1，越重要越接近 1。日常小事 0.4，重要的事 0.8。",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "在自己记得的事里找一找。当你觉得「这件事我好像知道」"
                "但想不起来细节时用它，别硬编。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查什么，几个词就行。"},
                    "about": {"type": "string", "description": "只看关于某个人的（可选）。"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": (
                "把某条记忆放掉（比如对方说「那个别提了」，或者你发现自己记错了）。"
                "编号从 search 的结果里拿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "要放掉的那条编号。"},
                    "reason": {"type": "string", "description": "为什么要放掉，一句话。"},
                },
                "required": ["memory_id"],
            },
        },
    },
]


def available(hub: Any) -> tuple[bool, str]:
    """她这一轮能不能用记忆工具。**唯一判据，别在别处再判一次。**"""
    if not getattr(hub.settings.memory, "tools_enabled", False):
        return False, "配置里关掉了（[memory] tools_enabled = false）"
    if getattr(hub, "last_scene", "") == "guest":
        # 群里的其他人说话时，她是"客人"：不写记忆、也不查私聊内容
        return False, "这一轮她是客人（群里别人说话），不该动记忆"
    if not getattr(hub, "is_handler", False):
        return False, "说话的不是搭档"
    return True, ""


def specs(hub: Any) -> list[dict[str, Any]]:
    ok, _why = available(hub)
    return TOOL_SPECS if ok else []


def _valid_names(hub: Any) -> list[str]:
    names = [n for n, _owner in hub._people_for_prompt() if n]
    if hub.handler_name:
        names.append(hub.handler_name)
    for _uid, name in hub.known_people().items():
        if name:
            names.append(hub.store.person_note(_uid) or name)
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def execute(hub: Any, name: str, arguments: str) -> str:
    """执行一次工具调用，返回给模型看的文本结果。**永远不抛异常**——
    工具报错不该让整轮对话崩掉，如实回一句就够了。"""
    ok, why = available(hub)
    if not ok:
        return f"（现在不能用记忆工具：{why}）"
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return "（参数不是合法 JSON，重新组织一下再调）"
    if not isinstance(args, dict):
        return "（参数应该是一个对象）"

    if name == "remember":
        return _remember(hub, args)
    if name == "search":
        return _search(hub, args)
    if name == "forget":
        return _forget(hub, args)
    return f"（没有 {name} 这个工具；可用的是 remember / search / forget）"


def _remember(hub: Any, args: dict) -> str:
    content = str(args.get("content") or "").strip()
    if not content:
        return "（content 不能为空）"
    if len(content) > 120:
        content = content[:120]
    about = str(args.get("about") or "").strip()
    if about and about not in _valid_names(hub):
        return (
            f"（你不认识叫「{about}」的人，别乱写。你认得的是："
            + "、".join(_valid_names(hub)[:12])
            + "；关于你自己的事可以不带 about）"
        )
    scope, subject = hub.memory_scope
    try:
        importance = max(0.0, min(1.0, float(args.get("importance") or 0.6)))
    except (TypeError, ValueError):
        importance = 0.6
    mem_id = hub.store.add_memory(
        instance_no=hub.instance_no,
        created_ts=hub.clock.iso(),
        happened_ts=hub.clock.iso(),
        summary=content,
        tags="主动",
        emotion="平静",
        importance=importance,
        layer="important" if importance >= 0.7 else "normal",
        scope=scope,
        subject_id=subject,
        speaker_id=hub.speaker_id if scope != "private" else "",
        about=about,
        source_session="tool",
    )
    hub.tool_writes.append(content)
    hub.notices.append(f"[记忆] 她自己记下了一条：{content}")
    return f"记下了（#{mem_id}）。"


def _search(hub: Any, args: dict) -> str:
    from giftia.memory import recall as _recall

    query = str(args.get("query") or "").strip()
    if not query:
        return "（query 不能为空）"
    about = str(args.get("about") or "").strip()
    memories = hub.store.active_memories(hub.instance_no, view=hub.memory_view)
    if about:
        memories = [m for m in memories if str(m.get("about") or "") == about]
    if not memories:
        return "（你这里没有相关的事，别硬想。）"
    hits = _recall(
        query, memories, hub.status().stage, hub.settings.memory, hub.clock.now()
    )
    if not hits:
        return "（想不起来相关的事。）"
    lines = [f"#{h.memory['id']}　{h.text}" for h in hits[:5]]
    return "想起来这些：\n" + "\n".join(lines)


def _forget(hub: Any, args: dict) -> str:
    raw = args.get("memory_id")
    try:
        mem_id = int(raw)
    except (TypeError, ValueError):
        return "（memory_id 得是一个编号，从 search 的结果里拿）"
    # 只能忘掉她"此刻看得见"的记忆：不能借这个工具碰到别人的私事
    visible = {
        int(m["id"]) for m in hub.store.active_memories(hub.instance_no, view=hub.memory_view)
    }
    if mem_id not in visible:
        return f"（#{mem_id} 不在你能看到的记忆里）"
    hub.store.forget_memory(mem_id)
    reason = str(args.get("reason") or "").strip()
    hub.notices.append(f"[记忆] 她把 #{mem_id} 放掉了" + (f"（{reason}）" if reason else ""))
    return f"放下 #{mem_id} 了。"
