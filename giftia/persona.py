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
- 你在和你的搭档一对一说话。一次回复 1～4 句，短一点，不要写成小作文。
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


def render_memories(recalled: list[Recalled]) -> str:
    """用 ◆ 开头，和人格内核里的普通项目符号区分开；每条带一个置信度标记。"""
    if not recalled:
        return "（关于这个话题，你什么都想不起来了。）"
    lines = []
    for r in recalled:
        mark = CONFIDENCE_MARK.get(r.confidence, CONFIDENCE_MARK["cite"])
        layer = {"core": "（这是关于他本人的事）", "important": ""}.get(r.layer, "")
        lines.append(f"◆ {mark}{layer} {r.happened}｜{r.text}")
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
    blocks.append(RULES)

    return "\n\n".join(b for b in blocks if b.strip())


def _render_people(people: list[tuple[str, bool]]) -> str:
    """群里的花名册。她只知道谁是谁，不知道别人的底细。"""
    if not people:
        return ""
    lines = ["你在群里认得这些人，别叫错："]
    for person, is_owner in people:
        lines.append(f"- {person}（你的搭档）" if is_owner else f"- {person}")
    lines.append("其他人你还不认识——等他们自我介绍，别乱猜、也别乱叫。")
    return "\n".join(lines)


def after_recycle_line(settings: Settings) -> str:
    return settings.persona.line("after_recycle_line")
