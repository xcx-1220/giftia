"""她什么时候开口：响应档位 + 触发判定。

借鉴 QQ-agent 的 `tier-slider.js`——那边刻意做成零依赖纯函数，
避免"配置模块 ↔ 提示词模块"循环依赖。这里同理：**只做纯计算，
不碰数据库、不碰平台、不碰模型**，所以可以单独测。

档位（`[runtime] response_tier`，0~100 无级）：

| 位置 | 档 | 行为 |
|---|---|---|
| 0~19   | 1 | 只在她被 @ 时开口（最安静） |
| 20~44  | 2 | 被 @ **或**说到关键词时开口 |
| 45~69  | 3 | 再加上"按概率自己插嘴"，概率随滑条线性上升 |
| 70~100 | 4 | 群里任何消息都可能让她开口（最吵） |

没命中的消息**当场标已读**：只旁听、不回复、不建会话、也不为它花模型调用
（名字判定只走正则）。旁听来的话会攒着，等搭档那一轮或退出路径统一整理成记忆——
整理那一步会花一次调用（见 `brain.flush_listen_pending`），所以准确说法是
"不回复、不建会话、不逐条调模型"，而不是字面上的"零 token"。
"""

from __future__ import annotations

TIER_BOUNDS = (20, 45, 70)


def tier_of(pos: int) -> int:
    """0~100 → 1~4 档。"""
    pos = max(0, min(100, int(pos)))
    if pos < TIER_BOUNDS[0]:
        return 1
    if pos < TIER_BOUNDS[1]:
        return 2
    if pos < TIER_BOUNDS[2]:
        return 3
    return 4


def speak_probability(pos: int) -> float:
    """3 档下"自己插嘴"的概率（0~1）。跟着滑条线性上升。

    公式沿用 QQ-agent：`(pos - 20) / 70`，所以 45 → 36%、70 → 71%。
    档位 3 的下界正好给一个"偶尔说一句"的手感，不会一上来就很吵。
    """
    pos = max(0, min(100, int(pos)))
    return max(0.0, min(1.0, (pos - 20) / 70.0))


def decide(
    pos: int,
    *,
    at_bot: bool,
    text: str,
    keywords: tuple[str, ...] | list[str] = (),
    roll: float = 1.0,
) -> tuple[bool, str]:
    """这条消息要不要让她开口？返回 (是否开口, 原因)。

    `roll` 是调用方先掷好的随机数（0~1）——**必须只掷一次并落库**，
    我借鉴的那个项目在这里踩过坑：预判和实跑各掷一次，结果对不上，
    还被写进文档当成"原设计如此"。
    """
    if at_bot:
        return True, "被点名"
    tier = tier_of(pos)
    if tier == 1:
        return False, "1 档：只应 @"
    hits = [k for k in keywords if k and k in text]
    if hits:
        return True, f"命中关键词「{hits[0]}」"
    if tier == 2:
        return False, "2 档：没有关键词"
    if tier == 3:
        p = speak_probability(pos)
        return (roll < p), f"3 档：掷骰 {roll:.2f} vs 概率 {p:.2f}"
    return True, "4 档：群里的话都可能接"
