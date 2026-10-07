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
    unit_label: str = "小时"
    terminal_warning_hours: float = 72.0

    def effective_hours(self, override: float | None = None) -> float:
        """实际寿命：命令行覆盖 > demo > 正式设定。"""
        if override and override > 0:
            return float(override)
        if self.demo_total_hours > 0:
            return float(self.demo_total_hours)
        return float(self.total_hours)


@dataclass(frozen=True)
class MemoryConfig:
    consolidate_every: int = 8
    session_window: int = 24
    session_char_budget: int = 6000
    recall_candidates: int = 40
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
    handler_name: str = "搭档"
    user_id: str = "handler"
    show_memoir_after_recycle: bool = True


@dataclass(frozen=True)
class Persona:
    name: str = "艾拉"
    model_code: str = "Giftia-Type-07"
    handler_title: str = "搭档"
    core: str = ""
    opening_line: str = "……请多指教。"
    greeting_line: str = "……你来了。"
    after_recycle_line: str = "……初次见面。"
    out_of_character_hint: str = ""

    def core_text(self) -> str:
        """把 {name} / {model_code} / {handler_title} 占位符填好。"""
        return _fill(self.core, self)

    def hint_text(self) -> str:
        return _fill(self.out_of_character_hint, self)

    def line(self, key: str) -> str:
        return _fill(getattr(self, key, "") or "", self)


def _fill(text: str, p: "Persona") -> str:
    return (
        (text or "")
        .replace("{name}", p.name)
        .replace("{model_code}", p.model_code)
        .replace("{handler_title}", p.handler_title)
    )


@dataclass(frozen=True)
class Settings:
    model: ModelConfig = field(default_factory=ModelConfig)
    lifespan: LifespanConfig = field(default_factory=LifespanConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    persona: Persona = field(default_factory=Persona)
    stages: tuple[Stage, ...] = ()
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

    return Settings(
        model=model,
        lifespan=lifespan,
        memory=_sub(MemoryConfig, raw.get("memory", {})),
        runtime=_sub(RuntimeConfig, raw.get("runtime", {})),
        persona=_sub(Persona, praw),
        stages=stages,
    )
