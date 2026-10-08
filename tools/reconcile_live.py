"""整理她的记忆：把"换个说法记了两遍"的合并掉。

默认**只报告不修改**（dry run）。确认无误后加 --apply 才真的合并。
合并是可回溯的：重复的那些只是标记了 merged_into，数据还在库里。

用法（在服务器上，以 giftia 用户跑）：
    sudo -u giftia python3 /opt/giftia/tools/reconcile_live.py            # 看看会合并什么
    sudo -u giftia python3 /opt/giftia/tools/reconcile_live.py --apply    # 真的合并
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from giftia.brain import make_llm  # noqa: E402
from giftia.config import load_settings  # noqa: E402
from giftia.memory import reconcile  # noqa: E402
from giftia.store import Store  # noqa: E402

apply = "--apply" in sys.argv
settings = load_settings()
store = Store(settings.db_path)
llm = make_llm(settings)
instance_no = store.meta_get_int("instance_no", 1)

print(f"数据库　{settings.db_path}")
print(f"实例　　#{instance_no}　模式　{'**真的合并**' if apply else '只报告（dry run）'}")
print()

total_merged = 0
scopes: list[tuple[str, str]] = [("private", "")]
for m in store.all_memories(instance_no):
    sc = str(m.get("scope") or "private")
    sub = str(m.get("subject_id") or "")
    if (sc, sub) not in scopes:
        scopes.append((sc, sub))

for scope, subject in scopes:
    view = [("private", "")] if scope == "private" else [(scope, subject)]
    mems = store.active_memories(instance_no, view=view)
    if len(mems) < 4:
        continue
    label = "搭档（私聊）" if scope == "private" else f"{scope}:{subject[:12]}"
    print(f"── {label}　{len(mems)} 条 ──")
    result = reconcile(
        store, llm, settings,
        instance_no=instance_no, scope=scope, subject_id=subject, dry_run=not apply,
    )
    if not result["detail"]:
        print("   没有发现重复")
    for item in result["detail"]:
        print(f"   保留：{item['keep']}")
        for drop in item["drop"]:
            print(f"     合并 ← {drop}")
    total_merged += int(result["merged"])
    print()

print(f"合计{'将' if not apply else '已'}合并 {total_merged} 条")
if not apply and total_merged:
    print("确认无误后加 --apply 真的执行（可回溯，数据不会丢）")
store.close()
