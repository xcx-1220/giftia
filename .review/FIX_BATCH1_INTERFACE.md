# 修复批次 1 接口约定（隐私根因）

> 本文件是**契约**。改代码前先读它；如果你的实现与这里不一致，先改这里再改代码（并在提交信息里说明）。
> 目标：消掉 4 条隐私 P0 + 相关 P1，且**不破坏现有 299 项通过**。

## 一、`Store.active_memories` 的 `view` 语义（team-man 负责实现）

现状（危险）：`view=None` = **不过滤**，于是"忘了传 view"就等于把所有人的事摊开。
目标语义：

```python
ALL_SCOPES = "all"          # 模块级常量，显式表示"我要看全量"

active_memories(instance_no, view=...)   # view 现在是**必填关键字参数**
```

`view` 的取值形式：

| 取值 | 含义 |
|---|---|
| `"all"` / `store.ALL_SCOPES` | 显式全量（**只允许导出/自检/迁移使用**，调用点必须写注释说明为什么） |
| `[(scope, subject), ...]` | 白名单视角，与现状相同 |
| `None` | **不再接受**；传 `None` 视为编程错误 → 抛 `TypeError`（fail-loud，别 fail-open） |

`_view_clause` 的既有语义（保持）：

- `("private", "")` → `scope = 'private'`（当前"搭档的私事"桶）
- `("private", "<uid>")` → `scope = 'private' AND subject_id = ?`
- `("group", "*")` → `scope = 'group'`（所有群）
- `("group", "<gid>")` / `("person", "<uid>")` → 精确匹配

**迁移要求**：不要再有"默认视角"这种东西。`recycle.py` / `occasions.py` / `dream.py` / `brain.py`
里所有原先不传 `view` 的调用点，必须显式选一个视角（见下）。

## 二、`private` 记忆绑定主体（team-man + team-b 协作）

现状：`("private", "")` 不带主体，所以"谁是搭档"一换，整份私事就换主人（P0-3 根因）。

新规则：

1. **新写入**：搭档私聊产生的记忆写成 `scope='private'`、`subject_id=''`（**保持现状不变**，
   这样老库/CLI/自测都不用改），但在读取侧靠下面的"绑定时刻过滤"解决越权。
2. **读取**：搭档视角由 brain 组装为
   `[("private", ""), ("person", handler_id), ("group", "*")]`——**与现状一致**。
3. **越权的那一环靠"绑定时刻"解决**（关键）：
   `brain.set_handler_id(uid)` 时，把**上一任搭档**留下的 private 记忆
   在本次绑定之前的时间戳记为 `meta['handler_bound_at']`，并**不再允许新搭档看到
   `happened_ts < handler_bound_at` 的 private 记忆**（他们之间没有共同的过去）。
   - 第一次绑定（库里还没有 private 记忆）时 `handler_bound_at = ""`，表示"没有历史私事需要遮蔽"。
   - `/unbind` 之后没有搭档，任何私聊都**不是**搭档（见 §三），所以这段遮蔽只在"换人"时起作用。
   - 实现位置：`brain.memory_view` 的组装处（brain.py），**不要**散在 SQL 里。
   - 遮挡实现方式由 team-b 选：最省事的是在 `memory_view` 之外的检索前过滤
     （`mem["happened_ts"] >= handler_bound_at`），或在 `_view_clause` 里加一个可选的时间下界。

> 这条规则同时解决两件事：① 换人后新人看不到前任的私事；② 不破坏"同一任的她记得跟你说过什么"。

## 三、`/unbind` 与未绑定态的语义（team-b 负责 brain，team-adapt 负责适配器）

**统一后的契约**（README 也要同步改）：

1. **没有搭档时，任何私聊都不是搭档**：`is_handler` 为真当且仅当 `owner` 非空且 `user_id == owner`。
   - 适配器：`is_handler = bool(owner) and user_id == owner`（**删掉 `else (kind == "c2c")`**）。
   - 但**自动认主**仍然保留：`auto_bind=true` 且 `auto_bind_confirm` 之下，第一条私聊会先把它设为搭档
     （这正是"认主"），随后这一条消息就按搭档处理——唯一要求是它必须经过
     §二 的"绑定时刻"遮蔽，不能带出上一任的私事。
2. **没有搭档时，群里/私聊只允许以下指令**：`/bind`、`/help`、`/unbind`（幂等，直接回一句"我已经是一个人"）。
   其余指令（`/profile` `/forget` `/impress` `/name` `/status` `/days` …）一律婉拒：
   「……这个我现在做不了，得先让我知道你是我的搭档。」并且**不执行、不写库**。
3. `/unbind` 只允许**当前搭档本人**执行（`owner` 非空且 `user_id == owner`）；
   `owner` 为空时执行 `/unbind` 是幂等空操作。
4. `/profile`、`/impress`、`/forget`、`/reconcile` 是**搭档专属**指令：
   - 必须 `user_id == owner`（`owner` 为空时一律拒绝）；
   - **群里一律拒绝**（它们是"你看她的记忆"的视角，不该当众输出）。

## 四、文档同步（lead 负责，别人别动 README）

上面三条会改变对外行为，README §3.5 / §3.6 / §3.7 / §6 的"认主/换人/指令表"必须同步。
`/unbind` 的说明要写清"解绑后你留下的私事不会被下一位读到，但你自己也要重新 `/bind` 才能继续"。

## 五、验证要求（每条都要有输出）

1. `python .review\run_tests.py tools/selftest.py` → **失败项不得多于基线，且总数不得下降**（基线 302 项 299 过 3 败）。
2. `python .review\probes\lead_v1_p0_verify.py` / `lead_v2_p0_commands.py` / `lead_v3_p0_occasions.py`
   三条 Lead 探针里对应的 `[FAIL]` 必须变成 `[PASS]`。
3. 新增回归断言（写进你自己的探针或直接补进 `tools/selftest.py` 对应小节）：
   - 群发提示词不含 `person` / 其他群的大日子；
   - 回忆录 `.md`、封存 `.json`、`diary` 不含 `person` 归属的记忆；
   - `/days` 不列别人的大日子；
   - 解绑后陌生人私聊的提示词里**没有**前任私事；
   - 未绑定时群成员 `/profile` 不输出任何档案，且不写库。
4. **不许为了让测试变绿而改测试的期望值**——除非那条期望本身就与 README 矛盾（那要先说明）。
