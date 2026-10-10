# team-store 修复报告（存储 / 记忆 / 配置层）

- 负责人：`team-store`
- 范围：`giftia/store.py`、`giftia/memory.py`、`giftia/config.py`、`giftia/lifespan.py`、`config/settings.toml`
  ＋（Lead 授权）`tools/selftest.py` 的 21 处调用点、`tools/live_check.py` 的 1 处
- 任务书：P1-6（view 语义）、P0-2（回填归属）、P0-5（写库编码）、P1-7（consolidate 事务）、
  P1-8（SQLite 稳定性）、P1-9（lifespan 边界）、P1-10（新会话游标）、§8 配置校验、§5.3 检索质量
- 探针：`.review/probes/fix_store_p0.py`（46 项）、`fix_memory_p1.py`（40 项）、`fix_config_lifespan.py`（49 项）

## 0. 一句话结论

9 条任务全部落地，**没有留下半成品**；本层探针 135 项全过，
`tools/selftest.py` **303 项全过（失败 0）** —— 基线是 302 项 299 过 3 败（那 3 项由 team-behavior 修掉）。
`tools/audit.py` 10/0（与基线证据 `.review/evidence/audit.log` 的 10/0 完全一致，无回归）。

## 1. 逐条改动与验证

### P1-6 `Store.active_memories` 的 view 语义（根因）

`giftia/store.py`
- 新增模块级常量 `ALL_SCOPES = "all"`，并给 `Store` 加了同名类属性（`Store.ALL_SCOPES` 与
  `giftia.store.ALL_SCOPES` 两种写法都成立 —— Lead 的探针用前者，team-recycle 用后者）。
- `active_memories(instance_no, view)` / `memories_by_person(instance_no, view)`：**view 必填、无默认值**。
- `_view_clause`：`view=None` → `TypeError`（fail-loud），错误信息里写清"要看全量请显式传 `ALL_SCOPES`
  并注明用途"；字符串只接受 `"all"`，其它字符串 → `ValueError`。
- `[("private","")] / ("group","*") / ("person",uid) / []` 的既有语义**一个都没改**。
- 我自己的内部调用点：`memories_by_person` 现在也要求 view（透传给 `active_memories`）；
  `memory.py` 的 `consolidate` / `reconcile` 本来就有显式视角，未动。

验证（`.review/probes/fix_store_p0.py` 第 1 节，全 PASS）：

```
[PASS] Store.ALL_SCOPES 与模块级常量都是 'all'
[PASS] view 省略时抛 TypeError（不再默认不过滤）
[PASS] view=None 抛 TypeError（fail-loud，不 fail-open）
[PASS] view=ALL_SCOPES → 全量 4 条
[PASS] view=[('private','')] → 只有搭档私事 1 条
[PASS] view=[('group','*')] → 两个群共 2 条
[PASS] view=[('group','GROUP-A')] → 精确命中 1 条
[PASS] view=[] → 什么都不给看
[PASS] memories_by_person 也要求 view
```

调用点收口（Lead 授权方案 B）：
- `tools/selftest.py` 21 处 → `view=Store.ALL_SCOPES`，只在第一次出现处（line 134 上方）加了一句注释。
  `git diff --stat tools/selftest.py` = 43 行改动（21×±1 + 1 行注释），**没有改任何断言/期望值**。
- `tools/live_check.py`：加 `from giftia.store import Store`，1 处补 `view=Store.ALL_SCOPES`。
- 别人的文件我没碰：`brain.py:1261/1345/1376`、`occasions.py:198`、`dream.py:46`、`recycle.py:102`
  由 team-brain / team-recycle 按新契约改了（我跑 selftest 时那几处已不再抛 TypeError）。

### P0-2 老库 scope 回填按 session_id 解析群标识

`giftia/store.py`
- `_session_scope_key()`：从 `i{n}-g<token>-<日期>` / `i{n}-p<token>-<日期>` 解析 `(scope, token)`；
  **先按 `i{n}-[gp]` 取整段、再去掉末尾日期段** —— 不能简单按 `-` 切第一段，
  否则 `i1-gGROUP-A-20261001` 会被解析成 `GROUP`，与 `last_group_openid` 做前缀比对时**误确认**
  （这是 Lead 探针第一轮实测抓出来的，已修）。
- 归属规则：
  - token 与 `state['last_group_openid']` 前缀比对**确认**（两个方向的前缀都算）→
    写**完整 openid**（session 里只有 12 位截断值，用完整值才能被 `("group", gid)` 视角精确命中）；
  - 对不上 / 解析不出来 → **保持 private**（宁可不给她看，也别看错人）；
  - `-p` 会话 → `subject_id = speaker_id`（同样要求前缀比对得上）；
  - 已经标成 group/person 但能**证明**标错的（stored subject 与 session token 对不上）→ 撤回 private。
    （v1 那次把群 A 标成群 B 的行不是 private，只认老标记就永远不会被纠正。）
- 标记：新增一次性重跑标记 `msg_scope_backfill_v2`，判断只看它；
  `msg_scope_backfilled` 仍然照写（别的代码可能读它）。

验证：`.review/probes/lead_v1_p0_verify.py`（Lead 的独立探针，P0-2 段由 FAIL → **PASS**）：

```
backfill 更新 1 条：
  #1 i1-gGROUP-A-20261001     scope=private  subject='' OK
  #2 i1-gGROUP-B-20261002     scope=group    subject='GROUP-B' OK
  [PASS] 每条群消息的 subject 是否与 session_id 里的群标识一致（错 0 条）
```

真实形态（12 位截断 + 多群）在 `.review/probes/fix_store_p0.py` 第 5、5b 节：
群 A 保持 private、群 B 用完整 openid 归位、v1 标错的行被 v2 纠正（全 PASS）。
memory-man 的 `mm_scope_backfill.py` 里 P0-8「群 A 的内容不得出现在群 B 的可见集合里」= **PASS（未泄漏）**。

> **一个需要 Lead 知道的判据差异**：`mm_scope_backfill.py` 的 P0-7 断言是
> `subject_id == "GROUP-A"`（要求每条群消息都被**正向归属**）。我的实现按你的契约在
> "token 与 last_group 对不上"时保持 `private`，所以它仍报 FAIL（`subject=''`）。
> 真正的 P0（跨群泄漏）由它的 P0-8 判 PASS 证明已修。
> 若你希望"对不上时也写 session 里的 token（截断值）而不是 private"，说一声，我改
> —— 代价是偏离契约字面，但好处是这些老消息不会被当成"搭档的私事"（那是 P0-3 类风险）。

### P0-5 写库编码安全（孤立代理字符）

`giftia/store.py`
- 新增模块级 `sanitize_text()` + `_LONE_SURROGATE_RE`：把 `\ud800-\udfff` 换成 **U+FFFD**，
  正常文本原样返回（幂等，一个字节都不动）。
- 应用到**所有 TEXT 写入口**：`add_message`（10 个参数）、`add_memory`（15 个字段）、
  `add_diary`、`add_dream`、`meta_set`、`state_set`、`update_memory`、`update_message_content`。

验证（`fix_store_p0.py` 第 2 节，全 PASS）：`json.loads('"hello \\ud83d world"')` 构造出的真孤立代理，
`add_message` 不再抛 `UnicodeEncodeError`，落库为 `'hello \ufffd world'`；其余入口逐条断言。
端到端：`.review/probes/qa_bugs.py` 的 B3 段原来两条"打脸断言"是 PASS（= bug 成立），现在翻成 FAIL：

```
[FAIL] ★ handle() 抛出 UnicodeEncodeError（这一轮彻底丢失，用户收不到回复）
[FAIL] 消息没有落库（写库失败在最前面）  → 0 → 2      ← 消息落库了（0 → 2 条）
[PASS] 进程还能继续（下一次正常消息没问题）
```

### P1-7 consolidate 事务

`giftia/store.py`
- 新增 `begin()/commit()/rollback()`：`BEGIN IMMEDIATE`（立刻拿写锁，冲突走 busy_timeout，
  不拖到 commit 才报 locked）；嵌套时抛 `RuntimeError`。
- `add_memory` / `update_memory` / `mark_consolidated` / `meta_set` 增加**关键字参数** `commit: bool = True`
  （默认不变 → 旧调用方行为完全不变）。

`giftia/memory.py`
- `consolidate` 的"写记忆 + 标 consolidated + 推两个游标"整段包进 `store.begin() … commit()`，
  异常 `rollback()` 后原样抛出。
- `on_draft` 回调（关系加分 / 改称呼）**挪到提交之后**统一触发：回调自己会 `state_set`（带 commit），
  留在事务里会把没写完的整理提前提交掉，回滚就不完整了。回滚时因此也不会有"凭空的亲密度"。
- 去重逻辑（`similarity`）未动。

验证（`fix_memory_p1.py` 第 1 节，全 PASS）：把 `meta_set` 换成"第 2 次调用就抛 I/O 错误"的替身 →

```
[PASS] 崩溃确实发生在事务内（模拟第 2 个 meta_set 失败）
[PASS] 记忆一条都没落库（整批回滚）
[PASS] 消息仍是未整理（下一轮可重试）
[PASS] 会话游标没有被推到任何未整理消息之后（保持地板值）
[PASS] 全局游标没有推进
[PASS] on_draft 回调没有在回滚的事务里触发（关系加分不会凭空发生）
[PASS] 重试成功写出 2 条记忆
[PASS] 回调在提交之后才触发（条数与记忆一致）
[PASS] 两个游标都推到了最后一条
[PASS] 重试/重复对话不会产生重复记忆（相似度去重仍在）
```

> 说明：会话游标的**初次建立**仍在 `load_pending`（读路径）里完成，值是
> `min(本会话最早未整理 id - 1, 全局游标)` —— 它是"地板"而不是"推进"，回滚后重试得到同一个值，
> 不可能让消息被跳过。要保持"读路径零写入"就得改 `load_pending` 的签名/返回值，按你的
> "不要改签名"要求我没动，只把它写进注释和探针。

### P1-8 SQLite 稳定性（＋P3-1 索引）

`giftia/store.py`
- `PRAGMA busy_timeout=5000`。
- `journal_mode=WAL` 的**返回值检查**：不是 `wal` 就抛 `RuntimeError` 并给人话
  （网络盘/只读挂载不支持 WAL；静默降级成 delete 会表现成随机 `database is locked`）。
- `sqlite3.connect(..., check_same_thread=True)` 显式化 + 注释说明"保持现状语义（谁开谁用）"。
- 新增 `CREATE INDEX idx_msg_mid ON messages(mid)`（P3-1 顺带）。
- **顺带修掉一处顺序耦合**：索引创建从 `SCHEMA` 里拆出来成 `INDEXES`，在 `_migrate()` **之后**执行
  —— 否则老库还没有 `mid` 列时，建索引会直接 `no such column: mid` 打不开库；
  同时把 `sealed` 补进 `_MIGRATIONS["memories"]`（最早的 schema 没有它，而 `idx_mem_instance` 依赖它）。

验证（`fix_store_p0.py` 第 3、6 节，全 PASS）：busy_timeout=5000、journal_mode=wal、
`idx_msg_mid` 存在且 `EXPLAIN QUERY PLAN` 用得上（不再 `SCAN messages`）、
跨线程用同一连接抛 `ProgrammingError`、**老 schema（只有 6 列 messages / 4 列 memories）也能打开并补齐**。

### P1-9 lifespan 边界

`giftia/config.py`
- `LifespanConfig.validated()`：`total_hours` 允许区间 `[1, 1_000_000]` 小时；
  **0 当成"没填"**→ 回落默认 81920 小时 + stderr 警告；负数 / 非有限 / 超上限 → `配置错误：…` 启动即失败；
  `demo_total_hours` 0=不用、负数→报错；`terminal_warning_hours` 负数→报错。由 `load_settings` 调用。
- 数据类本身仍可被程序化构造（探针/测试要造边界值），第二道防线在 `status()`。

`giftia/lifespan.py`
- `total_hours`：库里的值必须是有限且 > 0 才采用，否则回落配置值。
- `status()`：坏值（0/负数/nan/inf）→ 打印一行警告并回落可用值（绝不表现成"出生即到期"）；
  `timedelta(hours=total)` 用 try/except 兜住 `OverflowError`（极大但有限的值，如 1e300），
  退回"激活 + 100 年"——只影响展示的回收预定日期，ratio/stage 不受影响。
- 短寿命演示（0.5 小时）**不会被兜底吃掉**，仍按真实比例推进到 terminal。

验证（`fix_config_lifespan.py` 第 1、2 节，全 PASS）＋ `.review/probes/mm_consolidate_time.py` 5b 段：

```
total_hours=         0.0 → total=81920.0 stage=clear ratio=1.0000 expired=False
[PASS] P2-6 total_hours=0.0 不得被静默接受成'立刻到期'
total_hours=        -1.0 → total=81920.0 stage=clear ratio=1.0000 expired=False
[PASS] P2-6 total_hours=-1.0 不得被静默接受成'立刻到期'
total_hours=1000000000.0 → total=1000000000.0 stage=clear ratio=1.0000 expired=False   ← 不再 OverflowError
```

> 5b 段**不再崩**（三种值都不抛异常）。该探针随后会在 **5c** 段停下：那一段直连
> `load_settings`（故意不写 `[[degradation.stages]]`），现在按契约抛
> `ValueError: 配置错误：settings.toml 缺少 [[degradation.stages]]（至少要有一档）…` —— 这正是 fail-loud 的预期结果，
> 而它那条断言本来就是硬编码 `check(..., False)`。

### P1-10 新会话游标

`giftia/store.py`：新增 `first_open_message_id(instance_no, session_id)`（`MIN(id)`，只看未整理未撤回）。
`giftia/memory.py`：`load_pending` 建游标时取 `min(本会话最早未整理 id - 1, 全局游标)`；
本会话没有未整理消息时退回全局游标。

验证（`fix_memory_p1.py` 第 2 节，全 PASS）：

```
会话1 pending=[1]；会话3 pending=[3]
[PASS] 新会话自己的 id=1 不会被全局游标跳过
[PASS] 新会话游标 = min(1-1, 2) = 0
[PASS] 已建过游标的会话不受影响（id=2 之后才取）
```

> `mm_consolidate_time.py` 第 3 节的 P2-5 仍然打印 FAIL，但**不是回归**：该探针 1~4 节共用同一个库
> （`make_brain(tmp/x, settings=settings)` 里 `settings.root` 指向父目录），到第 3 节时
> `last_consolidated:i1-m-20261001` 已经被第 2 节的 `consolidate()` 写成 4，而它期望的 `id=1`
> 是第 1 节那条 `i1-gGROUP-A-…` 的消息 —— 会话维度根本不同。探针自己的期待值与它构造的数据不一致，
> 修复前后的输出都是同一行 "pending=[5] / meta=4"。我用独立库（`fix_memory_p1.py` 第 2 节）
> 复现并验证了这条修复。

### §8 配置校验（根因级）

`giftia/config.py`
- `_sub()` 收集未知键 → stderr 警告（带段名，例如 `settings.toml [runtime] 里有未知的配置键（拼错了？）：response_tierr`）；
  `persona.toml` 的 `[bond]`/`[moods]` 用 `ignore=` 排除，不误报。
- `[[degradation.stages]]` 缺整段 / 为空 / 缺字段 / `blur` 越界 → **报错**，错误文案含"配置错误"与后果说明。
- `response_tier` 夹到 0~100 并提示；`speak_max_per_day` 等上限为负 → 报错。
- `keywords` 必须是字符串列表（`keywords = "晚安"` → 报错并给出正确写法），
  出厂 `keywords = ["艾拉", "机器人", "小艾"]` **继续可用**。
- `memory` / `dream` / `mood` / `relationship` / `model` 的计数与 0~1 字段一起校验。
- `.env` 解析：`utf-8-sig`（BOM）、去掉行首 `export `、行内 `#` 注释（引号内不算）、
  **只剥成对的引号**（不成对时原样保留）、CRLF、空值、值里带 `=`、已存在的环境变量优先。
- `OutreachConfig` 新增两个字段（Lead 指定）：`mood_share_min_idle_hours: float = 6.0`、
  `wakeup_max_per_30_days: int = 4`；`config/settings.toml` 的 `[outreach]` 段同步加了两项并写清注释。

验证（`fix_config_lifespan.py` 第 0、1、3、4、5 节，全 PASS，共 49 项）：
真实出厂配置照常加载（5 档 stages、tier=45、81920 小时、keywords 三项）、
`total_hours` 六种坏值矩阵、stages 三种坏配置、tier 夹取、7 类负值/越界、
13 条 `.env` 用例。

### §5.3 检索质量

`giftia/memory.py` / `giftia/config.py` / `config/settings.toml`
- `is_cited`：**要求至少命中一个二元组/词**（`shared_bigrams >= 1`）——
  只靠单字（含 `a`/`b`/`c` 这种单字母词元）凑满 `cite_min_shared` 不再算"说出口"。
- `mark_cited`：**一条回复最多强化一条**（`recalled` 已按得分降序 → 取第一个判真的），
  避免一句话给 3 条记忆各 +1。
- `recall()`：逐条分词结果按 `(id, 文本)` 缓存（`_doc_counts`，上限 4096 条，文本改了就换键）；
  候选集预筛 `cfg.recall_prefilter`（默认 2000，**正常数据量下不改变任何行为**，
  只给每轮检索开销上界；排序只看层级/重要性/id，不做分词）。
- **`min_score` 的相对语义一字未改**（没有动归档公式），只在 `Stage` 文档、`recall` docstring、
  `MemoryConfig` 注释与 `settings.toml` 注释里写明"它是**相对**门槛"；
  新增绝对门槛 `min_raw_score`（未归一化 BM25 原始分，默认 0.0 = 不启用）。

验证：`fix_memory_p1.py` 第 3~7 节全 PASS；memory-man 的 `mm_recall_reinforce.py`：

```
[PASS] P2-3 一条回复可以同时给多条记忆 +1（模糊判定下的连带强化）  → 一句话强化了 1 条
（被强化的 id：[1] → recall_count={1: 1, 2: 0, 3: 0}）
```

### 顺带（同一文件、同一类问题，已在登记册有编号）

- **快照恢复丢 `forgotten` 列**（§5.2）：`restore_memories` 的 `cols` 补上 `forgotten`。
  验证：`mm_snapshot_injection.py` →
  `[PASS] P2-11 快照恢复应保真（forgotten 等列不得在恢复后变化） → 完全一致`。

## 2. 函数签名改动清单（契约要求 / 事务需要）

| 位置 | 改动 | 兼容性 |
|---|---|---|
| `Store.active_memories(instance_no, view)` | `view` 从 `=None` 变为**必填** | **故意的破坏性改动**（契约 §一）；调用点已全部收口 |
| `Store.memories_by_person(instance_no, view)` | 同上 | 同上 |
| `Store.add_memory(..., *, commit=True)` | 新增关键字参数 | 旧调用方不变 |
| `Store.update_memory(..., *, commit=True)` | 同上 | 同上 |
| `Store.mark_consolidated(ids, *, commit=True)` | 同上 | 同上 |
| `Store.meta_set(key, value, *, commit=True)` | 同上 | 同上 |
| `Store.add_message/update_message_content/add_diary/add_dream/state_set` | 签名不变，内部做文本归一化 | 完全兼容 |
| `LifespanConfig.validated()` | 新增方法 | 新增，不破坏 |
| `MemoryConfig.min_raw_score` / `recall_prefilter`、`OutreachConfig.mood_share_min_idle_hours` / `wakeup_max_per_30_days` | 新增字段（有默认值） | 兼容 |
| `Store.first_open_message_id()` / `Store.begin/commit/rollback()` | 新增方法 | 新增，不破坏 |
| `sanitize_text()` | 新增模块级函数 | 新增，不破坏 |

> **注意给探针作者**：`meta_set` 现在多了 `commit` 关键字参数，**用替身 monkeypatch 它的探针要接受 `**kwargs`**。
> `.review/probes/mm_store_sqlite.py:107` 的 `flaky_meta_set(key, value)` 因此会
> `TypeError: got an unexpected keyword argument 'commit'`（那是探针的替身签名，不是产品代码的问题）。
> `bc_fault.py` / `bc_tools.py` / `team_brain_fixes.py` 的替身都是 `*a, **kw`，不受影响。

## 3. 验证汇总（真实输出）

| 命令 | 结果 | 基线 |
|---|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项** | 302 项 299 过 3 败 |
| `python .review\probes\fix_store_p0.py` | 通过 46 项，失败 0 项 | 新增探针 |
| `python .review\probes\fix_memory_p1.py` | 通过 40 项，失败 0 项 | 新增探针 |
| `python .review\probes\fix_config_lifespan.py` | 通过 49 项，失败 0 项 | 新增探针 |
| `python .review\probes\lead_v1_p0_verify.py` | P0-2 段 `[PASS] …（错 0 条）` | 修复前 FAIL |
| `python .review\probes\mm_consolidate_time.py` | 5b 段三种值全 `[PASS]`、不再崩；随后在 5c 段按预期抛配置错误 | 5b 段 OverflowError |
| `python .review\probes\mm_recall_reinforce.py` | `[PASS] P2-3 …一句话强化了 1 条`（第 4 节起因探针自身未补 view 而 TypeError） | 修复前 `被强化的 id：[1,2,3]` |
| `python .review\probes\mm_snapshot_injection.py` | `[PASS] P2-11 快照恢复应保真 → 完全一致` | 修复前 1 处差异 |
| `python .review\probes\mm_scope_backfill.py` | P0-8 `[PASS] 未泄漏`；P0-7 因判据差异 FAIL（见上文说明） | 两条都 FAIL |
| `python .review\run_tests.py tools/audit.py` | 通过 10 项，失败 0 项（4 条提示） | 与 `.review/evidence/audit.log` 完全一致（10/0） |
| `python .review\run_tests.py tools/ws_selftest.py` | 通过 17 项，失败 0 项 | 17/0 |
| `python .review\run_tests.py tools/qq_protocol_selftest.py` | 通过 72 项，失败 0 项 | 72/0 |

## 4. 遗留 / 需要 Lead 决策

1. **`mm_scope_backfill.py` P0-7 的判据**（上文）：要不要把"对不上时保持 private"改成"写 session 里的 token"。
   我按契约实现，等你一句话。
2. **其它探针里的 53 处 `active_memories(x)` 没补 view**（`.review/probes/*.py`，例如
   `qa_e2e.py` 13 处、`bc_flow.py` 4 处、`beh_dream.py` 4 处、`mm_recall_reinforce.py:143`、
   `qa_bugs.py:133`、`mm_store_sqlite.py:200`…）。它们不是我负责的文件，我没动；
   这些探针现在会在第一个这样的调用点抛 `TypeError` 而中止（产品代码没问题）。
   要不要像 selftest 一样统一补 `view=Store.ALL_SCOPES`，请分派。
3. **`mm_store_sqlite.py:107` 的 `flaky_meta_set(key, value)`** 需要接受 `**kwargs`（见上表说明）。
4. **未做（不在本批清单，建议下一批）**：
   - `_age_hours` 未做 tz 归一化（§5.1；tz-aware 时间戳会让 `recall()` 抛 `TypeError`，`human_age` 专门防了它）；
   - `mark_recalled` 只按 `mid` 匹配、没有 session 条件（P3-1 的语义半条，我只加了索引）；
   - `messages_by_prefix` 把 `%`/`_` 删掉而不是转义（P3-4）；
   - `blur_text` 的银行家舍入（P3-2）、`max_chars` 二次截断与 off-by-one（P2-1）——
     这两条在 `memory.py`，属于 B4「数值/时间/边界」交给 team-behavior 的条目，
     如果实际落在我文件里，请明确归谁（避免两人同时改 `memory.py`）。
5. **`OutreachConfig` 两个新字段目前还没人读**：在波浪 2 接上之前，`tools/audit.py` 会把它们
   算作"从未被读取的配置字段"提示（`hint()` 只加提示、不改 PASS/FAIL 计数）。

---

# 第 2 波（Lead 2026-10-10 验收后追加）

Lead 决策：① `mm_scope_backfill.py` 改**探针判据**（不改 store 语义）；② 授权迁移 `.review/probes/*.py` 的全部裸
`active_memories` 调用；③ `mm_store_sqlite.py` 的替身适配 `*a, **kw`；④ B4 剩余几条（`_age_hours` tz、`mark_recalled`
会话条件、`messages_by_prefix` LIKE 转义、`blur_text` floor、`max_chars`）归我；`relative_cutoff/历史 query` 与
`core 保底注入` 归 Lead。

## 1. 产品代码（`giftia/store.py`、`giftia/memory.py`）

| 条目 | 改动 | 验证 |
|---|---|---|
| `_age_hours` tz 归一化（§5.1） | `fromisoformat` 后按 `human_age` 的办法对齐 tz（aware↔naive 两个方向都归一），不再让 tz-aware 时间戳把 `recall()` 打崩 | `fix_memory_p1.py` §8 两条 PASS |
| `blur_text` 舍入（P3-2） | `int(round(...))` → `math.floor(...)`，擦除量随 ratio 单调不回退 | `fix_memory_p1.py` §8：擦除点 `[0,1,1,1,1,1]` 单调 PASS；`mm_degradation.py` 仍 0 失败 |
| `max_chars` 截断（P2-1） | ① 截断**不再**把 `blurred` 置 True（`blurred` 只由 `blur_text` 决定，内核记忆不再被误标"已模糊"）② 长度真的不超过配置值（省略号 `……` 占 **2** 个字符，已算进预算；`max_chars<2` 的极端值也不超长） | `fix_memory_p1.py` §8 三条 PASS（`len<=max_chars`、`blurred=False`、`max_chars=1` 不超长） |
| `messages_by_prefix` LIKE 转义（P3-4） | `%`/`_`/`\` 改成 `ESCAPE '\'` 转义（以前是直接删掉 → 含 `_` 的会话前缀静默失配） | `fix_memory_p1.py` §8：`i1-pus_er-` 只命中带下划线的会话；`i1-%` 不再当通配符 |
| `mark_recalled` 会话条件（P3-1） | 新增**可选** `session_id`：传了就 `WHERE mid = ? AND session_id = ?`；不传保持旧行为（兼容既有调用方，`tools/selftest.py:1244` 不受影响） | `fix_memory_p1.py` §8：同 mid 两条分属 s1/s2，按 s1 撤回只标记 1 条；不传时仍标记 2 条 |

> **调用方提示**：`mark_recalled` 的会话条件要生效，**适配器侧得把 session 传进来**——
> `adapters/qq_official.py:642` 现在仍是 `mark_recalled(mid)`（没有 session）。那条路径拿到的是撤回事件
> （`event_type` 含 RECALL，payload 里有 `group_openid` / 用户 openid），我**没有改适配器**（不在我文件范围），
> 请 Lead 或适配器负责人补上：有会话就传 `session_id`，拿不到就沿用旧的全表行为。

## 2. 探针

- **`mm_scope_backfill.py` 判据修订**（Lead 裁定）：原来要求"每条 `-g` 行都正向归属"，与契约"对不上就保持
  private"冲突。新判据 = ① 没有任何行被归属到**别的群**（跨群泄漏）；② 能确认的行必须归属正确（用完整
  openid）。文件里写了修订理由，**store 语义一行未改**。结果：`回填探针失败计数：0`。
- **`mm_store_sqlite.py` 替身适配**：`flaky_meta_set(key, value, *a, **kw)` 并原样转发（`meta_set` 多了
  `commit=`）。它现在完整跑完并如实显示修复后的行为：崩溃后 `memories=0 条, messages=[{consolidated:0}]`。
- **裸 `active_memories` 迁移**：AST 级扫描（按 `view` 关键字/第二个位置参数判断），只加
  `view=Store.ALL_SCOPES` + 必要 import，**未改任何断言**。命中 9 个文件 28 处：
  `beh_dream.py` 4、`mm_multi_query_scale.py` 3、`mm_recall_reinforce.py` 1、`mm_snapshot_injection.py` 2、
  `mm_store_sqlite.py` 1、`mm_unfiltered_paths2.py` 1、`qa_bugs.py` 1、`qa_e2e.py` 13、`qa_time.py` 2。
  另外 `mm_unfiltered_paths.py:113` 的 `view=None`（"量一下全量"的用法，不是 None 语义测试）也迁到
  `ALL_SCOPES` 并注明契约；`fix_store_p0.py:73` 是**故意漏传 view** 的 fail-loud 测试，保持原样并加了注释。
  `bc_*` / `beh_*` / `lead_v*` / `pa_ws_probe` 等文件里的裸调用**在我动手前已由各自负责人迁完**。
- **`pa_config_probe.py` 两处适配**（§8 契约的后果，已在注释里写明）：
  ① `toml_case()` 给"只测某个字段"的最小配置补一档合法 `[[degradation.stages]]`（已经自带 stages 的 2a/2f 不动），
  否则会停在"缺 stages"的 fail-loud 上、盖住被测字段；② 2e 的 keywords 用例改为"抛配置错误 或 读成字符串列表"
  都算正确（逐字匹配 `('晚','安')` 仍判失败）——检查意图不变。
  现在它跑完：**22 项通过 / 1 项失败**，唯一失败是 2d 的既有陈旧期望（它断言"拼错的键要生效"，`response_tier == 100`），我没动它。

> 迁移过程说明（如实记录）：第一版迁移脚本把 `ast` 的**字节**列偏移当字符下标用，中文行里插错位置，
> 一度把 9 个探针插成语法错误。已全部 `git checkout` 回滚重做（改字符偏移换算 + 处理关键字参数），
> 现在全部探针 `ast.parse` 通过。

## 3. 第 2 波验证汇总（真实输出）

| 命令 | 结果 |
|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项** |
| `python .review\run_tests.py tools/audit.py` | 通过 10 项，失败 0 项（4 条提示） |
| `python .review\run_tests.py tools/ws_selftest.py` / `qq_protocol_selftest.py` | 17/0、72/0 |
| `fix_store_p0.py` / `fix_memory_p1.py` / `fix_config_lifespan.py` | 46/0、**51/0**（新增 §8 十一条）、49/0 |
| `mm_scope_backfill.py` | **失败计数 0**（判据修订后） |
| `mm_store_sqlite.py` | 完整跑完；崩溃后 `memories=0 / consolidated=0`（事务生效） |
| `pa_config_probe.py` | 22 通过 / 1 失败（2d 陈旧期望） |
| 全量探针清扫（39 个） | 仅 3 个仍有 traceback，且都不是我的层：`mm_consolidate_time.py`（5c 按契约 fail-loud，预期）、`beh_outreach.py:156`（`reason=None` 格式化，P1-16 行为变更后探针没跟上）、`beh_cli.py:126`（探针自身 `UnboundLocalError`） |

## 4. 交回 Lead（第 2 波）

1. **`mark_recalled` 的调用方**：`adapters/qq_official.py:642` 需要传 `session_id` 才能拿到 P3-1 的收益（见上）。
2. **`beh_outreach.py:156`**：P1-16 之后 `plan()` 可能返回 `None`，探针的 `f"{r:<11}"` 会 `TypeError`；属行为侧探针格式化，我没动。
3. **`beh_cli.py:126`**：探针自身 `UnboundLocalError: first`，与本次改动无关。
4. **`mm_unfiltered_paths.py` 仍报两条 FAIL**，属"判据过期"而非回归：
   `P1 观察：dreaming 资格按全量记忆数判定`（它比的是两个计数，不是 `should_dream()` 的行为）、
   `P0-4 封存 JSON 不得含他人私聊内容`（Lead 已裁定封存 JSON 保留全量运维档案 = 分层期望）。要不要像
   `mm_scope_backfill` 一样改判据，请裁定。
