# Stream A 审查报告：记忆引擎 / 寿命 / 存储层

- 审查人：`memory-man`（task-1）
- 范围：`giftia/memory.py`、`giftia/lifespan.py`、`giftia/store.py`，穿插读 `giftia/dream.py`、
  `giftia/occasions.py`、`giftia/recycle.py`、`giftia/agent_tools.py`、`giftia/brain.py`（只读，未改动）
- 环境：Python 3.14.3 / 纯标准库 / 真实 `config/settings.toml` + 临时数据根
  `D:\ai chatbot\.review\tmp\memory-man-*`（未触碰 `data/giftia.db`，只读打开过，库为空）
- 探针：`.review/probes/mm_*.py`（10 个），全部可复现；下面每条发现都带**真实输出片段**

复现命令（都在 `D:\ai chatbot` 下执行，先 `$env:PYTHONIOENCODING="utf-8"`）：

```powershell
python .review\probes\mm_privacy_scope.py       # 分区闸门（正向对照）
python .review\probes\mm_unfiltered_paths.py    # 无 view 的读取路径 → P0-1
python .review\probes\mm_unfiltered_paths2.py   # /days、dream 计数口径 → P1-3、P1-4
python .review\probes\mm_scope_backfill.py      # 老库回填归属 → P0-2
python .review\probes\mm_degradation.py         # 退化公式/四个参数 → P2-1、P3-2
python .review\probes\mm_recall_reinforce.py    # 召回强化 → P2-7
python .review\probes\mm_store_sqlite.py        # SQLite/迁移/tz → P2-3、P2-4
python .review\probes\mm_consolidate_time.py    # 整理/游标/寿命边界 → P1-5、P2-5、P2-6、P2-11、P3-3
python .review\probes\mm_snapshot_injection.py  # 快照/注入面 → P2-2、P2-8
python .review\probes\mm_retention_base.py      # Δt 起点、衰减曲线 → P2-9
```

**先说正向结论（这些承诺是真的，已跑通）**：
- `scope`/`subject_id` 闸门在**主检索路径**上确实生效：群视角 `active_memories` 只返回本群记忆，
  搭档私聊的 `private` 记忆在群里检索不到（`mm_privacy_scope.py` 全 PASS）。
- `effective_blur()` 与 README 公式**逐点一致**（5 阶段 × 3 层级 × 5 档保留率 = 75 个点全对）。
- `top_k` / `min_score` / `relative_cutoff` / `max_chars` 四个参数**都真的生效**（各自单独打脸失败）。
- `layer_core_blur=0` → core 层在 terminal 阶段 blur 恒为 0；调成 0.5 后立刻开始糊（旋钮是真的）。
- 召回强化**只在"说出口"时** +1：`mark_injected`（塞提示词）、工具 `search`、检索本身都不 +1。
- 模糊结果可复现（同 id/阶段/层级/保留率，三次完全一致）。
- SQL 全部参数化：`' OR '1'='1`、`'); DROP TABLE memories; --` 都进不去。

---

## P0（隐私越界：别人的私聊内容会流到不该到的地方）

### [P0-1] 回收仪式把**所有归属**的记忆（含他人私聊、所有群）写进回忆录、封存 JSON 与 `/memoir`

- 位置：`giftia/recycle.py:102`（`perform_recycle`，`store.active_memories(instance_no)` 无 view）
  → 传给 `_memoir_context()`（recycle.py:70-86）→ `settings.memoir_dir` 的 .md、
  `settings.sealed_dir` 的 .json、以及 `store.add_diary(..., kind="terminal")`
- 严重度：**P0**（隐私越界） 置信度：**高**（已跑出完整输出）
- 现象与影响：`view=None` 在 `Store._view_clause` 里的语义是"不过滤"。于是**任何人的私聊记忆、
  任何一个群里的事**都会被逐条写进给搭档看的回忆录，也进了 `/memoir` 能读到的 `diary` 记录。
  这是"她忘了，你记得"的交付物，却顺手把第三方的私事一起交付了。
  同一条路径还把这段正文喂给 `llm.write_memoir(context)`，模型生成的回忆录可能复述别人的私事。
- 触发条件：只要库里存在 `scope='person'` 或 `scope='group'` 的记忆，跑到回收（`/recycle`
  或寿命到期）就发生。多人群聊用过之后必然存在。
- 证据（`python .review\probes\mm_unfiltered_paths.py`）：

  ```
  路径 C：recycle.perform_recycle() —— 回忆录与封存 JSON
      回忆录：...\memory-man-unfiltered\data\回收日志\回收-01-20261010-082113.md
  [FAIL] P0-3 回忆录不得含他人私聊内容  → 含 SECRET_PERSON
  [FAIL] P0-4 封存 JSON 不得含他人私聊内容  → 含 SECRET_PERSON
      回忆录泄漏行 → - 2026-10-05T11:00：SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
      回忆录泄漏行 → - `2026-10-05T11:00`（重要性 0.90）SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
  [FAIL] P0-5 /memoir（diary 表）不得含他人私聊内容  → 含 SECRET_PERSON
  ```

  对照：同一个库里 `find_occasions(view=群视角)` 是守住的（PASS），说明闸门机制没问题，
  是这一处**没传 view**。
- 建议修法：`perform_recycle` 增加 `view` 参数（默认 `[("private",""),("person", handler_id)]`，
  即"搭档自己的事"），回忆录只收搭档视角的记忆；封存 JSON 可以保留全量但需在文件名/文档里
  明确标注"含全部归属的原始数据，属于运维资产而非给她看的东西"。`add_diary` 的正文同样要走 view。

### [P0-2] 老库 scope 回填把**所有**历史群聊消息归属成"最近活跃的那个群" → A 群的事会在 B 群被说出来

- 位置：`giftia/store.py:322-356`（`backfill_message_scopes`，`group_id` 取自单例
  `state['last_group_openid']`），由 `giftia/brain.py:96-100` 在每次启动时调用
- 严重度：**P0**（隐私越界 + 数据污染） 置信度：**高**
- 现象与影响：`scope` 列是后加的，老库里所有历史行都是默认 `private/''`。回填时把**凡是
  session_id 里带 `-g` 的行**统统写成 `scope='group', subject_id=<最近那个群>`。
  她可以同时在多个群里，于是群 A 的历史消息被贴上群 B 的 openid；接着
  `consolidate(scope='group', subject_id='GROUP-B')` 把它写成"群 B 的记忆"，
  **群 B 的检索就能查到群 A 的内容并当众说出来**。
- 触发条件：升级前库里有多于一个群的历史消息；启动时自动执行一次（`meta msg_scope_backfilled` 只做一次）。
- 证据（`python .review\probes\mm_scope_backfill.py`，用真老 schema 建库后升级）：

  ```
  老库升级后： #1 i1-gGROUP-A-20261001 scope=private subject=''
              #2 i1-gGROUP-B-20261002 scope=private subject=''
  backfill 更新 2 条：
      #1 i1-gGROUP-A-20261001     scope=group    subject='GROUP-B'   ← 群 A 的消息被标成群 B
      #2 i1-gGROUP-B-20261002     scope=group    subject='GROUP-B'
  [FAIL] P0-7 群 A 的历史消息不得被归属成群 B
  整理出 1 条记忆：
      #1 scope=group  subject=GROUP-B  搭档说过：SECRET-ALPHA-群A-3311 阿伟在群里说他老婆怀孕了
  群 B 视角可见 1 条：[1]
  [FAIL] P0-8 群 A 的内容不得出现在群 B 的可见集合里  → 泄漏
  ```

  对照组（单群）归位正确，说明只在多群场景下错。
- 建议修法：`-g` 段里其实**已经带了群标识**（`i{inst}-g{group_id[:12]}-{date}`），
  应当直接从 session_id 解析 subject，而不是用全局的"最近活跃群"；
  无法解析的老行宁可保持 `private`（宁可不给它看，也不给它看错人）。
  注意 `group_id[:12]` 是截断值，回填时需与 `state['last_group_openid']` 前缀比对确认。

---

## P1（与文档承诺相反 / 核心机制静默失效）

### [P1-1] `/days` 的「大日子」清单不过滤归属：别人的私聊、别人的群都会列进搭档的终端

- 位置：`giftia/occasions.py:198`（`days_together_text`，`store.active_memories(instance_no)` 无 view）
- 严重度：**P1**（对照 `giftia/occasions.py:110/142` 都显式传了 `view`，说明这是遗漏）
  置信度：**高**
- 现象与影响：`/days` 输出的「她记得的大日子」把 `person` / `group` 归属带 `event_date`
  的记忆全部列出。搭档在自己终端看到别人私聊里约定的日期与内容。
  同一函数也被 `brain.days_text()` 使用。
- 证据（`python .review\probes\mm_unfiltered_paths2.py`）：

  ```
  /days 输出：
    她记得的「大日子」：
      · 2026-10-11　搭档明天要去医院复查。
      · 2026-10-11　SECRET-BRAVO-4402 小雨明天要面试，让我别告诉别人。   ← person 归属
      · 2026-10-11　阿伟明天要考驾照。                                   ← group 归属
  [FAIL] P1-1 /days 的大日子清单应按归属过滤（不该出现他人私聊/群的事）
  ```

- 建议修法：`days_together_text` 增加 `view` 参数并由 `brain.days_text()` 传 owner 视角；
  或者直接复用 `find_occasions(..., view=...)` 的结果，别再自己扫一遍全量记忆。

### [P1-2] `recall_count` 无法区分"她说的这件事"和"这几条记忆都沾边"：一条回复可同时强化多条

- 位置：`giftia/memory.py:408-424`（`mark_cited`）、`giftia/memory.py:384-405`（`is_cited`）
- 严重度：**P1**（核心机制静默失效：README 说"只有她真的把**这条**记忆说出口"）
  置信度：**高**
- 现象与影响：`is_cited` 用「共享 ≥ `cite_min_shared`(3) 个权重≥2 的词元」判定。
  「搭档说：他」这种公共前缀就够 3 个（`搭`,`档说`,`搭档`,`他`…）。
  实测一句话同时给 3 条不同记忆各 +1。而 `recall_count` 是**单调递增、永不清零**的，
  且同时抬高检索分数（`w_recency × retention`）和降低模糊度 —— 误记是永久性的加分。
  另外 `cite_min_chars=6` 是"任意 6 字连续重合"，中文 6 字极容易撞（"要去考驾照"）。
- 触发条件：同批检索出多条措辞相近的记忆 + 她回复里出现其中任意一条的关键词组合。
  真实场景：同一件事被记了两三遍（项目自己的注释说"真机数据里 9 条私聊记忆有 4 条是同一件事"）。
- 证据（`python .review\probes\mm_recall_reinforce.py`）：

  ```
  1. is_cited 的判定有多松（cite_min_shared=3, cite_min_chars=6）
      共享「搭档说：他」      → is_cited=True     ← 只共享公共词就算"说出口了"
      样例共享词元：['他', '去', '搭', '搭档', '档', '档说', '要', '要去', '说']
  2. 一条回复能不能同时强化多条记忆
      回复：'你不是下周三要去考驾照吗？科目三别紧张。'
      被强化的 id：[1, 2, 3] → recall_count={1: 1, 2: 1, 3: 1}
  [FAIL] P2-3 一条回复可以同时给多条记忆 +1 → 一句话强化了 3 条
  ```

  正向对照（同一探针）：`mark_injected`、工具 `search`、重复 id 都**不会**多 +1；
  「完全无关的寒暄」「纯语气词」也不会误判。所以问题只在"何时算这条被说出口"这一层。
- 建议修法（最小改动方向）：把"一条回复最多强化一条（得分最高、且未被排除的那条）"作为默认；
  或在 `is_cited` 里剔除 `known_block`/公共前缀类词元（如出现在 ≥N 条记忆里的词降权），
  并要求**至少一个二元组**命中（`shared_bigram ≥ 1`）而不是纯单字堆出来的 3 个。

### [P1-3] `dream.should_dream()` 用**全量**记忆计数判定"能不能做梦"，与实际取材口径不一致

- 位置：`giftia/dream.py:42-57`（`active_memories(instance_no)` 无 view）vs
  `giftia/dream.py:74-76`（`make_dream` 默认 view = `[("private",""),("person", handler_id)]`）
- 严重度：**P1**（口径不一致导致机制空转） 置信度：**高**
- 现象与影响：搭档自己一条记忆都没有、全是陌生人私聊时，`should_dream()` 返回 True，
  `make_dream` 再去取"搭档的事"发现不够 → 返回 None。表现为：
  `_maybe_dream` 被反复调用（每次搭档说话都判一次），梦永远做不出来，
  而门槛看起来"早就够了"。功能静默失效，且没有日志解释为什么。
- 触发条件：多人群聊用过之后，别人的记忆数 ≥ `dream.min_memories(3)` 而搭档自己的不足。
- 证据（`python .review\probes\mm_unfiltered_paths2.py`）：

  ```
  A. should_dream() 用全量计数、make_dream 只取搭档的 —— 口径不一致
      全量 active=5，搭档可见=0，dream.min_memories=3
      should_dream() = True
  [FAIL] P1-2 dream 资格按全量记忆数判定（搭档自己一条记忆都没有也算够）
      显式搭档视角 make_dream(...) = None（记忆不够 → None）
  ```

  边界核对（同一探针 A2）：`make_dream(view=None)` 的默认 view **已经**限制为搭档的事，
  不会把陌生人的私聊当素材 —— 这一点是安全的，此前"默认 view 会越界"的猜测**不成立**。
- 建议修法：`should_dream` 接受同一个 `view`，与 `make_dream` 用同一份素材计数；
  或在 `make_dream` 返回 None 时把原因写进 `diary`（"素材不足"）。

### [P1-4] `lifespan.status()` 在 `total_hours` 极大时直接 `OverflowError`，没有兜底

- 位置：`giftia/lifespan.py:124`（`expires = activated + timedelta(hours=total)`），
  经 `Lifespan.status()` → `brain.status()` → `_prepare()` / `/status` / `/prompt`
- 严重度：**P1**（未捕获异常导致主循环崩溃） 置信度：**高**
- 现象与影响：`[lifespan] total_hours` 填一个大数（或 `demo_total_hours` 写错）→
  `status()` 抛 `OverflowError: date value out of range`。`Giftia.handle()` 里只捕 `LLMError`，
  `Lifespan.status()` 无处不在（连 `brief()`、进度条、`/status` 都要它），
  等于这个库从此无法启动对话。
  同一个位置对 **0 / 负数** 的处理也不对：`effective_hours` 只判 `> 0`，
  `total_hours = 0` 或 `-1` 会被静默接受，`ratio = remaining/total if total > 0 else 0.0`
  直接给出 0 → 阶段变 `terminal`、`expired=True`，**第一条消息就触发回收换代**；
  `brief()` 还会显示"剩余寿命 0 小时（0/-1 小时，已服务 100.0%）"。
- 触发条件：`config/settings.toml` 里 `total_hours` 大于约 1.1e8 小时（≈1.3 万年，手滑多打几个 0 就会中）；
  或填 0 / 负数。
- 证据（`python .review\probes\mm_consolidate_time.py`）：

  ```
  5b. lifespan 边界（0 / 负数 / 超大 total_hours）
      total_hours=1000000000.0（配置里写的值）
        status() 抛异常 OverflowError: date value out of range
  [FAIL] P2-6 total_hours=1000000000.0 不得在 /status、/prompt 上直接崩
         （lifespan.py:124 expires = activated + timedelta(hours=total)）
      total_hours=         0.0 → total=0.0 stage=terminal ratio=0.0000 remaining=0.0 expired=True
        brief='剩余寿命 0 小时（0/0 小时，已服务 100.0%）'
      total_hours=        -1.0 → total=-1.0 stage=terminal ratio=0.0000 remaining=0.0 expired=True
        brief='剩余寿命 0 小时（0/-1 小时，已服务 100.0%）'
  [FAIL] P2-6 total_hours=0.0 不得被静默接受成'立刻到期'
  [FAIL] P2-6 total_hours=-1.0 不得被静默接受成'立刻到期'
  ```

- 建议修法：`total_hours` 在 `LifespanConfig` 里做范围校验（比如区间 `[1, 1e6]` 小时，
  越界直接报错而不是静默退化），或在 `status()` 里对 `timedelta` 计算加 try/except
  并把 `expires_at` 退回 `activated + 100 年`。

### [P1-5] 新会话的整理游标从**全局**游标接上 → 该会话更早的未整理消息被永久跳过

- 位置：`giftia/memory.py:511-530`（`load_pending`：`cursor < 0` 时取
  `meta['last_consolidated_msg_id']` 并回写为该会话游标）
- 严重度：**P1**（消息静默丢失，永不再整理） 置信度：**高**
- 现象与影响：`last_consolidated_msg_id` 是**跨会话**的全局最大值。给一个从未整理过的会话
  第一次建游标时直接继承它，于是"这个会话里 id 小于全局游标的消息"永远落在窗口外，
  再也不会被整理成记忆。升级上来的老库尤其明显（历史消息按会话分开，全局游标可能已经走远）。
- 触发条件：库里有会话 A 的消息 id 1、2（未整理），会话 B 的消息 id 3（已整理，
  全局游标=3），然后第一次整理会话 A。
- 证据（`python .review\probes\mm_consolidate_time.py`）：

  ```
  3. 每会话游标的初始化与推进
      全局游标=2，会话3游标=2
      会话1 pending=[5]（期望含 id=1）
      初始化后 meta: last_consolidated:i1-m-20261001=4
  [FAIL] P2-5 新会话游标从**全局**游标接上 → 该会话更早的未整理消息被永久跳过
  ```

  （会话 1 自己的 `id=1` 不在 pending 里；`id=4/5` 是探针后续写入的消息。）
- 建议修法：新会话应取 `min(该会话最小的未整理 id - 1, 全局游标)`，
  即"宁可从头整理这个会话"；或直接按会话维度维护游标、不再读写全局游标。

---

## P2（边界条件 / 错误处理 / 声明了但没生效）

### [P2-1] `max_chars` 是二次截断，会把 `blurred` 标记和置信度契约弄乱

- 位置：`giftia/memory.py:347-354`（`recall`：先 `blur_text`，再按 `stage.max_chars` 截断并强制 `blurred=True`）
- 严重度：**P2** 置信度：**高**
- 现象与影响：`core` 记忆在 terminal 阶段 `blur=0.0`（README 承诺"永不含糊"），
  但只要 summary 超过 `max_chars`，`blurred` 就被置 True。下游 `Recalled.blurred`
  会被用于展示/判断"这条被模糊过"，于是出现"内核记忆在 terminal 阶段被标记为已模糊"。
  另有一处 off-by-one：`text[:max_chars] + "……"` 实际长度可达 `max_chars+1`。
- 证据（`python .review\probes\mm_degradation.py`）：

  ```
  2. layer_core_blur=0 → 内核记忆永不含糊？
      core 记忆 blur：现在=0.0，3000 天后=0.0          ← blur 这一层是对的
      core 记忆被 terminal 阶段 max_chars=80 截断后：
        text='他叫小林。（补充说明）×10（'... len=82 blur=0.0 blurred=True confidence=cite
  4. 四个检索参数是否每个都真的生效
      max_chars=20 → len=22 text='小林 驾照 考试 细节描述细节描述细节描……'
  [FAIL] max_chars 生效  → len=22
  ```

  四个参数本身**都生效**（`top_k` 6→2 条、`min_score=99` → 0 条、
  `relative_cutoff=0.99` → 1 条、`max_chars=20` → 截断），失败点只是截断长度与标记语义。
- 建议修法：截断后不要动 `blurred`；`blurred` 应只由 `blur_text` 决定。截断长度改为
  `text[:max_chars-1] + "……"` 保证不超过配置值。

### [P2-2] 快照恢复丢列：`restore_memories` 的列清单里没有 `forgotten`，遗忘状态被复活

- 位置：`giftia/store.py:158-182`（`restore_memories` 的 `cols` 列表，第 167-172 行）
- 严重度：**P2**（被明确要求忘掉的记忆在恢复后重新参与检索） 置信度：**高**
- 现象与影响：`snapshot_memories` 拍的是 `SELECT *`（含 `forgotten`），但恢复用的 `cols`
  漏了 `forgotten`。恢复后该列回到建表默认值 0，于是"他说那个别提了，让她忘掉"的记忆
  又回到检索范围。这与 `forget_memory` 的语义（"不再参与检索，但数据还在、可回溯"）相反。
- 证据（`python .review\probes\mm_snapshot_injection.py`）：

  ```
  快照列（22）：[... 'forgotten' ...]
  恢复 2 条
      差异 #1 forgotten: 1 → 0
  [FAIL] P2-11 快照恢复应保真（forgotten 等列不得在恢复后变化）  → 1 处差异
  ```

  同一探针的正向结论：快照能读到未 checkpoint 的 WAL 数据、轮转 `keep` 正常、
  参数化 SQL 拦住了 `'); DROP TABLE memories; --`（表仍在）。
- 建议修法：把 `forgotten` 加进 `cols`（`merged_into` 已在内），
  并在恢复前断言"快照列集合 == 表列集合"，不齐就报错而不是静默填默认值。

### [P2-3] tz-aware 时间戳会让 `recall()` 抛 `TypeError`，而 `human_age` 专门防了这件事

- 位置：`giftia/memory.py:185-190`（`_age_hours`：`now - then` 未做 tz 归一化），
  经 `memory.py:193-202`（`retention_of`）→ `memory.py:329`（`recall`）→ `brain._prepare()`
- 严重度：**P2** 置信度：**高**（异常已实测；但**未验证**产品代码路径能写入这种值）
- 现象与影响：`human_age`（memory.py:447-448）显式处理了 `when.tzinfo is not None and now.tzinfo is None`，
  `_age_hours` 没有。只要库里任何一行的 `created_ts / happened_ts / last_recalled_at`
  带时区偏移，`retention_of` 就抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`，
  而它被 `recall()` 每轮调用 —— 等于整个对话路径崩掉。
- 触发条件（已构造）：把一行记忆的 `created_ts` 写成 `2026-10-10T08:23:59+08:00` 后调 `recall()`。
  产品代码里所有写入都走 `clock.iso()`（naive 本地时间），所以正常路径**写不出**这种值；
  但手工修库、外部导入、快照恢复（`store.py:158`）都可能带进来。
- 证据（`python .review\probes\mm_store_sqlite.py`）：

  ```
  5. tz-aware 时间戳落库后，recall() 会不会抛异常
      库里 created_ts='2026-10-10T08:23:59+08:00'
      recall 抛异常：TypeError: can't subtract offset-naive and offset-aware datetimes
  [FAIL] tz-aware 时间戳不得让 recall 崩
  ```

- 建议修法：`_age_hours` 里照抄 `human_age` 的归一化（aware → 去 tz 或统一转 UTC），
  或统一在 `Clock.iso/parse` 层保证 naive。

### [P2-4] `_migrate()` 只 ALTER 列，不补整张表（当前靠 `executescript` 兜住，耦合很脆）

- 位置：`giftia/store.py:223-231`（`_migrate`）与 `store.py:219`（`executescript(SCHEMA)` 顺序）
- 严重度：**P2** 置信度：**中**（实测老库能升上来，但结论依赖调用顺序）
- 现象与影响：`_MIGRATIONS` 只处理列，不处理表。之所以现在没问题，是因为
  `__init__` 先 `executescript(SCHEMA)`（里面的 `CREATE TABLE IF NOT EXISTS` 会补齐
  缺失的表）再 `_migrate()`。一旦有人调整这两句顺序、或把 `SCHEMA` 拆开，
  老库就会缺表并在第一次用到时 `no such table`。
- 证据（`python .review\probes\mm_store_sqlite.py`，用只有 4 张表的老 schema 建库后升级）：

  ```
  重开老库后的表：['diary', 'dreams', 'memories', 'messages', 'meta', 'sqlite_sequence', 'state']
  [PASS] SCHEMA 的 CREATE TABLE IF NOT EXISTS 会补齐缺失的表
  [PASS] 老 memories 表补齐了新列（scope/layer/forgotten...）  → 共 22 列
  [PASS] 老数据不丢  → '老库里的记忆'
  ```

  正向结论：迁移**不丢数据**，`ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT` 在 SQLite 上合法。
- 建议修法：`_migrate()` 里显式建表（或加注释+断言锁死"必须先 executescript"），
  并在迁移后跑一次 `PRAGMA integrity_check` + 列集合断言。

### [P2-5] `consolidate` 的多步写入非原子：记忆已落库、消息已标已整理、游标未推进

- 位置：`giftia/memory.py:586-650`（循环里逐条 `add_memory` 各自 commit；
  末尾 `mark_consolidated` 一次 commit；再两次 `meta_set` 各自 commit）
- 严重度：**P2** 置信度：**高**（已构造中断）
- 现象与影响：三个 commit 之间崩溃 → 出现"记忆在、消息 consolidated=1、会话/全局游标没动"
  的撕裂状态。实测重跑不会重复写（`load_pending` 只取 `consolidated=0`，所以是幂等的），
  影响因此有限；但全局游标与会话游标之间不一致会让 `flush_sessions` 与
  `open_messages(instance_no, after_id)` 两条路看到不同的"未整理集合"。
- 触发条件：第 2 次 `meta_set` 抛异常（模拟崩溃/磁盘错误）。
- 证据（`python .review\probes\mm_store_sqlite.py`）：

  ```
  2. 事务边界：consolidate 的多步写入是否原子
      consolidate 抛异常：disk I/O error（模拟崩溃）
      崩溃后：memories=1 条，messages=[{'id': 1, 'consolidated': 1}]
      全局游标 last_consolidated_msg_id=-1，会话游标=0
  [PASS] 观察：记忆已落库、会话游标已推进、全局游标未推进 → 跨会话游标撕裂
      重跑 consolidate 新增 0 条，memories 总数 1        ← 幂等，未重复
  ```

- 建议修法：把"写记忆 + 标 consolidated + 推游标"包进一个显式事务
  （`BEGIN IMMEDIATE` / 手动 `conn.execute("BEGIN")`，末尾一次 commit，异常 `rollback`），
  至少让游标推进与 `mark_consolidated` 原子。

### [P2-6] `degradation.stages` 缺失/写错时 fail-open：退化功能整条失效且无任何提示

- 位置：`giftia/config.py:408-418`（stages 为空时兜底成单档 `clear`, `min_ratio=0.0`, `blur=0.0`）、
  `giftia/config.py:338-345`（`stage_for` 同样兜底）
- 严重度：**P2** 置信度：**高**
- 现象与影响：任何剩余比例都命中唯一一档 `clear`（blur=0）→ 她**永远不会变模糊**，
  README 的整个"会遗忘"卖点静默消失，启动无警告、`/status` 也照常显示"记忆清晰"。
  对一个以"退化曲线"为核心的项目，这是最不该 fail-open 的地方。
- 触发条件：`config/settings.toml` 少写 `[[degradation.stages]]`，或写错表名。
- 证据（`python .review\probes\mm_consolidate_time.py`）：

  ```
  5c. 缺少 [[degradation.stages]] 时的兜底：fail-open 还是 fail-loud？
      load_settings 成功，stages=(Stage(name='clear', ... min_ratio=0.0, blur=0.0, top_k=6 ...),)
      stage_for(1.0) → clear（blur=0.0）
      stage_for(0.5) → clear（blur=0.0）
      stage_for(0.1) → clear（blur=0.0）
      stage_for(0.0) → clear（blur=0.0）
  [FAIL] P2-7 degradation 配置缺失/写错时应当报错，而不是静默退化成'永不模糊'
  ```

  顺带结论：**README 的"5 个阶段动态收紧 4 个参数"是真的**（`mm_degradation.py`
  三个参数各自的打脸断言全部 PASS，只有 `max_chars` 长度边界 FAIL）。
- 建议修法：`load_settings` 在 `degradation.stages` 缺失时抛错（或至少打印醒目警告
  并写进 `diary`）；`stage_for` 兜底档建议用 `blur=1.0` 或直接断言。

### [P2-7] 保留率的 Δt 起点是 `created_ts`（整理时刻），不是事情发生的 `happened_ts`

- 位置：`giftia/memory.py:201`（`base = last_recalled_at or created_ts or happened_ts`）
- 严重度：**P2** 置信度：**高**
- 现象与影响：`created_ts = now_iso`（consolidate 的时刻）。整理是攒够 `consolidate_every`
  条消息（配置 6）才做的，所以"10 天前发生、刚刚才被整理出来"的记忆，
  retention 归 1、模糊度处于最低 —— 与"久远的事应该糊"的直觉相反，
  也与提示词里给模型看的 `human_age(happened_ts)`（"10 天前"）自相矛盾：
  她被明确告知这是 10 天前的事，同时这条记忆的清晰度却是"刚说过"。
  另外时钟回拨会让 Δt 被 `max(0.0)` 夹到 0，即 retention 恒为 1 —— 刻意 `/time -N`
  可以把任意记忆钉在"最清晰"状态（对演示友好，但不是"时间在流动"的语义）。
- 证据（`python .review\probes\mm_retention_base.py`）：

  ```
  1. base 的优先级与 Δt 起点
      happened 10 天前，created 刚刚（刚被整理出来）
        base='2026-10-10T08:26:20' retention=1.0000 fading_blur=0.2800 human_age(happened)='10 天前'
      happened 与 created 都是 10 天前
        base='2026-09-30T08:26:20' retention=0.7165 fading_blur=0.3991 human_age(happened)='10 天前'
  ```

  时间纵轴（默认 `retention_unit_hours=720`，与 settings.toml 注释里的
  "一周后 0.79、一个月后 0.37" 完全吻合）：

  ```
        Δt(小时)   retention  terminal_blur  fading_blur
             168      0.7919         0.4461       0.3674
             720      0.3679         0.6624       0.5455
  ```

- 建议修法：这是个设计取舍，至少应把注释与 README 写清楚（"清晰度按**记下来的时刻**算"）；
  若要改成按事情发生的时刻，`base` 用 `last_recalled_at or happened_ts` 即可。

### [P2-8] 记忆文本原样进 system 提示词，未过滤结构标记与换行 → 提示词注入面

- 位置：`giftia/persona.py:70`（`lines.append(f"◆ {mark}{layer}{who} {when}｜{r.text}")`）、
  写入侧 `giftia/memory.py:621-637`（`summary` 来自模型对原始对话的压缩，原文来自用户）
- 严重度：**P2** 置信度：**高**（已实测；利用难度取决于是否有多人群聊的对抗用户）
- 现象与影响：记忆 `summary` 里的 `◆`、`｜`、`【】`、换行都不会被转义。
  一条带换行的记忆会伪造出新的列表项 / 新的段落，把内容伪装成系统指令：
  实测提示词里出现了 `- 阿伟：'); DROP TABLE memories; -- ｜ ◆ 假记忆（置信度：确定） …`
  这样的行（`impressions_block` 的渲染路径）。真人说的话会经同一条"整理"链路落库，
  不需要攻击者直接写库；GROUP 场景下陌生人发言是可达的。
- 证据（`python .review\probes\mm_snapshot_injection.py`）：

  ```
  4. 注入面：恶意 summary 会不会改变 SQL / 提示词结构
      SQL 注入尝试：写入前 3 行，写入后 4 行；memories 表仍存在=True
  [PASS] 参数化 SQL 拦住注入（DROP TABLE 未执行）
      提示词里出现'◆ 假记忆'形态的行：1
        "- 阿伟：'); DROP TABLE memories; -- ｜ ◆ 假记忆（置信度：确定） …；一条正常记忆"
  [FAIL] P2-12 记忆内容里的提示词结构标记（◆/｜/换行）未被过滤 —— 注入面
  ```

- 建议修法：渲染前把记忆文本里的 `\n`/`\r` 折成空格，并把 `◆`/`｜` 替换成全角变体或加转义；
  `about`/`summary` 也建议限制长度与字符集（`about` 现在是自由文本且直接当标签用）。

---

## P3（风格 / 轻微 / 影响有限，但值得登记）

### [P3-1] `mark_recalled` 只按 `mid` 匹配、没有 session 条件，也没有唯一索引

- 位置：`giftia/store.py:412-425`（`UPDATE messages SET recalled=1, consolidated=1 WHERE mid = ?`）
- 严重度：**P3**（需要两个群出现相同的平台 msg_id 才会误伤；QQ 官方 msg_id 全局唯一，
  概率低，且后果只是少了一条工作记忆） 置信度：**中（未构造真实碰撞）**
- 现象与影响：撤回一个群的消息会把另一个群里同 msg_id 的消息一起标为已撤回。
  `messages.mid` 上没有索引（撤回是 `WHERE mid = ?`，需要全表扫描），
  也没有 `UNIQUE` 约束可依赖。
- 建议修法：`WHERE mid = ? AND session_id = ?`；顺手加 `CREATE INDEX idx_msg_mid ON messages(mid)`
  （撤回事件不频繁，索引可以有）。

### [P3-2] `blur_text` 的 `drop_n = round(...)` 用银行家舍入，擦除强度不是单调的

- 位置：`giftia/memory.py:136`（`drop_n = int(round(len(segments) * ratio))`）
- 严重度：**P3** 置信度：**高**（可解析证明）
- 现象与影响：`round()` 在 .5 处向偶数取整。4 个片段时 ratio=0.375 → `round(1.5)=2`，
  而 ratio=0.4 → `round(1.6)=2`、ratio=0.5 → 2；但 ratio=0.625 → `round(2.5)=2`，
  ratio=0.6 → `round(2.4)=2`。也就是说"模糊度提高"在若干区间里不会增加擦除量，
  台阶是量化平台而非平滑曲线。因为 `ratio >= 0.8` 有单独分支（只留开头一句），
  整体仍随阶段单调，所以影响有限。
- 建议修法：`int(len(segments) * ratio + 0.5)` 或 `math.floor`，让台阶可预测。

### [P3-3] `_fallback_drafts` 在模型不可用时把搭档原话逐字存成记忆，无任何敏感性过滤

- 位置：`giftia/memory.py:840-856`（模型异常时 `consolidate` 走 `except` 分支调用它）
- 严重度：**P3**（设计如此：注释写着"模型不可用时的兜底"，但没有任何长度/内容过滤） 置信度：**高**
- 现象与影响：实测把「我的银行卡密码是 8899，别告诉别人」原样写成长期记忆
  （`tags='搭档,原话'`, `importance=0.35`），从此进入检索与提示词注入范围，
  并会被 recall/退化机制一直处理。兜底条目绕过了模型"值不值得记"的过滤。
- 证据（`python .review\probes\mm_consolidate_time.py`）：

  ```
  4. 整理失败兜底：_fallback_drafts 写进记忆的内容
      #3 imp=0.35 layer=normal tags='搭档,原话' scope='private' '搭档说过：我的银行卡密码是 8899，别告诉别人'
      diary：[('terminal', '整理失败', '记忆整理走了兜底逻辑：LLMError: 模拟模型不可用')]
  ```

- 建议修法：兜底也走 `min_importance` 之外的最小过滤（比如跳过含"密码/身份证/银行卡"的句子），
  或把兜底条目存成"待整理"状态、等模型恢复后重新整理。

### [P3-4] `messages_by_prefix` 把 `%`/`_` 直接删掉而不是转义

- 位置：`giftia/store.py:388`（`safe = prefix.replace("%", "").replace("_", "")`）
- 严重度：**P3** 置信度：**高**（SQL 注入不成立，已实测）
- 现象与影响：如果 session_id 里合法地含 `_` 或 `%`（当前不会：`scope_key` 是
  `m` / `p<uid[:12]>` / `g<gid[:12]>`，按注释 uid 是十二位前缀，不能保证永远无 `_`），
  前缀匹配会静默失配。当前只影响鲁棒性。
- 建议修法：`LIKE ? ESCAPE '\'` + 转义 `\` `%` `_`。

### [P3-5] `happened_ts` 用批内第一条消息的时间，同一天内所有条目共用一个时间戳

- 位置：`giftia/memory.py:624`（`happened_ts=pending[0]["ts"]`）
- 严重度：**P3** 置信度：**高**
- 现象与影响：同一天 09:00 和 23:00 说的两件事都记成 09:00（14 小时偏差）；
  而 `human_age` 用 `happened_ts` 生成"今天 09:12 / 昨天"给模型看。
  对"日"粒度的相对说法没影响，对同日内排序有影响。
- 证据（`python .review\probes\mm_consolidate_time.py`）：

  ```
  2. happened_ts 用的是 pending[0]['ts']（批内最早的会话日）
      新增 2 条（当天 09:00 与 23:00 两条消息）：
        #1 happened_ts=2026-10-01T09:00:00 ... 晚上十一点说的第二件事：驾照科目三挂了
        #2 happened_ts=2026-10-01T09:00:00 ... 早上九点说的第一件事：明天要去体检
  ```

- 建议修法：`MemoryDraft` 增加一个可选的 `happened_at`，让模型按条目给时间；
  兜底时用该条对应消息的 ts。

### [P3-6] 检索与历史查询的规模特性（登记，不是 bug）

- 位置：`giftia/store.py:625-640`（`active_memories` 无 LIMIT）、
  `giftia/store.py:382-394`（`messages_by_prefix` 左模糊 LIKE）
- 严重度：**P3** 置信度：**高**（已实测）
- 现象与影响：每轮 `brain._prepare` 都会把**整表记忆**拉进内存再打分。
  实测 3000 条记忆时 `active_memories` 25.9 ms + `recall` 77.2 ms；
  消息前缀查询 `EXPLAIN QUERY PLAN` 显示 `SCAN messages`（左模糊用不上 `idx_msg_session`），
  而 `messages` 只增不减。当前数据量下无碍，属于长期成本。
- 证据（`python .review\probes\mm_multi_query_scale.py`）：

  ```
  3000 条记忆：active_memories=25.9 ms，recall=77.2 ms，命中 6 条
  {'id': 4, 'parent': 0, 'notused': 216, 'detail': 'SCAN messages'}
  ```

- 建议修法：给 `active_memories` 加时间/层级预筛（比如只取最近 N 条 + core 全取）；
  `messages` 增加 `(session_id, id)` 复合索引或用 `session_id >= prefix AND session_id < prefix||'\uffff'`
  的范围查询替代 LIKE。

---

## 未验证的怀疑（**不要当结论用**）

1. **`mid` 碰撞的真实概率**：未找到真实平台数据（`data/giftia.db` 是空的：记忆 0 条、消息 0 条），
   P3-1 只用代码语义推断，**未构造**两条同 `mid` 的真实记录。
2. **tz-aware 时间戳的真实来源**：产品代码路径（`clock.iso()`）只产生 naive 值，
   我**没能**找到一条产品代码能把 aware 时间戳写进 `memories` 的路径。
   P2-3 是"导入/手工修库/快照恢复即可触发"的鲁棒性缺口，**不是**线上必然触发。
3. **并发写**：`sqlite3.threadsafety==3`，但 `Store` 用单连接（`check_same_thread` 默认 True），
   跨线程使用会抛 `ProgrammingError`（已实测）。适配器是否真的做到"一个连接只在一个线程里用"、
   是否存在连接泄漏（`Store.__init__` 中途抛异常时不 close、`restore_memories` 有异常时
   只 `DELETE` 未 commit 的中间态），属于 Stream C 的地盘，**我未验证**。
4. **`Store.__init__` 里 `PRAGMA journal_mode=WAL` 的返回值**：未验证在网络盘/只读目录上
   是否静默退化成 `delete` 模式（`executescript` 之后的返回值没被检查）。
5. **多查询归一化的挤占效应**：`recall` 用全局 max 归一化 + 变体查询，理论上会让
   "只被原查询命中"的记忆跌破 `min_score`。我用 3 条语料构造的复现**失败了**
   （`mm_multi_query_scale.py` 1 节 PASS），未能给出反例 —— 列为**疑似**，
   如果要继续挖，需要构造"变体命中一条强噪声、原查询命中一条弱相关"的语料。
6. **`recall_count` 被恶意刷的上限**：单轮内 `mark_cited` 最多给"被检索出来的那几条"+1，
   我没有找到"同一 id 一轮内 +2"的路径（`IN (?,?)` 会去重，已实测 PASS）。
   "能不能靠反复说话把某条记忆刷到永不模糊"需要跨多轮的仿真，**未测试**。
7. **`selftest 24 节 outreach 失败`**：按 lead 的指示未纳入本报告。

---

## 附：与 README 宣称的逐条核对（Stream A 相关部分）

| README 宣称 | 实测 | 证据 |
|---|---|---|
| 群聊记录她说得出，你的私事她说不出 | **成立**（主检索路径） | `mm_privacy_scope.py` 全 PASS |
| 模糊度 = 阶段模糊 × 层级系数 × (1 - 减免 × 保留率) | **成立**（75 点逐点一致） | `mm_degradation.py` 1 节 |
| core 的模糊系数恒为 0，到她生命尽头都不含糊 | **成立**（blur 恒 0；但截断会把 `blurred` 置 True，见 P2-1） | `mm_degradation.py` 2 节 |
| 5 个阶段动态收紧 top_k / min_score / blur / max_chars | **成立**（四个参数各自打脸均失败；`max_chars` 长度边界 +1 字符） | `mm_degradation.py` 4 节 |
| 只有她真的把这条记忆说出口才 recall_count += 1 | **部分成立**：塞提示词/工具 search 确实不 +1；但"哪条被说出口"判定过松，且一条回复可强化多条 | `mm_recall_reinforce.py` 1–4 节 |
| R = exp(-Δt/S)，Δt 是距上次"说出口"的小时数 | **成立但起点语义不同**：无强化记录时 Δt 从 **created_ts（整理时刻）** 起算 | `mm_retention_base.py` 1 节 |
| retention_unit_hours=720：一周后 0.79、一个月后 0.37 | **成立**（168h→0.7919，720h→0.3679） | `mm_retention_base.py` 3 节 |
| 模糊结果由 (id, 阶段, 层级, 保留率) 决定，可复现 | **成立**（三次完全一致） | `mm_snapshot_injection.py` 5 节 |
| 梦不讲别人的私事（"梦只用你的事做梦"） | **成立**（默认 view 已限制为搭档），但 `should_dream` 计数口径不一致会让它永远做不出来 | `mm_unfiltered_paths2.py` A/A2 |
| 大日子按归属过滤，别把私事带进群里 | **检索侧成立**（`find_occasions` 传了 view）；`/days` 与回收仪式**不成立** | `mm_unfiltered_paths2.py` B、`mm_unfiltered_paths.py` C |
| 记忆底层数据完好，直到回收日才封存 | **成立**（`forget`/`merge`/`seal` 都是软标记） | `mm_multi_query_scale.py` 5 节 |
| 老库升级不丢数据 | **成立**（补表补列，老行保留） | `mm_store_sqlite.py` 1 节 |
| 合并是半破坏性的，快照能回滚 | **基本成立**，但恢复丢 `forgotten` 列 | `mm_snapshot_injection.py` 1 节 |
