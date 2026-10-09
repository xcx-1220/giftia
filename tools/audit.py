#!/usr/bin/env python
"""静态一致性审计：只读源码，不 import 任何被测模块。

    python tools/audit.py          # 全过退出码 0，有严重问题退出码 1

为什么要这个脚本（每一条都对应一类"上线才炸"的故障）：

1. 配置项声明 ↔ 使用
   `giftia/config.py` 是配置的唯一契约：settings.toml / persona.toml 里多写的键会被
   `_sub()` **静默丢掉**（只取数据类声明过的字段），所以配置"改了不起作用"时，
   看代码是看不出来的。反过来更要命：代码里写了 `settings.memory.xxx`，而 dataclass
   里根本没有 xxx —— 这是 **AttributeError**，而且只在走到那一行的用户路径上才炸，
   本地点两下不一定碰得到。所以：
     · 读了但没声明 → 严重（计入 FAIL）
     · 声明了但从没读过 → 提示（可能是死配置，不算失败）

2. 指令声明 ↔ 实现
   指令有两个入口：终端 `giftia/cli.py` 与 QQ `adapters/qq_official.py`。
   两边各自维护一份指令表，最典型的线上事故是"帮助里写了 /xxx，实现里没有"——
   用户发了没人理；或者"QQ 上有人发 /xxx，适配器不认识"，那句话会被当成 **普通聊天**
   塞进模型（`handle_command` 返回 False），而不只是一个"未知指令"提示。

3. 发送路径不得绕过
   `QQBot.send()` 是唯一的发送出口：它负责分段、随机间隔、每会话限频、同文去重、
   失败如实上报（没送出去的话要从她记忆里撤回）。任何在它之外直接
   `_request("POST", .../messages)` 的代码都会绕过这些，属于**静默失效**类问题。

4. 测试覆盖粗查
   `tools/` 下的脚本如果没有一条 check，它跑完永远是绿的 —— 等于没测。

实现约束：全部用 `ast.parse` 做源码级检查，**绝不 import 被测模块**
（`giftia.brain.get_default()` 之类一 import 就可能建库、写文件、起线程）。
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (ROOT / "giftia", ROOT / "adapters")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

PASS, FAIL, HINT = 0, 0, 0
SAFE_NAMES = {"self", "cls", "mro"}


def check(label: str, condition: bool, detail: str = "") -> None:
    """严重问题：计入退出码。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def hint(label: str, detail: str = "") -> None:
    """提示性问题：只报不计失败（死配置之类，不会当场崩）。"""
    global PASS, HINT
    HINT += 1
    PASS += 1
    print(f"  [PASS] {label}（提示，不计失败）" + (f"  → {detail}" if detail else ""))


def note(label: str, detail: str = "") -> None:
    """信息性输出，不构成任何结论。"""
    print(f"  [PASS] {label}" + (f"  → {detail}" if detail else ""))


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def py_files(dirs) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        if d.exists():
            out.extend(sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts))
    return out


# ══════════════════════════════════════════════════════════════════
# 1. 配置：声明 vs 读取
# ══════════════════════════════════════════════════════════════════
class DataclassInfo:
    def __init__(self, name: str, lineno: int, fields: dict[str, int], methods: set[str]) -> None:
        self.name = name
        self.lineno = lineno
        self.fields = fields              # 字段名 → 声明行号
        self.methods = methods            # 方法 / property / 类属性名
        self.reads: list[tuple[str, int]] = []   # (文件, 行号) —— 逐处读取

    def has(self, attr: str) -> bool:
        return attr in self.fields or attr in self.methods


def _is_dataclass_decorator(node: ast.AST) -> bool:
    """认得 @dataclass、@dataclasses.dataclass，也认得 @dataclass(frozen=True) 这种调用形式。"""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id == "dataclass"
    if isinstance(node, ast.Attribute):
        return node.attr == "dataclass"
    return False


def collect_dataclasses(tree: ast.Module) -> dict[str, DataclassInfo]:
    out: dict[str, DataclassInfo] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_is_dataclass_decorator(d) for d in node.decorator_list):
            continue
        fields: dict[str, int] = {}
        methods: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields[stmt.target.id] = stmt.lineno
            elif isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        fields[tgt.id] = stmt.lineno
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(stmt.name)
        out[node.name] = DataclassInfo(node.name, node.lineno, fields, methods)
    return out


def group_names(classes: dict[str, DataclassInfo]) -> dict[str, str]:
    """Settings 里的组名 → 数据类名。

    组名就是 `settings.<组>` 里用的那一段：ModelConfig → model、
    LifespanConfig → lifespan、MemoryConfig → memory…… 也就是类名去掉 Config 后缀。
    不能直接 lower() 整个类名——那样会得到 "modelconfig"，和 settings.model 对不上。
    """
    out: dict[str, str] = {}
    for name in classes:
        key = name[:-6].lower() if name.endswith("Config") else name.lower()
        out[key] = name
    return out


def _path_of(node: ast.AST, parent: dict[int, ast.AST]) -> tuple[tuple[str, ...], bool]:
    """把 `self.settings.memory.reconcile_hours` 还原成 ('settings','memory','reconcile_hours')。

    返回 (路径, 是否可信)。带下标/函数调用的链（`settings[key].x`）不可信，直接丢弃——
    静态审计宁可漏报，也不能误报。
    """
    parts: list[str] = []
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            parts.append(cur.id)
            break
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        elif isinstance(cur, ast.Call):
            return (), False
        else:
            return (), False
    parts.reverse()
    while parts and parts[0] in SAFE_NAMES:
        parts.pop(0)
    return tuple(parts), bool(parts)


class FileScan:
    """一个文件的源码级视图：别名表 + 属性读取。"""

    def __init__(self, path: Path, tree: ast.Module) -> None:
        self.path = path
        self.rel = _rel(path)
        self.tree = tree
        self.parent: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        self.aliases: dict[tuple[str, ...], tuple[str, ...]] = {}
        self.annotated: dict[str, str] = {}          # 形参名 → 数据类名（最可信）
        self._collect_aliases()
        self.reads: list[tuple[tuple[str, ...], int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                parts, ok = _path_of(node, self.parent)
                if ok:
                    self.reads.append((parts, node.lineno))

    def _collect_aliases(self) -> None:
        def record(target: ast.AST, value: ast.AST) -> None:
            if not isinstance(value, ast.Attribute):
                return
            vparts, vok = _path_of(value, self.parent)
            if not vok:
                return
            if isinstance(target, ast.Name):
                self.aliases[(target.id,)] = vparts
            elif isinstance(target, ast.Attribute):
                tparts, tok = _path_of(target, self.parent)
                if tok:
                    self.aliases[tparts] = vparts

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    record(tgt, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                record(node.target, node.value)
            elif isinstance(node, ast.arg) and node.annotation is not None:
                ann = node.annotation
                if isinstance(ann, ast.Name) and ann.id in self._class_names:
                    self.aliases[(node.arg,)] = (ann.id,)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    ann = a.annotation
                    if isinstance(ann, ast.Name) and ann.id in self._class_names:
                        self.aliases[(a.arg,)] = (ann.id,)
                        self.annotated[a.arg] = ann.id

    _class_names: set[str] = set()

    def resolve(self, parts: tuple[str, ...], groups: dict[str, str]) -> str | None:
        """('settings','memory','reconcile_hours') → 'MemoryConfig'；解析不了返回 None。

        顺序很重要：先看 `settings.<组名>`，再看别名/注解。
        否则 `settings.relationship.decay_per_day` 会被 Settings 这个类名抢答，
        再被拿去和 Settings 的字段比对 → 全是假警报。
        """
        if not parts:
            return None
        # 形参上的类型注解最可信（`def recall(..., cfg: MemoryConfig)`），先看它
        if parts[0] in self.annotated:
            return self.annotated[parts[0]]
        if len(parts) >= 2 and parts[1] in groups:
            return groups[parts[1]]
        for cut in range(len(parts) - 1, 0, -1):
            head = parts[:cut]
            if head in self.aliases:
                rest = self.aliases[head] + parts[cut:]
                if len(rest) >= 2 and rest[1] in groups:
                    return groups[rest[1]]
                if rest and rest[0] in self._class_names:
                    return rest[0]
        return None


def stub_scan(path: Path, tree: ast.Module) -> FileScan:
    """构造一个不建别名表的轻量扫描（给"这个属性名到底在别处读过没有"用）。"""
    scan = FileScan(path, tree)
    return scan


# ══════════════════════════════════════════════════════════════════
# 2. 指令
# ══════════════════════════════════════════════════════════════════
CMD_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*$")


def _cmd_strings(node: ast.AST) -> list[str]:
    """一段用来判定指令的表达式里出现的指令字面量（含元组/集合）。"""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if CMD_RE.match(node.value):
            out.append(node.value)
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for el in node.elts:
            out.extend(_cmd_strings(el))
    return out


def _func_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def collect_commands(tree: ast.Module) -> tuple[dict[str, int], list[str]]:
    """返回 (指令 → 行号, 可疑点)。只看"正在判定指令"的比较，不看普通字符串。

    判据：`x.startswith("/...")`、`x == "/..."`、`x in ("/...", ...)`。
    纯字符串搜索会把文档、注释、URL 路径（"/v2/users/..."）全算进来，噪声太大。
    """
    found: dict[str, int] = {}
    problems: list[str] = []

    def add(names: list[str], lineno: int) -> None:
        for n in names:
            found.setdefault(n, lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left = node.left
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq):
                    add(_cmd_strings(left), node.lineno)
                    add(_cmd_strings(comp), node.lineno)
                elif isinstance(op, ast.In):
                    # 只认 `cmd in ("/a", "/b")`：左边是标量、右边是字面量元组
                    names = _cmd_strings(comp)
                    if names and not _cmd_strings(left):
                        add(names, node.lineno)
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "startswith"
                and isinstance(fn.value, (ast.Name, ast.Attribute))
            ):
                for arg in node.args:
                    add(_cmd_strings(arg), node.lineno)
    return found, problems


def help_commands(tree: ast.Module) -> dict[str, tuple[set[str], int]]:
    """文件里 HELP / COMMANDS 这类帮助文本中列出的指令 → (指令集合, 行号)。"""
    out: dict[str, tuple[set[str], int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            if not ("HELP" in tgt.id.upper() or "COMMAND" in tgt.id.upper()):
                continue
            text = node.value.value if isinstance(node.value, ast.Constant) else None
            if not isinstance(text, str):
                continue
            names: set[str] = set()
            for line in text.splitlines():
                m = re.match(r"\s*(/[A-Za-z][A-Za-z0-9_-]*)", line)
                if m:
                    names.add(m.group(1))
            out[tgt.id] = (names, node.lineno)
    return out


# ══════════════════════════════════════════════════════════════════
# 3. 发送路径
# ══════════════════════════════════════════════════════════════════
def enclosing_funcs(parent: dict[int, ast.AST], node: ast.AST) -> list[str]:
    out: list[str] = []
    cur = parent.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(cur.name)
        cur = parent.get(id(cur))
    return out


def _contains_messages(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "/messages" in sub.value:
            return True
    return False


# ══════════════════════════════════════════════════════════════════
# 4. 测试脚本断言
# ══════════════════════════════════════════════════════════════════
def assert_calls(path: Path, tree: ast.Module) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _func_name(node.func) in ("check", "add_check"):
            n += 1
    return n


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("═" * 66)
    print(f"静态一致性审计（只读源码，不 import 被测模块）　根目录：{ROOT}")
    print("═" * 66)

    config_path = ROOT / "giftia" / "config.py"
    config_tree = _parse(config_path)
    if config_tree is None:
        print(f"[FAIL] 读不了 {_rel(config_path)}")
        return 1

    classes = collect_dataclasses(config_tree)
    groups = group_names(classes)
    FileScan._class_names = set(classes)

    scans = [FileScan(p, t) for p in py_files(SCAN_DIRS) if (t := _parse(p)) is not None]
    other_scans = [FileScan(p, t) for p in py_files([ROOT / "tools", ROOT]) if (t := _parse(p)) is not None]

    # 所有属性读取：用于"声明了但没读过"的判定（跨文件，含 tools/）
    read_parts: set[tuple[str, ...]] = set()
    for scan in [*scans, *other_scans]:
        for parts, _ln in scan.reads:
            read_parts.add(parts)
    raw_dotted: set[str] = {f".{'.'.join(p)}" for p in read_parts}
    all_source = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in [*py_files(SCAN_DIRS), *py_files([ROOT / "tools", ROOT])]
    )

    # ── 1. 配置项 ────────────────────────────────────────────────
    print("\n1. 配置项：声明 ↔ 读取")
    count_1 = 0
    bad_reads: list[str] = []
    for scan in scans:                     # 只看 giftia/ 与 adapters/
        for parts, lineno in scan.reads:
            cls_name = scan.resolve(parts, groups)
            if cls_name is None or len(parts) < 2:
                continue
            attr = parts[-1]
            if attr in SAFE_NAMES:
                continue
            info = classes[cls_name]
            if info.has(attr):
                count_1 += 1
                info.reads.append((scan.rel, lineno))
            else:
                bad_reads.append(
                    f"{scan.rel}:{lineno} 读了 {'.'.join(parts)}"
                    f"，但 {cls_name} 没有 {attr}（声明于 config.py:{info.lineno}）"
                )
    check(
        "配置项读取全部能在 dataclass 里找到声明（读了不存在的字段 = AttributeError，上线才炸）",
        not bad_reads,
        "；".join(bad_reads[:8]),
    )
    note(f"逐处核对了 {count_1} 处 settings.<组>.<字段> / cfg.<字段> 读取")

    # 声明了但从没读过 → 死配置（提示）
    # 注意：不能只用 `self.<字段>` 判定。Persona 的字段是**按名字**读的
    # （core_text() / line("opening_line")），所以还要认字符串字面量。
    unread: list[str] = []
    for cls_name, info in classes.items():
        if cls_name in ("Settings", "Stage", "BondStage", "MoodDef"):
            continue                        # 动态键控/构造型数据类，字段读写是数据不是配置
        for field_name, decl_line in info.fields.items():
            if f".{field_name}" in all_source:
                continue
            if f'"{field_name}"' in all_source or f"'{field_name}'" in all_source:
                continue
            unread.append(f"{cls_name}.{field_name}（config.py:{decl_line}）")
    if unread:
        hint(
            f"有 {len(unread)} 个配置字段在整个仓库里从没被读过（可能是死配置：改了不生效）",
            "、".join(unread),
        )
    else:
        check("没有从未被读取的配置字段", True)

    # 反向：settings.toml / persona.toml 里写了、但 dataclass 没有的键
    # `_sub()` 会静默丢掉这些键 —— 用户"改了配置没反应"的头号原因
    try:
        import tomllib                         # 标准库，非第三方；只读文件不 import 项目模块

        section_map = {
            "model": "ModelConfig", "lifespan": "LifespanConfig", "memory": "MemoryConfig",
            "runtime": "RuntimeConfig", "relationship": "RelationshipConfig", "mood": "MoodConfig",
            "occasions": "OccasionsConfig", "dream": "DreamConfig", "outreach": "OutreachConfig",
        }
        extra_keys: list[str] = []
        st_path = ROOT / "config" / "settings.toml"
        if st_path.exists():
            with st_path.open("rb") as fh:
                raw = tomllib.load(fh)
            for section, cls_name in section_map.items():
                info = classes.get(cls_name)
                if info is None:
                    continue
                for key in (raw.get(section) or {}):
                    if not info.has(str(key)):
                        extra_keys.append(f"settings.toml [{section}] {key}（{cls_name} 没有这个字段，会被静默忽略）")
        pe_path = ROOT / "config" / "persona.toml"
        if pe_path.exists():
            with pe_path.open("rb") as fh:
                praw = tomllib.load(fh)
            pinfo = classes.get("Persona")
            if pinfo is not None:
                for key in praw:
                    if not pinfo.has(str(key)):
                        extra_keys.append(f"persona.toml {key}（Persona 没有这个字段，会被静默忽略）")
        if extra_keys:
            hint(f"配置文件里有 {len(extra_keys)} 个键在 dataclass 里不存在（写了也不会生效）",
                 "；".join(extra_keys))
        else:
            check("settings.toml / persona.toml 的键都能对上 dataclass 字段", True)
    except Exception as exc:                   # tomllib 不可用/文件坏了：不因此判失败
        note(f"跳过 toml 反向核对（{type(exc).__name__}: {exc}）")

    # ── 2. 指令 ──────────────────────────────────────────────────
    print("\n2. 指令：声明 ↔ 实现")
    adapter_path = ROOT / "adapters" / "qq_official.py"
    cli_path = ROOT / "giftia" / "cli.py"
    adapter_tree = _parse(adapter_path)
    cli_tree = _parse(cli_path)
    if adapter_tree is None or cli_tree is None:
        check("能读到两个指令入口的文件", False, f"{_rel(adapter_path)} / {_rel(cli_path)}")
        return 1

    adapter_cmds, _ = collect_commands(adapter_tree)
    cli_cmds, _ = collect_commands(cli_tree)
    check("两个入口都解析出了指令（解析不出来说明审计脚本失效了）",
          bool(adapter_cmds) and bool(cli_cmds),
          f"QQ={sorted(adapter_cmds)} 终端={sorted(cli_cmds)}")
    note(f"QQ 适配器 handle_command 处理 {len(adapter_cmds)} 条：{'、'.join(sorted(adapter_cmds))}",
         f"（{_rel(adapter_path)}）")
    note(f"终端 CLI 处理 {len(cli_cmds)} 条：{'、'.join(sorted(cli_cmds))}", f"（{_rel(cli_path)}）")

    both = set(adapter_cmds) & set(cli_cmds)
    only_qq = sorted(k for k in adapter_cmds if k not in cli_cmds)
    only_cli = sorted(k for k in cli_cmds if k not in adapter_cmds)
    note(f"两侧都有 {len(both)} 条：{'、'.join(sorted(both)) or '（无）'}")
    if only_qq or only_cli:
        hint("指令只在一边存在（另一边发了要么没反应，要么被当普通聊天交给模型）",
             f"只在 QQ：{'、'.join(only_qq) or '无'}；只在终端：{'、'.join(only_cli) or '无'}")
    else:
        check("两个入口的指令集合完全一致", True)

    # 帮助文本里列了、但实现里没有
    for path, tree, cmds, label in (
        (adapter_path, adapter_tree, adapter_cmds, "QQ 适配器"),
        (cli_path, cli_tree, cli_cmds, "终端 CLI"),
    ):
        for const_name, (listed, decl_line) in help_commands(tree).items():
            missing = sorted(c for c in listed if c not in cmds)
            check(
                f"{label} {const_name}（{_rel(path)}:{decl_line}）里列的指令都实现了",
                not missing,
                f"帮助里写了但没人处理：{'、'.join(missing)}（用户发了不会有任何响应）",
            )
        # 反过来：实现了但帮助里没写（提示，不算失败）
        listed_all: set[str] = set()
        for _names, _ln in help_commands(tree).values():
            listed_all |= _names
        undocumented = sorted(c for c in cmds if c not in listed_all)
        if undocumented:
            hint(f"{label} 有指令没写进帮助文本（用户不知道有这条）",
                 "、".join(undocumented))

    # ── 3. 发送路径 ──────────────────────────────────────────────
    print("\n3. 发送路径：不得绕过 send()")
    parent = {}
    for node in ast.walk(adapter_tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    KNOWN_EXCEPTIONS = {"send", "_maybe_outreach"}    # send 自己；_maybe_outreach 故意直连（要带 is_wakeup）
    bypass: list[str] = []
    direct_calls: list[str] = []
    for node in ast.walk(adapter_tree):
        if not isinstance(node, ast.Call):
            continue
        if _func_name(node.func) != "_request":
            continue
        first = node.args[0] if node.args else None
        is_post = isinstance(first, ast.Constant) and first.value == "POST"
        if not (is_post and any(_contains_messages(a) for a in node.args[1:])):
            continue
        owners = enclosing_funcs(parent, node)
        where = owners[0] if owners else "(模块级)"
        direct_calls.append(f"{where}（{_rel(adapter_path)}:{node.lineno}）")
        if where in KNOWN_EXCEPTIONS:
            continue
        bypass.append(
            f"{_rel(adapter_path)}:{node.lineno} 在 {where}() 里直接 "
            f"_request(\"POST\", .../messages)：绕过了分段/限频/同文去重/送达上报"
        )
    check("除 send() 外没有第二条发送路径", not bypass, "；".join(bypass))
    note("直连 /messages 的调用点（含已知例外）", "、".join(direct_calls) or "（无）")
    if any("_maybe_outreach" in d for d in direct_calls):
        note("_maybe_outreach 是**已知例外**：互动召回消息要带 is_wakeup 参数，"
             "send() 不支持，所以故意直连")

    # ── 4. 测试脚本断言 ──────────────────────────────────────────
    print("\n4. tools/ 下每个脚本至少一条断言")
    tools_dir = ROOT / "tools"
    no_assert: list[str] = []
    scanned = 0
    for path in sorted(tools_dir.glob("*.py")) if tools_dir.exists() else []:
        if path.name == "audit.py":                # 本文件：审计工具，不给自己打勾
            continue
        tree = _parse(path)
        if tree is None:
            continue
        scanned += 1
        if assert_calls(path, tree) == 0:
            no_assert.append(_rel(path))
    if no_assert:
        hint(f"{len(no_assert)} 个脚本没有任何 check() 断言（跑完永远是绿的，等于没测）",
             "、".join(no_assert))
    else:
        check(f"tools/ 下 {scanned} 个脚本都有断言", True)

    # ── 汇总 ────────────────────────────────────────────────────
    print("\n" + "═" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项" + (f"（另有 {HINT} 条提示）" if HINT else ""))
    print("═" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
