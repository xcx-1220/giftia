"""配置加载：settings.toml（行为参数）+ persona.toml（人格内核）+ .env（凭据）。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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
    """记忆退化阶段。remaining_ratio 越高越清晰。"""

    name: str
    label: str
    min_ratio: float
    top_k: int
    min_score: float
    blur: float
    max_chars: int
    note: str = ""


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
    max_per_30_days: int = 4          # 对齐官方互动召回的额度
    min_gap_hours: float = 6.0
    # True = 用官方的「互动召回消息」（is_wakeup），额度更紧但更合规；
    # False = 普通主动消息（有频控，没有 30 天 4 条的硬限制）
    use_wakeup: bool = False
    # 主动消息发给谁：owner（私聊给搭档，默认）| group（发到最后活跃的群）| both
    # 注意：发到群里时只会用"大家都看得见"的理由（大日子/纪念日），
    # 梦、心事、"快到终端服务了"这类只在私聊说。
    target: str = "owner"


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

    # 检索细节
    relative_cutoff: float = 0.5
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
def load_env_file(path: Path | None = None, *, override: bool = False) -> int:
    """读取 .env（零依赖的 KEY=VALUE 解析）。返回载入的条目数。

    已经存在的环境变量默认不被覆盖——这样 CI/服务器上注入的凭据优先级更高。
    支持 # 注释、空行、以及值两端可有可无的引号。
    """
    path = path or ENV_FILE
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
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


def _sub(cls, data: dict):
    """只取数据类里声明过的字段，多余的键忽略（方便以后加注释/实验字段）。"""
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


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

    model = _sub(ModelConfig, raw.get("model", {}))
    if force_mock:
        model = ModelConfig(**{**model.__dict__, "provider": "mock"})

    lifespan = _sub(LifespanConfig, raw.get("lifespan", {}))
    if lifespan_override and lifespan_override > 0:
        lifespan = LifespanConfig(
            **{**lifespan.__dict__, "demo_total_hours": float(lifespan_override)}
        )

    stages = tuple(
        sorted(
            (_sub(Stage, s) for s in raw.get("degradation", {}).get("stages", [])),
            key=lambda s: s.min_ratio,
            reverse=True,
        )
    )
    if not stages:
        stages = (
            Stage("clear", "记忆清晰", 0.0, 6, 0.15, 0.0, 400, "记忆清晰。"),
        )

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

    return Settings(
        model=model,
        lifespan=lifespan,
        memory=_sub(MemoryConfig, raw.get("memory", {})),
        runtime=_sub(RuntimeConfig, raw.get("runtime", {})),
        relationship=_sub(RelationshipConfig, raw.get("relationship", {})),
        mood=_sub(MoodConfig, raw.get("mood", {})),
        occasions=_sub(OccasionsConfig, raw.get("occasions", {})),
        dream=_sub(DreamConfig, raw.get("dream", {})),
        outreach=_sub(OutreachConfig, raw.get("outreach", {})),
        persona=_sub(Persona, praw),
        stages=stages,
        bond_stages=bond_stages,
        moods=moods,
    )
