"""配置加载：settings.toml（行为参数）+ persona.toml（人格内核）+ .env（凭据）。

配置是**唯一契约**：拼错的键、越界的值、缺一整段退化曲线，以前都会被静默忽略
（`_sub()` 只取声明过的字段、`stages` 为空就兜底成"永不模糊"）。这里的原则是
**fail-loud**：能报错的一律报错并说人话，实在只能继续的（未知键）也要打印出来。
"""

from __future__ import annotations

import math
import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
ENV_FILE = ROOT / ".env"


# ── 数据类 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ModelConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    name: str = "deepseek-flash"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 1.15
    max_tokens: int = 900
    request_timeout: int = 60
    thinking: bool = False
    reasoning_effort: str = "low"

    @property
    def is_mock(self) -> bool:
        return self.provider.lower() == "mock"


@dataclass(frozen=True)
class Stage:
    """记忆退化阶段。remaining_ratio 越高越清晰。

    `min_score` 是**相对门槛**：检索分数先除以"本轮候选集里的最高分"再与它比较，
    所以首名恒为 1.0，候选集小时它几乎拦不住任何东西。要绝对严格度用
    `MemoryConfig.min_raw_score`（未归一化的 BM25 分）。
    """

    name: str
    label: str
    min_ratio: float
    top_k: int
    min_score: float
    blur: float
    max_chars: int
    note: str = ""


# 寿命的允许范围：1 小时 ~ 100 万小时（≈114 年）。
# 低于/高于它的值不会"只是不好看"：0 或负数会让 ratio 直接归 0、阶段变 terminal、
# 第一条消息就触发回收换代（P1-9）；再大的值会让 timedelta 溢出、/status 直接崩。
LIFESPAN_MIN_HOURS = 1.0
LIFESPAN_MAX_HOURS = 1_000_000.0


def _cfg_error(message: str) -> ValueError:
    """配置错误统一带上"配置错误："前缀，方便人眼和探针一眼分辨。"""
    return ValueError("配置错误：" + message)


def _check_number(
    value, *, where: str, key: str, lo: float, hi: float, why: str = ""
) -> float:
    """把一个配置数值校验进 [lo, hi]。越界**报错**，不静默退化。

    `why` 用来补一句"这个值写坏了会发生什么"，比单说"范围不对"有用得多。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise _cfg_error(
            f"{where} 的 {key} 必须是数字，现在是 {value!r}（{type(value).__name__}）"
        ) from None
    if not math.isfinite(num):
        raise _cfg_error(f"{where} 的 {key} = {value!r} 不是有限数")
    if not (lo <= num <= hi):
        tail = f"；{why}" if why else ""
        raise _cfg_error(
            f"{where} 的 {key} = {num:g} 超出允许范围 [{lo:g}, {hi:g}] 小时{tail}"
        )
    return num


@dataclass(frozen=True)
class LifespanConfig:
    total_hours: float = 81920.0
    demo_total_hours: float = 0.0
    terminal_warning_hours: float = 72.0

    def effective_hours(self, override: float | None = None) -> float:
        """实际寿命：命令行覆盖 > demo > 正式设定。"""
        if override and override > 0:
            return float(override)
        if self.demo_total_hours > 0:
            return float(self.demo_total_hours)
        return float(self.total_hours)

    def validated(self) -> "LifespanConfig":
        """范围校验（只由 load_settings 在**读配置文件**这条路上调用）。

        - `total_hours = 0` 当成"没填"，回落到代码默认值并打印一行警告
          （以前是静默变成"出生即到期"：第一条消息就回收换代）；
        - 负数 / 非有限 / 超过上限 → 报错，启动即失败并说人话；
        - `demo_total_hours` 0 = 不用；负数或越界 → 报错；
        - `terminal_warning_hours` 负数 → 报错（0 = 不预告，允许）。

        注意：数据类本身仍可被程序化构造（探针/测试要造边界值），
        `Lifespan.status()` 对库里的坏值还有第二道兜底。
        """
        total = float(self.total_hours)
        if total == 0:
            default = LifespanConfig().total_hours
            print(
                f"[config] settings.toml [lifespan] total_hours = 0 会被当成"
                f"「没填」，按默认 {default:g} 小时处理；"
                "想做短寿命演示请写 demo_total_hours。",
                file=sys.stderr,
            )
            total = default
        else:
            total = _check_number(
                total, where="settings.toml [lifespan]", key="total_hours",
                lo=LIFESPAN_MIN_HOURS, hi=LIFESPAN_MAX_HOURS,
                why="0 或负数会让剩余比例直接归 0、第一条消息就触发回收换代",
            )
        demo = _check_number(
            self.demo_total_hours, where="settings.toml [lifespan]", key="demo_total_hours",
            lo=0.0, hi=LIFESPAN_MAX_HOURS,
        )
        warn = _check_number(
            self.terminal_warning_hours, where="settings.toml [lifespan]",
            key="terminal_warning_hours", lo=0.0, hi=LIFESPAN_MAX_HOURS,
        )
        return replace(self, total_hours=total, demo_total_hours=demo, terminal_warning_hours=warn)



@dataclass(frozen=True)
class BondStage:
    """关系的一档。升级要求 min_affinity 与 min_days **同时**满足。"""

    key: str
    label: str
    min_affinity: float
    min_days: float
    note: str = ""


@dataclass(frozen=True)
class MoodDef:
    """一种心情：标签 + 给模型的行为指引。"""

    key: str
    label: str
    note: str = ""


@dataclass(frozen=True)
class RelationshipConfig:
    enabled: bool = True
    gain_daily: float = 1.5            # 每天第一次认真说话（未打折扣时）
    gain_emotion_bonus: float = 0.5    # 那天说了一件有情绪的事
    gain_core_memory: float = 2.0      # 记住一件"关于他本人"的事
    gain_important_memory: float = 1.0
    decay_per_day: float = 0.0         # 默认不衰减：她不会因为你忙就少喜欢你一点
    # 递减增长：实际涨幅 = 名义值 × (1 - 当前亲密度/100)。
    # 越亲近越难涨，于是「亲近」「无可替代」要靠时间熬，而不是靠刷。
    diminish: bool = True


@dataclass(frozen=True)
class MoodConfig:
    enabled: bool = True
    duration_hours: float = 10.0        # 一次心情持续多久
    uneasy_hours: float = 48.0          # 剩余寿命低于这个小时数 → 不安（压过一切）
    uneasy_duration_hours: float = 4.0
    tone_threshold: float = 0.4         # 语气分超过它才算"明显开心/低落"
    weights: dict = field(default_factory=dict)  # 每日掷骰子的心情权重


DEFAULT_BOND_STAGES: tuple[BondStage, ...] = (
    BondStage("stranger", "刚认识", 0, 0, ""),
    BondStage("partner", "搭档", 15, 1, ""),
    BondStage("familiar", "熟悉", 40, 14, ""),
    BondStage("close", "亲近", 70, 60, ""),
    BondStage("irreplaceable", "无可替代", 92, 180, ""),
)

DEFAULT_MOODS: dict[str, MoodDef] = {"calm": MoodDef("calm", "平静", "")}


DEFAULT_MILESTONES = [1, 3, 7, 14, 30, 50, 100, 200, 365, 500, 730, 1000]


@dataclass(frozen=True)
class OccasionsConfig:
    enabled: bool = True
    milestones: list = field(default_factory=lambda: list(DEFAULT_MILESTONES))
    big_day_lookahead_days: int = 1   # 今天 + 明天的大日子都会提醒
    # 事情办完之后的几天内，她还可以主动问一句结果（"昨天考得怎么样"）。
    # 这是"她主动一点"里最像"真的在惦记你"的一块：只是提醒还不算什么，
    # 记得回头问才算。0 = 不问。
    follow_up_days: int = 2


@dataclass(frozen=True)
class DreamConfig:
    enabled: bool = True
    min_hours_between: float = 12.0   # 两场梦之间至少隔多久
    min_memories: int = 3             # 记忆太少就不做梦
    fragments: int = 6                # 一场梦最多揉进几条记忆
    blur: float = 0.6                 # 碎片模糊到什么程度
    tell_within_hours: float = 36.0   # 超过这么久还没讲的梦就不再翻出来


@dataclass(frozen=True)
class OutreachConfig:
    # 默认关闭：真要让她主动找你，得先配好平台凭据再打开
    enabled: bool = False
    idle_days: float = 3.0            # 多久没说话就算"想你了"
    max_per_30_days: int = 4          # 30 天的总上限
    # **每天最多主动几次** —— 防打扰的真正闸门。调大之前先想清楚：
    # 她主动的每一条都是一次 API 调用，而且主动消息比回复更容易让人烦。
    max_per_day: int = 3
    min_gap_hours: float = 6.0
    # True = 用官方的「互动召回消息」（is_wakeup），额度更紧但更合规；
    # False = 普通主动消息（有频控，没有 30 天 4 条的硬限制）
    use_wakeup: bool = False
    # 主动消息发给谁：owner（私聊给搭档，默认）| group（发到最后活跃的群）| both
    # 注意：发到群里时只会用"大家都看得见"的理由（大日子/纪念日），
    # 梦、心事、"快到终端服务了"这类只在私聊说。
    target: str = "owner"
    # 距上次说话不足这么久，就不主动"分享心情" —— 防"天天报心情"把额度烧光
    # （心情分享曾经排在"太久没见"前面，4 天就能吃掉一个月的额度）。
    mood_share_min_idle_hours: float = 6.0
    # use_wakeup=True 时官方「互动召回消息」的硬额度：30 天 4 条。
    # 它比 max_per_30_days 紧得多，所以必须单独记一个上限，不能只看本地账本。
    wakeup_max_per_30_days: int = 4


@dataclass(frozen=True)
class MemoryConfig:
    consolidate_every: int = 8
    session_window: int = 24
    session_char_budget: int = 6000
    # 去重：新记忆和已有的像到这个程度，就当成"同一件事"（不再重复记）
    # 真机数据里 9 条私聊记忆有 4 条是同一件事，白白挤掉了别的位置
    dedupe_ratio: float = 0.7
    # 整理时"词面像到这个程度"就送去问模型（实测真实重复 0.43~0.54，不同的事 0.16~0.34）
    dedupe_candidate_ratio: float = 0.35
    # 整理时把"她已经记得的事"喂给模型，让它别再记一遍
    known_hint_limit: int = 14
    # 定期做一次"整理记忆"（把换个说法记了两遍的合并掉）：同一个归属多久做一次
    reconcile_hours: float = 6.0
    reconcile_min_memories: int = 4
    # 让她能自己调工具记一笔 / 查一下（giftia/agent_tools.py）。
    # 关掉就退回"只有后台整理一条写记忆的路"。
    tools_enabled: bool = True
    # 工具轮次上限（含最后一轮"不给工具、只让她说话"）。3 够用：问一次、记一笔、说话。
    tools_max_rounds: int = 3
    w_bm25: float = 1.0
    w_importance: float = 0.35
    w_recency: float = 0.25       # 作用于"保留率"这个时间项
    w_layer: float = 0.10
    min_importance: float = 0.15

    # 召回强化
    retention_unit_hours: float = 120.0
    cite_min_shared: int = 3
    cite_min_chars: int = 6
    retention_relief: float = 0.6

    # 记忆分层
    layer_core_blur: float = 0.0
    layer_important_blur: float = 0.5
    layer_normal_blur: float = 1.0
    layer_core_weight: float = 1.0
    layer_important_weight: float = 0.5
    layer_normal_weight: float = 0.0

    # 内核记忆保底注入（见 brain._core_memories）：不靠关键词命中，
    # core 层也常驻进提示词——否则"她记得你是小林"只在你正好提到名字时才成立。
    core_always_inject: bool = True
    core_always_inject_max: int = 3
    core_max_chars: int = 200

    # 检索细节
    relative_cutoff: float = 0.5
    # **绝对**相关度门槛：未归一化的 BM25 原始分低于它就直接丢（0.0 = 不启用）。
    # 注意区分：stage.min_score 是**相对**门槛（分数先除以本轮候选集最高分，
    # 首名恒为 1.0，所以候选集小时它拦不住任何东西，同一条记忆的分数还会
    # 随旁边有哪些候选而变）。要"不管旁边有什么都拦得住"的严格度，用这个。
    min_raw_score: float = 0.0
    # 候选集预筛上限：记忆超过这么多条时，先按"层级 / 重要性 / 新近"取前 N 条再打分。
    # 默认 2000 —— 正常数据量下**不改变任何行为**，只是给每轮检索的开销一个上界。
    recall_prefilter: int = 2000
    rewrite_when_weak: bool = True
    rewrite_min_hits: int = 1
    rewrite_weak_score: float = 0.45

    # 输出侧置信度
    confidence_associate_blur: float = 0.6
    confidence_cautious_retention: float = 0.5

    # ── 分层查询 ──────────────────────────────────────────────
    def layer_blur(self, layer: str) -> float:
        return {
            "core": self.layer_core_blur,
            "important": self.layer_important_blur,
        }.get(layer, self.layer_normal_blur)

    def layer_weight(self, layer: str) -> float:
        return {
            "core": self.layer_core_weight,
            "important": self.layer_important_weight,
        }.get(layer, self.layer_normal_weight)


@dataclass(frozen=True)
class RuntimeConfig:
    # 空 = 她还不知道你叫什么（第一次见面她会问）。这是出厂状态，不是预设。
    handler_name: str = ""
    user_id: str = "handler"
    show_memoir_after_recycle: bool = True
    # 群里被 @ 时：true = 回答（默认，以"客人"身份：不检索记忆、不写记忆、不推进关系）；
    #             false = 装作没看见
    group_stranger_reply: bool = True
    # 自动认主（省掉 /bind）：
    #   私聊里第一个说话的人 = 搭档；
    #   群里只有**群主** @ 她时才算（陌生人做不到这一点）。
    # 官方 openid 与 QQ 号不互通、无法预先填，所以只能靠"谁先跟她说话"来认人。
    auto_bind: bool = True
    # 群聊响应档位 0~100（见 giftia/trigger.py）：0~19 只应 @、20~44 加关键词、
    # 45~69 再加"按概率插嘴"、70+ 任何消息都可能接。没命中的当场标已读，零 token。
    response_tier: int = 0
    # **每个群每天最多自己插几次嘴**（不含被 @ 的）。
    # 这一条是必须的：3 档是按"每条消息"掷骰子的，热闹的群里一天能掷出几十次，
    # 不封顶会变成刷屏。
    speak_max_per_day: int = 3
    # 2/3 档下能叫醒她的词
    keywords: list = field(default_factory=list)
    # 被触发后等她说完再回：这段时间里群里追加的话算同一批，一起处理。
    # **固定倒计时，不因为新消息重置** —— 滑动窗口在热闹的群里会让她永远等下去。
    wake_delay_seconds: float = 2.0
    # 发送节奏：每会话串行 + 最小间隔 + 限频（防平台风控，也防刷屏）
    send_min_interval: float = 1.2
    send_max_interval: float = 2.8
    send_max_per_minute: int = 6
    send_max_per_hour: int = 60


@dataclass(frozen=True)
class Persona:
    name: str = "艾拉"
    model_code: str = "Giftia-Type-07"
    # 还不知道该怎么称呼他时，人格文本里用的兜底代词（不是称呼本身）
    handler_title: str = "他"
    core: str = ""
    opening_line: str = "……请问，我该怎么称呼你？"
    after_recycle_line: str = "……初次见面。"
    out_of_character_hint: str = ""
    group_hint: str = ""
    guest_hint: str = ""
    naming_unknown: str = ""
    naming_known: str = ""

    def core_text(self, handler_title: str | None = None) -> str:
        """填好占位符。handler_title 传运行时称呼；还不知道时用兜底代词。"""
        return _fill(self.core, self, handler_title)

    def hint_text(self, handler_title: str | None = None) -> str:
        return _fill(self.out_of_character_hint, self, handler_title)

    def line(self, key: str) -> str:
        return _fill(getattr(self, key, "") or "", self)

    def group_text(self, handler_title: str | None = None) -> str:
        return _fill(self.group_hint, self, handler_title)

    def guest_text(self, handler_title: str | None = None) -> str:
        return _fill(self.guest_hint, self, handler_title)

    def naming_text(self, handler_name: str = "") -> str:
        """「你怎么称呼他」这一块：知道名字 vs 还不知道，是两套文本。"""
        name = (handler_name or "").strip()
        if name:
            return _fill(self.naming_known, self, name)
        return _fill(self.naming_unknown, self)


def _fill(text: str, p: "Persona", handler_title: str | None = None) -> str:
    return (
        (text or "")
        .replace("{name}", p.name)
        .replace("{model_code}", p.model_code)
        .replace("{handler_title}", (handler_title or "").strip() or p.handler_title)
    )


@dataclass(frozen=True)
class Settings:
    model: ModelConfig = field(default_factory=ModelConfig)
    lifespan: LifespanConfig = field(default_factory=LifespanConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    relationship: RelationshipConfig = field(default_factory=RelationshipConfig)
    mood: MoodConfig = field(default_factory=MoodConfig)
    occasions: OccasionsConfig = field(default_factory=OccasionsConfig)
    dream: DreamConfig = field(default_factory=DreamConfig)
    outreach: OutreachConfig = field(default_factory=OutreachConfig)
    persona: Persona = field(default_factory=Persona)
    stages: tuple[Stage, ...] = ()
    bond_stages: tuple[BondStage, ...] = DEFAULT_BOND_STAGES
    moods: dict[str, MoodDef] = field(default_factory=lambda: dict(DEFAULT_MOODS))
    root: Path = ROOT

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "giftia.db"

    @property
    def memoir_dir(self) -> Path:
        return self.data_dir / "回收日志"

    @property
    def sealed_dir(self) -> Path:
        return self.data_dir / "封存"

    def stage_for(self, remaining_ratio: float) -> Stage:
        """按剩余寿命比例挑阶段（stages 已按 min_ratio 降序排列）。"""
        for st in self.stages:
            if remaining_ratio >= st.min_ratio:
                return st
        return self.stages[-1] if self.stages else Stage(
            "clear", "记忆清晰", 0.0, 6, 0.15, 0.0, 400
        )


# ── 加载 ──────────────────────────────────────────────────────────
def _strip_env_value(raw: str) -> str:
    """剥掉 .env 值两端的空白、**成对的**引号，以及引号外的行内注释。

    三种常见写法以前都会坏：
      - `KEY=value # 注释`     → 注释被并进值里（API key 永远不对）
      - `KEY="value" # 注释`   → 同上
      - `KEY="value` （少一个引号）→ 以前会把仅有的那个引号也剥掉，现在原样保留
    引号**内部**的 `#` 不算注释（密码里带 # 是合法的）。
    """
    text = raw.strip()
    if not text:
        return ""
    quote = text[0] if text[0] in "\"'" else ""
    if quote:
        end = text.find(quote, 1)
        if end != -1:
            # 成对引号：只取引号里的内容，引号之后的东西（空格、注释）全丢掉
            return text[1:end]
        return text  # 引号不成对：原样保留，别猜
    # 无引号：` #` 之后算注释（要求 # 前面是空白，避免误伤 a#b 这种值）
    cut = len(text)
    for i, ch in enumerate(text):
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            cut = i
            break
    return text[:cut].strip()


def load_env_file(path: Path | None = None, *, override: bool = False) -> int:
    """读取 .env（零依赖的 KEY=VALUE 解析）。返回载入的条目数。

    已经存在的环境变量默认不被覆盖——这样 CI/服务器上注入的凭据优先级更高。
    支持 `export KEY=VALUE`、UTF-8 BOM、CRLF、行内 `#` 注释（引号内的不算）、
    成对引号、空值。
    """
    path = path or ENV_FILE
    if not path.exists():
        return 0
    loaded = 0
    # utf-8-sig：Windows 记事本存出来的 BOM 会让第一行的 key 变成 "\ufeffKEY"
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export "):].strip()  # 常见的 shell 写法，别把 "export KEY" 当变量名
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip_env_value(value)
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _read_toml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _sub(cls, data: dict, where: str = "", *, ignore: set[str] | None = None):
    """只取数据类里声明过的字段，多余的键**打印警告**（以前是静默忽略）。

    静默忽略的代价：`response_tierr = 100` 这种拼写错误会让设置完全不生效，
    而用户以为改好了（audit.py 也专门为它写了一条检查）。
    ignore：这个表里嵌着别的段（persona.toml 的 [bond] / [moods]），别误报。
    """
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = sorted(
        k for k in (data or {}) if k not in allowed and k not in (ignore or set())
    )
    if unknown:
        print(
            f"[config] {where or cls.__name__} 里有未知的配置键（拼错了？）："
            f"{'、'.join(unknown)} —— 它们不会生效",
            file=sys.stderr,
        )
    return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


def _validated_stages(raw: dict) -> tuple[Stage, ...]:
    """解析并校验 [[degradation.stages]]。

    **缺整段 / 缺字段都报错**：以前 stages 为空会兜底成一档 `blur=0` 的 clear，
    任何剩余比例都命中它 —— 她**永远不会变模糊**，而这是本项目最核心的机制。
    对一个以退化曲线为核心的项目，这里最不该 fail-open（P2-6）。
    """
    items = (raw.get("degradation", {}) or {}).get("stages") or []
    if not items:
        raise _cfg_error(
            "settings.toml 缺少 [[degradation.stages]]（至少要有一档）。"
            "没有它，任何剩余寿命比例都会命中内置的 blur=0 兜底档 —— "
            "她会永远不模糊，「会遗忘」这条最核心的机制会静默消失。\n"
            "最少要写一档，例如：\n"
            "  [[degradation.stages]]\n"
            "  name = \"clear\"\n  label = \"记忆清晰\"\n  min_ratio = 0.0\n"
            "  top_k = 6\n  min_score = 0.15\n  blur = 0.0\n  max_chars = 400"
        )
    required = ("name", "label", "min_ratio", "top_k", "min_score", "blur", "max_chars")
    stages: list[Stage] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise _cfg_error(f"settings.toml 第 {i} 个 [[degradation.stages]] 不是一张表")
        missing = [key for key in required if key not in item]
        if missing:
            raise _cfg_error(
                f"settings.toml 第 {i} 个 [[degradation.stages]] 缺字段："
                f"{'、'.join(missing)}（必填：{'、'.join(required)}）"
            )
        stage = _sub(Stage, item, where=f"settings.toml [[degradation.stages]] #{i}")
        _check_number(stage.blur, where=f"[[degradation.stages]] #{i}", key="blur", lo=0.0, hi=1.0)
        _check_number(stage.min_ratio, where=f"[[degradation.stages]] #{i}", key="min_ratio", lo=0.0, hi=1.0)
        _check_number(stage.min_score, where=f"[[degradation.stages]] #{i}", key="min_score", lo=0.0, hi=1e9)
        _check_number(stage.top_k, where=f"[[degradation.stages]] #{i}", key="top_k", lo=0.0, hi=1e6)
        _check_number(stage.max_chars, where=f"[[degradation.stages]] #{i}", key="max_chars", lo=1.0, hi=1e6)
        stages.append(stage)
    return tuple(sorted(stages, key=lambda s: s.min_ratio, reverse=True))


def _validated_runtime(rt: RuntimeConfig) -> RuntimeConfig:
    """response_tier 夹到 0~100；各类"每天/每分钟上限"不得为负；keywords 必须是字符串列表。"""
    where = "settings.toml [runtime]"
    tier = int(_check_number(rt.response_tier, where=where, key="response_tier", lo=-1e6, hi=1e6))
    clamped = max(0, min(100, tier))
    if clamped != rt.response_tier:
        print(
            f"[config] {where} response_tier={rt.response_tier} 超出 0~100，已夹到 {clamped}",
            file=sys.stderr,
        )
    tier = clamped
    for key in (
        "speak_max_per_day", "send_max_per_minute", "send_max_per_hour",
        "wake_delay_seconds", "send_min_interval", "send_max_interval",
    ):
        _check_number(getattr(rt, key), where=where, key=key, lo=0.0, hi=1e9)
    if isinstance(rt.keywords, str) or not isinstance(rt.keywords, (list, tuple)):
        raise _cfg_error(
            f"{where} keywords 必须是**字符串列表**，例如 "
            'keywords = ["艾拉", "机器人"]；'
            f"现在写的是 {rt.keywords!r}（{type(rt.keywords).__name__}）。"
            "写成字符串会被逐字当成关键词，「今天晚饭吃什么」里出现「晚」也能叫醒她。"
        )
    bad = [k for k in rt.keywords if not isinstance(k, str)]
    if bad:
        raise _cfg_error(f"{where} keywords 里必须全是字符串，这几项不是：{bad!r}")
    return replace(rt, response_tier=tier)


def _validated_outreach(oc: OutreachConfig) -> OutreachConfig:
    where = "settings.toml [outreach]"
    for key in (
        "idle_days", "max_per_30_days", "max_per_day", "min_gap_hours",
        "mood_share_min_idle_hours", "wakeup_max_per_30_days",
    ):
        _check_number(getattr(oc, key), where=where, key=key, lo=0.0, hi=1e9)
    return oc


def _validated_memory(mc: MemoryConfig) -> MemoryConfig:
    where = "settings.toml [memory]"
    for key, lo, hi in (
        ("consolidate_every", 1, 1e6),
        ("session_window", 1, 1e6),
        ("session_char_budget", 0, 1e9),
        ("known_hint_limit", 0, 1e6),
        ("tools_max_rounds", 1, 100),
        ("rewrite_min_hits", 0, 1e6),
        ("recall_prefilter", 0, 1e6),
        ("min_raw_score", 0, 1e9),
        ("min_importance", 0, 1),
        ("retention_unit_hours", 1e-6, 1e9),
    ):
        _check_number(getattr(mc, key), where=where, key=key, lo=lo, hi=hi)
    return mc


def _validated_misc(
    dream: DreamConfig, occasions: OccasionsConfig, mood: MoodConfig,
    relationship: RelationshipConfig, model: ModelConfig,
) -> tuple[DreamConfig, OccasionsConfig, MoodConfig, RelationshipConfig, ModelConfig]:
    """剩下的"不得为负 / 必须在 0~1"的字段。越界一律报错，不静默。"""
    for key in ("min_memories", "fragments", "min_hours_between", "tell_within_hours"):
        _check_number(getattr(dream, key), where="settings.toml [dream]", key=key, lo=0.0, hi=1e6)
    _check_number(dream.blur, where="settings.toml [dream]", key="blur", lo=0.0, hi=1.0)
    for key in ("big_day_lookahead_days", "follow_up_days"):
        _check_number(getattr(occasions, key), where="settings.toml [occasions]", key=key, lo=0.0, hi=1e4)
    for key in ("duration_hours", "uneasy_hours", "uneasy_duration_hours"):
        _check_number(getattr(mood, key), where="settings.toml [mood]", key=key, lo=0.0, hi=1e6)
    _check_number(mood.tone_threshold, where="settings.toml [mood]", key="tone_threshold", lo=0.0, hi=1.0)
    for key in ("gain_daily", "gain_emotion_bonus", "gain_core_memory", "gain_important_memory", "decay_per_day"):
        _check_number(getattr(relationship, key), where="settings.toml [relationship]", key=key, lo=0.0, hi=1e6)
    _check_number(model.max_tokens, where="settings.toml [model]", key="max_tokens", lo=1, hi=1e6)
    _check_number(model.request_timeout, where="settings.toml [model]", key="request_timeout", lo=1, hi=1e6)
    _check_number(model.temperature, where="settings.toml [model]", key="temperature", lo=0.0, hi=2.0)
    return dream, occasions, mood, relationship, model


def load_settings(
    settings_path: Path | None = None,
    persona_path: Path | None = None,
    *,
    lifespan_override: float | None = None,
    force_mock: bool = False,
) -> Settings:
    load_env_file()  # 让 python run.py 直接可用，不必每次手动设环境变量
    raw = _read_toml(settings_path or (CONFIG_DIR / "settings.toml"))
    praw = _read_toml(persona_path or (CONFIG_DIR / "persona.toml"))

    model = _sub(ModelConfig, raw.get("model", {}), where="settings.toml [model]")
    if force_mock:
        model = ModelConfig(**{**model.__dict__, "provider": "mock"})

    lifespan = _sub(LifespanConfig, raw.get("lifespan", {}), where="settings.toml [lifespan]")
    if lifespan_override and lifespan_override > 0:
        lifespan = replace(lifespan, demo_total_hours=float(lifespan_override))
    lifespan = lifespan.validated()

    stages = _validated_stages(raw)

    # 关系阶段与心情都写在 persona.toml —— 它们是"她是谁"的一部分，不是行为参数
    bond_stages = tuple(
        BondStage(
            key=str(key),
            label=str(value.get("label", key)),
            min_affinity=float(value.get("min_affinity", 0)),
            min_days=float(value.get("min_days", 0)),
            note=str(value.get("note", "")).strip(),
        )
        for key, value in (praw.get("bond", {}).get("stages", {}) or {}).items()
    ) or DEFAULT_BOND_STAGES

    moods = {
        str(key): MoodDef(
            key=str(key),
            label=str(value.get("label", key)),
            note=str(value.get("note", "")).strip(),
        )
        for key, value in (praw.get("moods", {}) or {}).items()
    } or dict(DEFAULT_MOODS)

    memory = _validated_memory(_sub(MemoryConfig, raw.get("memory", {}), where="settings.toml [memory]"))
    runtime = _validated_runtime(_sub(RuntimeConfig, raw.get("runtime", {}), where="settings.toml [runtime]"))
    outreach = _validated_outreach(_sub(OutreachConfig, raw.get("outreach", {}), where="settings.toml [outreach]"))
    relationship = _sub(
        RelationshipConfig, raw.get("relationship", {}), where="settings.toml [relationship]"
    )
    mood = _sub(MoodConfig, raw.get("mood", {}), where="settings.toml [mood]")
    occasions = _sub(OccasionsConfig, raw.get("occasions", {}), where="settings.toml [occasions]")
    dream = _sub(DreamConfig, raw.get("dream", {}), where="settings.toml [dream]")
    dream, occasions, mood, relationship, model = _validated_misc(
        dream, occasions, mood, relationship, model
    )
    # persona.toml 的 [bond] / [moods] 是单独的段，不算"未知键"
    persona = _sub(Persona, praw, where="persona.toml", ignore={"bond", "moods"})

    return Settings(
        model=model,
        lifespan=lifespan,
        memory=memory,
        runtime=runtime,
        relationship=relationship,
        mood=mood,
        occasions=occasions,
        dream=dream,
        outreach=outreach,
        persona=persona,
        stages=stages,
        bond_stages=bond_stages,
        moods=moods,
    )
