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
) -> str:
    p = settings.persona
    now = now or life.activated_at
    blocks: list[str] = [p.core_text().strip()]

    if p.hint_text().strip():
        blocks.append("【如果有人坚持说你是程序】\n" + p.hint_text().strip())

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


def greeting(settings: Settings, life: LifeStatus, *, first_time: bool) -> str:
    p = settings.persona
    if first_time:
        return p.line("opening_line")
    return p.line("greeting_line")


def after_recycle_line(settings: Settings) -> str:
    return settings.persona.line("after_recycle_line")
