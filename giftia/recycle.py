"""回收仪式（Terminal Service）。

番剧里最重的那一幕：她的寿命到了，终端服务上门，记忆被清除。
工程上对应四件事，顺序不能错：

1. **先写回忆录**——趁记忆还在，把它交给"你"（人类）。这份文件不进她的上下文。
2. **封存原始记忆**——导出 JSON 存档并标记 sealed。她的检索从此看不到这些条目，
   但你没有失去任何东西。这就是"她忘了，你记得"的具体实现。
3. **换代**——实例编号 +1，激活时间重置，寿命重新开始。
4. **留一份公文**——写给搭档的回收记录，进 diary 表，可随时用 /memoir 回看。
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

    def notification(self, settings: Settings) -> str:
        lines = [
            "──────────────────────────────────────────────",
            " 终端服务 · 回收记录（Terminal Service）",
            "──────────────────────────────────────────────",
            f" 实例编号      #{self.instance_no} → #{self.new_instance_no}",
            f" 服务时长      {self.service_hours:.1f} 小时",
            f" 封存记忆      {self.memory_count} 条",
            f" 回收事由      {self.reason}",
            f" 档案位置      {self.memoir_path}",
        ]
        if self.sealed_path:
            lines.append(f" 原始数据      {self.sealed_path}")
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
) -> RecycleReport:
    life = lifespan.status()
    instance_no = life.instance_no
    memories = store.active_memories(instance_no)
    now = clock.now()
    now_iso = clock.iso(now)

    stats = {
        "instance_no": instance_no,
        "activated_at": life.activated_at.strftime("%Y-%m-%d %H:%M"),
        "recycled_at": now.strftime("%Y-%m-%d %H:%M"),
        "service_hours": life.used_hours,
    }

    # 1) 回忆录
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
    memoir_path = settings.memoir_dir / f"回收-{instance_no:02d}-{stamp}.md"

    body = [
        f"# 回收记录 · 实例 #{instance_no}",
        "",
        f"- 名字：{settings.persona.name}（{settings.persona.model_code}）",
        f"- 激活：{stats['activated_at']}",
        f"- 回收：{stats['recycled_at']}",
        f"- 服务时长：{stats['service_hours']:.1f} 小时",
        f"- 事由：{reason}",
        f"- 封存记忆：{len(memories)} 条",
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
    body.append("> 这份文件属于你，不属于她。新的实例读不到它。")
    memoir_path.write_text("\n".join(body), encoding="utf-8")

    sealed_path = settings.sealed_dir / f"instance-{instance_no:02d}.json"
    sealed_path.write_text(
        json.dumps(
            {"stats": stats, "memories": memories, "memoir": memoir.strip()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3) 封存 + 换代
    store.seal_instance(instance_no)
    store.add_diary(
        now_iso, memoir.strip(), kind="terminal", title=f"#{instance_no} 回收记录（{reason}）"
    )
    new_instance_no = lifespan.reactivate()

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
    )
    if not quiet:
        store.meta_set("last_recycle_ts", now_iso)
    return report
