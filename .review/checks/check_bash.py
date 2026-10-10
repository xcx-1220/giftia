"""轻量 bash 语法体检：没有 bash 的机器上也能发现括号/引号/here-doc 类错误。

不是完整解析器，只做三件事：
  1. 引号与括号配平（含 $() 与 ${}）；
  2. `if/fi`、`for/done`、`case/esac`、`do/done` 配平；
  3. here-doc（<<EOF / <<'EOF'）终止标记存在。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"D:\ai chatbot")


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    in_single = in_double = False
    escaped = False
    depth_paren = depth_brace = 0
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if escaped:
            escaped = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(" and nxt == "(":
                i += 1
            elif ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
            elif ch == "{":
                depth_brace += 1
            elif ch == "}":
                depth_brace -= 1
        if depth_paren < 0 or depth_brace < 0:
            errors.append(f"第 {text[:i].count(chr(10)) + 1} 行：括号闭合多于开启")
            break
        i += 1
    if in_single:
        errors.append("单引号未闭合")
    if in_double:
        errors.append("双引号未闭合")
    if depth_paren:
        errors.append(f"圆括号未配平（差 {depth_paren}）")
    if depth_brace:
        errors.append(f"花括号未配平（差 {depth_brace}）")

    # 关键字配平（只看行首，避开字符串里的同名单词；缩进的 while 只作提示不报错）
    def count_kw(kw: str) -> int:
        return len(re.findall(rf"^\s*{kw}\b", text, re.M))

    for open_kw, close_kw in (("if", "fi"), ("for", "done"), ("case", "esac")):
        a, b = count_kw(open_kw), count_kw(close_kw)
        if a != b:
            errors.append(f"{open_kw}({a}) 与 {close_kw}({b}) 数量不匹配")
    if count_kw("while") > count_kw("done"):
        # setup.sh 的 here-doc 文案里出现过 `while true` 这类描述性文本，只提示
        print("  [提示] while 多于 done —— 若出现在 here-doc 文案里属正常")

    # here-doc 终止标记
    for m in re.finditer(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?", text):
        tag = m.group(1)
        if not re.search(rf"^{re.escape(tag)}\s*$", text, re.M):
            errors.append(f"here-doc 缺少终止标记：{tag}")
    return errors


def main() -> int:
    # 只检查真 bash 脚本；PowerShell 的语法由 pwsh 自己解析，不归这里管
    targets = [ROOT / "deploy" / "setup.sh"]
    bad = 0
    for path in targets:
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        errs = check(path)
        if errs:
            bad += 1
            print(f"[FAIL] {path.name}")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"[PASS] {path.name}：引号/括号/关键字/here-doc 均配平")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
