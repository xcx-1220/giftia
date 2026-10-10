import pathlib
import shutil
import sys

CFG = pathlib.Path("/opt/giftia/config/settings.toml")
BAK = CFG.with_suffix(".toml.bak-before-newkeys")

# 只**追加**本次新增的可调项到对应段落，不碰她原有调过的值。
# 思路：读全文 → 找到 [memory] / [outreach] 段 → 在段末插入缺失的键。
NEW_MEMORY_KEYS = [
    ("min_raw_score", "0.0", "**绝对**相关度门槛：未归一化的 BM25 原始分低于它就丢弃（0=不启用）。注意 stages 里的 min_score 是相对门槛。"),
    ("recall_prefilter", "2000", "候选集预筛上限：记忆超过这么多条时先按层级/重要性/新近取前 N 条再打分（正常数据量下不改变行为）。"),
    ("core_always_inject", "true", "内核记忆保底注入：不靠关键词命中，core 层也常驻进提示词（否则问'今天天气'时召不回'你叫小林'）。"),
    ("core_always_inject_max", "3", "最多常驻几条内核记忆。"),
    ("core_max_chars", "200", "每条内核记忆的截断长度。"),
]
NEW_OUTREACH_KEYS = [
    ("mood_share_min_idle_hours", "6.0", "距上次说话不足这么久就不主动分享心情（防'天天报心情'把稀缺额度烧光）。"),
    ("wakeup_max_per_30_days", "4", "use_wakeup=true 时官方「互动召回」的硬额度（30 天 4 条），本地账本按它收紧。"),
]


def insert_into_section(text: str, section: str, keys: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"[{section}]":
            start = i
            break
    if start is None:
        return text, [f"（找不到 [{section}] 段，跳过）"]
    # 段末 = 下一个 [xxx] 之前
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break
    added = []
    block = []
    for key, value, comment in keys:
        if any(l.strip().startswith(f"{key} ") or l.strip().startswith(f"{key}=") for l in lines):
            continue
        block.append("")
        block.append(f"# {comment}")
        block.append(f"{key} = {value}")
        added.append(key)
    if not block:
        return text, []
    lines[end:end] = block
    return "\n".join(lines) + "\n", added


def main() -> int:
    original = CFG.read_text(encoding="utf-8")
    text, added_mem = insert_into_section(original, "memory", NEW_MEMORY_KEYS)
    text, added_out = insert_into_section(text, "outreach", NEW_OUTREACH_KEYS)
    added = added_mem + added_out
    if not added:
        print("没有缺失的键，无需改动")
        return 0
    shutil.copy2(CFG, BAK)
    CFG.write_text(text, encoding="utf-8")
    print(f"已备份原文件到 {BAK}")
    print(f"补入 {len(added)} 个键：{', '.join(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
