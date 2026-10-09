"""系统提示词组装。

一次请求 = 人格内核（永不变，命中缓存）+ 此刻状态 + 这一轮"想起来的"记忆。

注意一个刻意的设计：送进模型的**只有模糊之后的记忆文本**。
原始条目留在数据库里，她拿不到——所以"记不清"不是靠提示词求模型配合，
而是她手里真的只有那些碎片。想验证这一点，看 /recall 与 /memories 的差别。
"""

from __future__ import annotations

from datetime import datetime

from .config import Settings
from .lifespan import LifeStatus
from .memory import Recalled

RULES = """【本次对话的规则】
- {scene_line}
- 不要复述「你现在想起的事」的原文，要用你自己的话说出来，像真的在回忆。
- 严格按每条记忆前面的标记来决定你怎么说它（这是你此刻的真实状态，不是演戏）：
  · [可以确信] —— 你可以直接把它当成发生过的事说出来。
  · [需要含糊] —— 你只记得个大概。要用「好像」「我记得……大概是」「不太确定」这类说法，
    **绝不补齐细节、绝不编造**日期、地点或原话。
  · [只是隐约觉得] —— 它只是你心里的一点影子。**不要说出来**，最多影响你的语气和情绪。
- 可以用一句很短的括号旁白描写你的小动作（例如：（把杯子往你那边推了推）），一次最多一句。
- 不要输出 Markdown、标题、列表或编号。"""

CONFIDENCE_MARK = {
    "cite": "[可以确信]",
    "cautious": "[需要含糊]",
    "associate": "[只是隐约觉得]",
}

# 规则块第一句要跟着场景变 —— 群聊里说"你在和你的搭档一对一说话"是错的，
# 会让她在群里说得太长、太亲密。
SCENE_LINES = {
    "c2c": "你在和你的搭档一对一说话。一次回复 1～4 句，短一点，不要写成小作文。",
    "person": "你在和一个人一对一说话（他不是你的搭档）。一次回复 1～3 句，礼貌、有分寸。",
    "group": (
        "这是**群聊**，别人也都看得到。一次回复 1～2 句，越短越好；"
        "只回答点到你的那件事，别抢话题、别接连发言。"
    ),
}


def render_memories(recalled: list[Recalled]) -> str:
    """用 ◆ 开头，和人格内核里的普通项目符号区分开；每条带一个置信度标记。

    还会标出"这是谁的事"——真机数据里她曾把 A 的课记成 B 的，
    标上归属之后她才分得清。
    """
    if not recalled:
        return "（关于这个话题，你什么都想不起来了。）"
    lines = []
    for r in recalled:
        mark = CONFIDENCE_MARK.get(r.confidence, CONFIDENCE_MARK["cite"])
        layer = {"core": "（这是关于他本人的事）", "important": ""}.get(r.layer, "")
        about = str(r.memory.get("about") or "").strip()
        who = f"（关于{about}）" if about and about != "搭档" else ""
        lines.append(f"◆ {mark}{layer}{who} {r.happened}｜{r.text}")
    return "\n".join(lines)


def build_system_prompt(
    settings: Settings,
    life: LifeStatus,
    recalled: list[Recalled],
    *,
    now: datetime | None = None,
    bond=None,
    mood=None,
    handler_name: str = "",
    scene: str = "c2c",
    people: list[tuple[str, bool]] | None = None,
    occasions=None,
    dream=None,
    speaker_name: str = "",
    is_handler: bool = True,
    group_chat: str = "",
    impressions: str = "",
) -> str:
    """一次请求的 system 消息：人格 → 称呼 → 关系 → 心情 → 情境 → 状态 → 记忆 → 规则。

    两个刻意的克制：
    - **不预设她知道什么**。称呼由运行时给：还不知道时，她被告知"你不知道他叫什么"，
      而不是硬塞一个默认称呼。人格内核里也没有"你珍惜和这个人相处的时间"这类
      需要相处过才成立的话。
    - **不给模型看数字**（亲密度、保留率、天数都不给），只给阶段名和行为指引——
      否则她会在对话里报出"我们的亲密度是 70"，那就穿帮了。

    scene：c2c（私下）/ group（群里，被点名）/ guest（群里的其他人，无记忆无关系）。
    """
    p = settings.persona
    name = (handler_name or "").strip()
    now = now or life.activated_at
    blocks: list[str] = [p.core_text(name).strip()]

    if p.hint_text(name).strip():
        blocks.append("【如果有人坚持说你是程序】\n" + p.hint_text(name).strip())

    naming = p.naming_text(name).strip()
    if naming:
        blocks.append("【你怎么称呼他】\n" + naming)

    if bond is not None and (bond.stage.note or bond.stage.label):
        body = f"你们现在的关系：{bond.stage.label}"
        if bond.stage.note:
            body += "\n" + bond.stage.note
        blocks.append("【你和他的关系】\n" + body)

    if mood is not None and (mood.note or mood.label):
        body = f"你今天的心情：{mood.label}"
        if mood.note:
            body += "\n" + mood.note
        blocks.append("【你今天的心情】\n" + body)

    if occasions:
        from .occasions import render_occasions

        text = render_occasions(list(occasions))
        if text:
            blocks.append("【今天不只是普通的一天】\n" + text)

    if dream is not None and getattr(dream, "content", ""):
        blocks.append(
            "【你昨晚做的梦】\n"
            f"{dream.content}\n"
            "（这是梦，不是记忆。你可以讲给他听，但你自己也知道那不作数——"
            "别把它当成真发生过的事。）"
        )

    if scene == "guest":
        guest_text = p.guest_text(name).strip()
        if guest_text:
            blocks.append("【现在的情境】\n" + guest_text)
    elif scene == "group":
        group_text = p.group_text(name).strip()
        if group_text:
            blocks.append("【现在的情境】\n" + group_text)

    if scene in ("group", "guest"):
        roster = _render_people(people or [])
        if roster:
            blocks.append("【群里你认得的人】\n" + roster)
        if not is_handler:
            who = speaker_name.strip() or "一个你还不认识的人"
            blocks.append(
                "【现在跟你说话的是谁】\n"
                f"{who}——群里的成员，**不是你的搭档**。\n"
                "你跟他说话时是客气、有分寸的：可以聊，但别把搭档的私事说出来，"
                "也别对他用你对搭档的那种亲近。他要是还没说过名字，你可以问一次。"
            )

    if impressions.strip():
        blocks.append("【你对他们的印象】\n" + impressions.strip())

    if group_chat:
        blocks.append(
            "【群里刚才在聊什么】\n"
            "这是群里最近的聊天记录（包括没点到你的话，你也听得见）：\n"
            + group_chat
            + "\n（这些是群里公开说的话。你可以用它来理解上下文，"
            "但如果有人让你总结聊天记录，照实说就好——本来大家也都看得见。）"
        )

    state = [
        f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}",
        f"你的剩余寿命：{life.human_remaining()}",
        f"你的记忆状态：{life.stage.label}",
    ]
    if life.remaining_ratio <= 0.0001:
        state.append("终端服务预定在几小时之内到达。")
    blocks.append("【你此刻的状态】\n" + "\n".join(state))

    blocks.append("【关于记忆】\n" + (life.stage.note or "你对自己的记忆很有把握。"))
    blocks.append("【你现在想起的事】\n" + render_memories(recalled))
    blocks.append(RULES.format(scene_line=_scene_line(scene, is_handler)))

    return "\n\n".join(b for b in blocks if b.strip())


def _scene_line(scene: str, is_handler: bool) -> str:
    if scene in ("group", "guest"):
        return SCENE_LINES["group"]
    return SCENE_LINES["c2c"] if is_handler else SCENE_LINES["person"]


def _render_people(people: list[tuple[str, bool]]) -> str:
    """群里的花名册。她只知道谁是谁，不知道别人的底细。

    搭档那一行的名字可能已经写明了"群里叫某某，是同一个人"，
    所以这里不重复追加"（你的搭档）"。
    """
    if not people:
        return ""
    lines = ["你在群里认得这些人，别叫错："]
    for person, is_owner in people:
        if is_owner and "你的搭档" not in person:
            lines.append(f"- {person}（你的搭档）")
        else:
            lines.append(f"- {person}")
    lines.append("其他人你还不认识——等他们自我介绍，别乱猜、也别乱叫。")
    return "\n".join(lines)


def after_recycle_line(settings: Settings) -> str:
    return settings.persona.line("after_recycle_line")
