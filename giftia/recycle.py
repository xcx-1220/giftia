"""回收仪式（Terminal Service）。

番剧里最重的那一幕：她的寿命到了，终端服务上门，记忆被清除。
工程上对应四件事，顺序不能错：

1. **先写回忆录**——趁记忆还在，把它交给"你"（人类）。这份文件不进她的上下文。
2. **封存原始记忆**——导出 JSON 存档并标记 sealed。她的检索从此看不到这些条目，
   但你没有失去任何东西。这就是"她忘了，你记得"的具体实现。
3. **换代**——实例编号 +1，激活时间重置，寿命重新开始。
4. **留一份公文**——写给搭档的回收记录，进 diary 表，可随时用 /memoir 回看。

这份文件里有两个**不同归属**的产物，别混：

- **回忆录 `.md` 与 diary 公文**：交给搭档的东西，只写"你和她的事"
  （`("private", "")` + `("person", handler_id)`），别人的私聊与群里的事不进这里。
- **封存 `封存/instance-NN.json`**：运维资产，保留**全量原始数据**（所有归属），
  所以文件里显式写了 `"scope": "all_scopes"` 与 `"note"`，命名也带时间戳、绝不覆盖旧档案。

崩溃窗口（P1-20）：写文件 → `seal_instance` → `add_diary` → `reactivate` 之间任何一步挂掉，
库里会出现"记忆已封存、实例号没换代"的撕裂状态，重启后寿命仍到期 → 二次回收。
所以回收开始前先写 `meta:recycling_instance=<n>` 标记；下次进来看到同一实例的标记时，
**续跑换代**而不是重新生成档案（旧档案同名文件永不被覆盖）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .clock import Clock
from .config import Settings
from .lifespan import Lifespan
from .persona import after_recycle_line
from .store import Store

try:  # team-store 的显式全量哨兵（接口契约 §一：模块级 ALL_SCOPES = "all"）
    from .store import ALL_SCOPES
except ImportError:  # pragma: no cover - 过渡期兼容
    ALL_SCOPES = None  # type: ignore[assignment]

# 封存档案的显式标记：这份 JSON 是全量运维数据，不是给搭档或第三方看的东西。
SEALED_SCOPE = "all_scopes"
SEALED_NOTE = (
    "含全部归属的原始记忆（private / person / group），属于运维资产，"
    "不是给搭档或任何第三方看的东西；"
    "给搭档的回忆录（回收日志 .md 与 /memoir）只包含他的视角。"
)
# "正在回收第 N 任"的标记键；崩溃后靠它续跑换代，而不是重新生成档案。
RECYCLING_META_KEY = "recycling_instance"


def _handler_view(store: Store) -> list[tuple[str, str]]:
    """搭档视角：他自己的私事 + 他本人的私聊。

    **不含 `("group", ...)`**——群里的事不属于"你和她"的回忆录，
    更不能出现在交给搭档的回忆录里（P0-1）。
    和 brain.memory_view / dream.make_dream 的默认视角保持同一口径。
    """
    handler_id = str(store.state_get("handler_id", "") or "")
    view: list[tuple[str, str]] = [("private", "")]
    if handler_id:
        view.append(("person", handler_id))
    return view


def _all_scopes(store: Store) -> str | None:
    """显式全量的哨兵值：`store.ALL_SCOPES`（模块级常量 `"all"`）。

    封存 JSON 是运维资产，要的就是**全量**，所以这里必须显式要全量、而不是省略参数。
    过渡期兼容：若 store 尚未落地该常量，返回 `None`（旧契约的"不过滤"，语义等价）。
    """
    return getattr(store, "ALL_SCOPES", None) or ALL_SCOPES


def _unique_path(path: Path) -> Path:
    """绝不覆盖已存在的文件：同名就退让成 `xxx-2.json`、`xxx-3.json`…"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(2, 1000):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"同名档案太多，无法为 {path.name} 找到不冲突的名字")


def _resolve_sealed_path(settings: Settings, name: str) -> Path | None:
    """把标记里记下的封存文件名解析回路径（只认 `封存/` 目录里的文件）。"""
    if not name:
        return None
    candidate = Path(str(name))
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    candidate = settings.sealed_dir / candidate.name
    return candidate if candidate.exists() else None


@dataclass
class RecycleReport:
    instance_no: int
    new_instance_no: int
    memory_count: int
    service_hours: float
    reason: str
    memoir: str
    memoir_path: Path
    sealed_path: Path | None
    new_greeting: str
    sealed_count: int = 0                      # 封存 JSON 里的条数（全量原始数据）
    sealed_scope: str = SEALED_SCOPE           # 那份 JSON 的归属口径，显式写在文件里
    resumed: bool = False                      # 这次是崩溃后续跑换代（没有重新生成档案）

    def notification(self, settings: Settings) -> str:
        lines = [
            "──────────────────────────────────────────────",
            " 终端服务 · 回收记录（Terminal Service）",
            "──────────────────────────────────────────────",
            f" 实例编号      #{self.instance_no} → #{self.new_instance_no}",
            f" 服务时长      {self.service_hours:.1f} 小时",
            f" 回忆录记忆    {self.memory_count} 条（只看「你和她」的视角）",
            f" 回收事由      {self.reason}",
            f" 档案位置      {self.memoir_path}",
        ]
        if self.sealed_path:
            lines.append(
                f" 原始数据      {self.sealed_path}"
                f"（{self.sealed_count} 条，含全部归属的运维存档）"
            )
        lines.append("──────────────────────────────────────────────")
        if settings.runtime.show_memoir_after_recycle:
            excerpt = self.memoir.strip()
            if len(excerpt) > 420:
                excerpt = excerpt[:420].rstrip() + "……"
            lines.append("")
            lines.append(excerpt)
        return "\n".join(lines)


FALLBACK_MEMOIR = (
    "档案编号已归档。这名 Giftia 的服务期到此为止。\n\n"
    "她留下的记忆条目已经全部封存，原件交给你保管。\n"
    "在最后那段时间里，她已经叫不出你的名字，但她一直知道，身边这个人很重要。\n\n"
    "她的记忆将被清除，而你会记得。"
)


def _memoir_context(stats: dict, memories: list[dict], name: str) -> str:
    lines = [
        f"型号与名字：{name}",
        f"实例编号：#{stats['instance_no']}",
        f"激活时间：{stats['activated_at']}",
        f"回收时间：{stats['recycled_at']}",
        f"服务时长：{stats['service_hours']:.1f} 小时",
        f"记忆条目数：{len(memories)}",
        "",
        "她的记忆条目：",
    ]
    for m in memories:
        emotion = f"（{m['emotion']}）" if m.get("emotion") else ""
        lines.append(f"- {str(m['happened_ts'])[:16]}：{m['summary']}{emotion}")
    if not memories:
        lines.append("- （她还没来得及留下任何记忆条目）")
    return "\n".join(lines)


def _sealed_json(stats: dict, memories: list[dict], memoir: str) -> str:
    """封存档案的正文：全量原始数据 + 显式的口径标记（运维资产）。"""
    return json.dumps(
        {
            "scope": SEALED_SCOPE,
            "note": SEALED_NOTE,
            "memory_count": len(memories),
            "stats": stats,
            "memories": memories,
            "memoir": memoir,
        },
        ensure_ascii=False,
        indent=2,
    )


def _recycling_marker(store: Store) -> dict:
    try:
        raw = str(store.meta_get(RECYCLING_META_KEY, "") or "").strip()
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resume_report(
    settings: Settings, store: Store, lifespan: Lifespan, now_iso: str
) -> RecycleReport | None:
    """如果上一任回收在 `reactivate` 之前崩了：**续跑换代**，不重新生成档案。

    返回 None 表示"没有可续跑的回收"，调用方走正常流程。
    只有"标记存在 + 封存档案真的还在"才算可续跑；否则清掉陈旧标记、重新来过。
    """
    marker = _recycling_marker(store)
    if not marker:
        return None
    try:
        old_instance = int(marker.get("instance_no") or 0)
    except (TypeError, ValueError):
        old_instance = 0
    life = lifespan.status()
    if old_instance <= 0 or int(life.instance_no) != old_instance:
        # 标记是更早那次的残留（实例已经换代过了），清掉别挡住下一任。
        store.meta_set(RECYCLING_META_KEY, "")
        return None

    sealed_path = _resolve_sealed_path(settings, str(marker.get("sealed_path") or ""))
    if sealed_path is None:
        # 崩在"写档案"之前：库里这时还是"未封存 + 未换代"，重来一遍是安全的。
        store.meta_set(RECYCLING_META_KEY, "")
        return None

    reason = str(marker.get("reason") or "寿命结束")
    memoir = str(marker.get("memoir") or "").strip()
    new_instance_no = lifespan.reactivate()

    # 公文可能已经写过了（崩在 add_diary 之后）：有就不重复写。
    title = f"#{old_instance} 回收记录（{reason}）"
    already = any(
        str(row.get("title") or "") == title for row in store.list_diary(kind="terminal", limit=50)
    )
    if not already:
        store.add_diary(now_iso, memoir, kind="terminal", title=title)
    store.meta_set(RECYCLING_META_KEY, "")
    store.meta_set("last_recycle_ts", now_iso)

    return RecycleReport(
        instance_no=old_instance,
        new_instance_no=new_instance_no,
        memory_count=int(marker.get("memory_count") or 0),
        service_hours=float(marker.get("service_hours") or 0.0),
        reason=reason,
        memoir=memoir,
        memoir_path=Path(str(marker.get("memoir_path") or settings.memoir_dir)),
        sealed_path=sealed_path,
        new_greeting=after_recycle_line(settings),
        sealed_count=int(marker.get("sealed_count") or 0),
        resumed=True,
    )


def perform_recycle(
    settings: Settings,
    store: Store,
    clock: Clock,
    llm,
    lifespan: Lifespan,
    *,
    reason: str = "寿命结束",
    quiet: bool = False,
    extra_context: str = "",
    view: list[tuple[str, str]] | str | None = None,
) -> RecycleReport:
    """回收这一任的她。

    `view` = **回忆录 / diary 公文**读到谁的事：

    - 不传（推荐）→ 搭档视角 `[("private",""), ("person", handler_id)]`，**不含群**；
    - 传列表 → 用你给的白名单视角；
    - `store.ALL_SCOPES` → 显式全量（只给诊断/迁移用；封存 JSON 本来就是全量，
      与这个参数无关）。

    封存 JSON 恒定保留全量原始数据（带 `"scope": "all_scopes"` 标记），
    因为它是运维资产，不是交给搭档的回忆录。
    """
    now = clock.now()
    now_iso = clock.iso(now)

    # 0) 崩溃续跑：上一次同一实例的回收在换代前挂掉了？
    resumed = _resume_report(settings, store, lifespan, now_iso)
    if resumed is not None:
        if not quiet:
            store.meta_set("last_recycle_ts", now_iso)
        return resumed

    life = lifespan.status()
    instance_no = life.instance_no
    if view is None:
        memoir_view: list[tuple[str, str]] | str = _handler_view(store)
    else:
        memoir_view = view
    memories = store.active_memories(instance_no, view=memoir_view)
    sealed_memories = store.active_memories(instance_no, view=_all_scopes(store))

    stats = {
        "instance_no": instance_no,
        "activated_at": life.activated_at.strftime("%Y-%m-%d %H:%M"),
        "recycled_at": now.strftime("%Y-%m-%d %H:%M"),
        "service_hours": life.used_hours,
    }

    # 1) 回忆录（只写"你和她的事"）
    context = _memoir_context(stats, memories, settings.persona.name)
    if extra_context.strip():
        context = extra_context.strip() + "\n\n" + context
    try:
        memoir = llm.write_memoir(context)
    except Exception:
        memoir = FALLBACK_MEMOIR
    if not memoir.strip():
        memoir = FALLBACK_MEMOIR

    # 2) 落盘：给人看的回忆录 + 给机器看的原始数据
    settings.memoir_dir.mkdir(parents=True, exist_ok=True)
    settings.sealed_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    # 回忆录文件名精确到秒，同一秒重跑会撞名 → 走 _unique_path 永不覆盖。
    memoir_path = _unique_path(settings.memoir_dir / f"回收-{instance_no:02d}-{stamp}.md")

    body = [
        f"# 回收记录 · 实例 #{instance_no}",
        "",
        f"- 名字：{settings.persona.name}（{settings.persona.model_code}）",
        f"- 激活：{stats['activated_at']}",
        f"- 回收：{stats['recycled_at']}",
        f"- 服务时长：{stats['service_hours']:.1f} 小时",
        f"- 事由：{reason}",
        f"- 记忆条目：{len(memories)} 条（只含「你和她」的视角："
        f"你的私事 + 你的私聊；群里的事与别人的私聊不进这份回忆录）",
    ]
    if extra_context.strip():
        # 关系状态作为**事实**写进档案，而不是只丢给模型自由发挥
        body += ["", "## 关系", "", extra_context.strip()]
    body += [
        "",
        "## 回忆录",
        "",
        memoir.strip(),
        "",
        "## 原始记忆条目（未模糊）",
        "",
    ]
    if memories:
        for m in memories:
            body.append(
                f"- `{str(m['happened_ts'])[:16]}`（重要性 {float(m['importance']):.2f}"
                f"{'，' + m['emotion'] if m.get('emotion') else ''}）{m['summary']}"
            )
    else:
        body.append("- （无）")
    body.append("")
    body.append(
        f"> 这份文件属于你，不属于她。新的实例读不到它。"
        f"（全量原始数据含其它归属，共 {len(sealed_memories)} 条，在封存档案里："
        f"`{SEALED_SCOPE}`）"
    )
    memoir_path.write_text("\n".join(body), encoding="utf-8")

    # 封存档案：带时间戳、**已存在就改名**，绝不覆盖上一份（P1-20）。
    sealed_path = _unique_path(
        settings.sealed_dir / f"instance-{instance_no:02d}-{stamp}.json"
    )
    sealed_path.write_text(
        _sealed_json(stats, sealed_memories, memoir.strip()), encoding="utf-8"
    )

    # 3) 标记"正在回收" → 封存 + 换代（崩在中间就靠这个标记续跑）
    store.meta_set(
        RECYCLING_META_KEY,
        json.dumps(
            {
                "instance_no": instance_no,
                "sealed_path": sealed_path.name,
                "memoir_path": str(memoir_path),
                "memory_count": len(memories),
                "sealed_count": len(sealed_memories),
                "service_hours": life.used_hours,
                "reason": reason,
                "memoir": memoir.strip(),
                "started_at": now_iso,
            },
            ensure_ascii=False,
        ),
    )
    store.seal_instance(instance_no)
    store.add_diary(
        now_iso, memoir.strip(), kind="terminal", title=f"#{instance_no} 回收记录（{reason}）"
    )
    new_instance_no = lifespan.reactivate()
    store.meta_set(RECYCLING_META_KEY, "")

    report = RecycleReport(
        instance_no=instance_no,
        new_instance_no=new_instance_no,
        memory_count=len(memories),
        service_hours=life.used_hours,
        reason=reason,
        memoir=memoir.strip(),
        memoir_path=memoir_path,
        sealed_path=sealed_path,
        new_greeting=after_recycle_line(settings),
        sealed_count=len(sealed_memories),
    )
    if not quiet:
        store.meta_set("last_recycle_ts", now_iso)
    return report
