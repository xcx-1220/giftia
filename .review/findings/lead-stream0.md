# Lead 独立发现（Stream 0：横切 + 关键路径）

> 审查者：Team Lead。证据全部来自本机实跑，探针在 `.review/probes/lead_*.py`，
> 原始日志在 `.review/evidence/`。严重度定义见 `.review/PROTOCOL.md`。
> 本文件只写 Lead 亲自验证过的条目；队友的发现见各自文件。

## 0. 基线：测试套件的真实状态

跑法（沙箱下必须用这个垫片，原因见 PROTOCOL.md）：

```powershell
python .review\acceptance.py
```

| 套件 | README 宣称 | 实测 | 退出码 | 耗时 |
|---|---|---|---|---|
| `tools/selftest.py` | 218 项（§1）/ 270 项（§10）/ setup.sh 说 187 项 | **302 项：通过 299，失败 3** | 1 | 20.6s |
| `tools/ws_selftest.py` | 17 项 | 17 项通过 | 0 | 15.6s |
| `tools/qq_protocol_selftest.py` | 69 项 | **72 项**通过 | 0 | 9.3s |
| `tools/audit.py` | 11 项 | 11 项通过（另有 4 条提示） | 0 | 1.7s |
| `tools/demo_life.py` | 无数字 | 正常跑完 | 0 | 0.6s |
| `tools/show_prompt.py` | — | 正常跑完 | 0 | 0.4s |
| `adapters/qq_official.py --dry-run` | — | 正常跑完 | 0 | 0.3s |

原始证据：`.review/evidence/summary.json` 与各 `.log`。

---

### [P1] `selftest.py` 有 3 项失败，README 却宣称全绿（当前代码库不自洽）

- 位置：`tools/selftest.py:791,797,801`（第 24 节「主动联系」）；实现侧 `giftia/outreach.py:181-199`
- 严重度：P1；置信度：**高**（本机稳定复现，连跑 4 次结果一致）
- 现象：`python .review\run_tests.py tools/selftest.py` → `通过 299 项，失败 3 项`，退出码 1。
  失败项：`默认关闭时她不会主动找你`、`刚说过话时没有主动理由`、`闲了 4 天 → 她想你了`（实际 reason 是 `mood_share`）。
- 根因（已定位，并经 behavior-checker 更正我的初判）：`giftia/outreach.py` 里 `mood_share` 分支
  （第 181-199 行）排在 `missing_you`（第 228-244 行）之前，**且不检查"距上次说话多久"**；
  `force=True` 时它也不看 `enabled`（这条只在 `/outreach` 预览里用，不是缺陷）。
  当天这一任的确定性心情掷骰结果是 `low`（「有点低落」），于是"刚说完话"也立刻给出一个 `mood_share` 计划。
  我一开始怀疑"`enabled=False` 却仍返回计划"——**这个怀疑是错的，已撤回**：
  `config/settings.toml:193` 把 `outreach.enabled` 覆盖成了 `true`，
  测试却假设"出厂默认关闭"（`giftia/config.py:144` 的代码默认值确实是 `False`）。
  更正依据：`.review/findings/behavior-checker.md` §0 与探针 `beh_outreach.py` A1b。
- **这 3 项还会随日期红/绿摇摆**：掷骰种子是 `crc32("日期|实例号")`，calm 的日子（约一半）三项全绿，
  非 calm 的日子红 3 项。所以它不是稳定回归信号——CI 里会随机变红/变绿。
- 证据（`.review/probes/lead_d1_outreach.py`）：

  ```
  [day+0] 日期=2026-10-10 剩余=100.0h mood.key='low' label='有点低落'
          默认关闭→plan=OutreachPlan(reason='mood_share', ...)
          刚说过话 force=True → 'mood_share'
          闲 4 天  force=True → 'near_end'
  ```

- 影响：① 交付出去的仓库"测试不绿"，与 README `python tools/selftest.py` 的描述矛盾；
  ② 行为侧：README §3.5 的理由表（7 条）和 `outreach.plan()` 的实现已经分叉——
  `mood_share` 会**抢在 `missing_you`（"太久没见"）之前**触发，而"太久没见"是这个项目最核心的情感机制之一。
- 建议：先决定契约（是实现漏了 idle 判定，还是 README/测试过期），再让三者对齐；
  测试里 3 条断言应改成对"契约"的断言，而不是对某个 reason 的硬编码。

### [P1] README / setup.sh 里的测试项数有三套互相矛盾的数字

- 位置：`README.md:23`（218 项）、`README.md:145/754`（218 项里有 19 项）、`README.md:649`（270 项）、
  `README.md:785`（270 项）、`deploy/setup.sh:76`（"187 项测试"）、`deploy/setup.sh:77`（调 selftest）
- 严重度：P2；置信度：**高**
- 现象：真实项数 = **302**（299 通过 + 3 失败）。四处宣称没有一处对得上。
- 影响：新用户按 `setup.sh` 的自检描述核对会觉得"数字不对"，而它恰恰是部署时的"环境自检"文案。
- 建议：把项数从文案里去掉（改成"跑一遍 `tools/selftest.py`"），或在 CI 里自动同步。

---

### [P1] `min_score` 不是绝对相关度门槛，而是被归一化放大的相对门槛

- 位置：`giftia/memory.py:327`（`bm = best_raw[i] / top`）、`memory.py:331-338`（`if score < stage.min_score: continue`）
- 严重度：P2（设计取舍，但与 README §4 表格的描述不符）；置信度：**高**
- 现象：`bm` 除以**本次查询的候选集最高分**，所以"首名恒为 1.0"，`score` 上限约 1.45。
  两个后果：
  1. 候选集只有 1~2 条时，`min_score`（最高 0.55）几乎永远拦不住任何东西；
  2. 同一条记忆的分数**取决于旁边有哪些记忆**：加一条更详尽的同类记忆，它可能整体掉出结果。
- 证据（`.review/probes/lead_d4_min_score.py`）：

  ```
  ── 实验 1：'搭档的驾照放在抽屉里。'（1 个二元组重合、重要性最低、terminal 阶段）
    terminal(min_score=0.55) 召回 [(1, 1.144)]        ← 0.55 的门槛没拦住
  ── 实验 2：加一条'搭档下周三要考驾照'后
    2 个候选 → 召回 [(2, 1.337)]                      ← 弱的那条掉出去了
  ── 实验 4：原始 BM25 = [0.6539, 8.6798] → 归一化后 [0.0753, 1.0]
  ```

- 影响：README §4 表格把 `min_score` 描述成"收紧"的闸门，实际它主要对**候选集较大**的情况起作用；
  真正的第一道闸是 `EVIDENCE_GATE`（`memory.py:49`，至少一个二元组或两个单字重合）。
- 建议：要么把 `min_score` 改成对**原始 BM25 分**的绝对阈值（`raw < min_raw` 直接丢），
  要么在文档里明确"min_score 是相对门槛"。当前写法让调参者以为自己在调绝对严格度。

### [P2] 内核记忆（core）没有保底注入通道——"她记得你是小林"依赖你正好提到名字

- 位置：`giftia/memory.py:282-367`（`recall()`）、`giftia/brain.py:619-620`
- 严重度：P2；置信度：**高**
- 现象：`recall()` 对 `layer` 的唯一处理是给排序加 `w_layer`（core=1.0），
  但**候选必须先通过词面证据门槛**。查询里没有"小林/名字/称呼"这类字时，core 记忆根本进不了打分。
- 证据（`.review/probes/lead_d2_memory_core.py`）：

  ```
  [A] terminal 阶段 core 记忆 blur = 0.0  （README 说恒为 0）→ 成立
  [B] terminal 阶段问'今天天气怎么样'，召回 [4]（一条普通记忆）
      → 内核记忆（名字）没有被召回：本轮提示词里不会有「她记得你是小林」
  ```

  再加一条对照：库里只有 `搭档叫小林` 时，问"你还记得我要考什么吗" → 召回 `[]`（`.review/probes/lead_d4_min_score.py` 实验 6）。
- 影响：README 关键设计 3 承诺"到终端服务迫近时，她记得你是小林、记得你对她很重要"。
  实际上，只有当用户那句话里恰好出现名字/称呼相关字面时才会注入；
  她说"我不知道你叫什么了"和"我当然记得你叫小林"之间的差别，取决于一句话的字面，而非层级。
- 建议（二选一，都很小）：① 在 `build_system_prompt` 里加一格"你确定知道的事"，
  常驻注入 core 层记忆（限 2~3 条、`max_chars` 收紧）；② 在 `recall()` 里给 core 层一条保底通道。

### [P2] `min_score`/`top_k` 在真实运行中的表现与 README §4 表格的因果关系不完全一致

- 位置：`README.md:531-537` 表格 vs `memory.py:337-348`
- 严重度：P3；置信度：**高**
- 证据（`.review/probes/lead_d3_recall_gates.py`，30 条拥挤候选集）：
  `top_k` 五档**确实是有效封顶**（6/5/4/3/2 全部触顶），`relative_cutoff` 也生效（0.99 → 只剩 2 条）。
  但 `min_score` 在同一个候选集里从未起过作用（最低分 1.471 已远高于 0.55）。
- 结论：README 表格里"动态收紧 top_k、min_score、模糊比例"三条中，前两条与第三条 `blur` 是真闸门，
  `min_score` 是**兜底**（只在候选集很大或分数被拉低时才可能生效）。

### [P2] `GET /gateway` 之外，README §5 的"人设固定 → 命中缓存"只对前 ~900 字符成立

- 位置：`giftia/persona.py:107-114`（core → hint → naming 的顺序）
- 严重度：P3；置信度：**高**
- 证据（`.review/probes/lead_d5_prompt_prefix.py`，8 轮真实对话）：

  ```
  第 1 轮与最后一轮的公共前缀 = 927 字符（人格内核 909 字符）
  首次分叉点在【如果有人坚持说你是程序】之后、【你怎么称呼他】之前
  ```

  也就是说稳定前缀 ≈ 人格内核本身；`hint_text(name)`/`naming_text(name)` 既然要填名字，
  名字一变前缀就断。同一任的她名字固定后前缀就稳定了，这个数字**不影响结论**，
  只是把 README §5 的"那部分几乎免费"从"整个 system"修正为"约 900 字符的人格内核"。
- 建议：文档层面说明缓存命中的是"人格内核那一段"。

---

## Lead 的独立复现（对队友 P0 的二次验证：**不复用他们的探针**）

审查纪律要求"同一结论要有与报告者不同的证据来源"。以下三条 P0 我用自己的脚本重跑过，
产品代码零改动、`data/giftia.db` 未被触碰。

探针：`.review/probes/lead_v1_p0_verify.py`、`.review/probes/lead_v2_p0_commands.py`

### [P0-1 复核] 回收仪式写出的回忆录里确实带别人的私聊（memory-man P0-1）

```
库里有 3 条不同归属的记忆：private / person(小雨) / group(群A)
  [FAIL] 回忆录 .md 不含小雨的私聊
  [FAIL] 封存 JSON 不含小雨的私聊
  [FAIL] /memoir（diary 表）不含小雨的私聊
  泄漏行 → 　　2026-10-10T08:28：SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
  泄漏行 → - `2026-10-10T08:28`（重要性 0.90）SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
```

代码事实：`giftia/recycle.py:102` 是 `store.active_memories(instance_no)`，
而 `store._view_clause(None)` 返回 `("", [])` = **不过滤**（`store.py:601-623`）。
→ **P0 确认**。

### [P0-2 复核] 老库 scope 回填把群 A 的消息标成群 B（memory-man P0-2）

```
回填前： #1 i1-gGROUP-A-20261001 scope=private subject=''
        #2 i1-gGROUP-B-20261002 scope=private subject=''
backfill 更新 2 条：
  #1 i1-gGROUP-A-20261001 scope=group subject='GROUP-B' ← 归属错！session 里写的是 GROUP-A
  #2 i1-gGROUP-B-20261002 scope=group subject='GROUP-B' OK
[FAIL] 每条群消息的 subject 是否与 session_id 里的群标识一致（错 1 条）
```

注意 `session_id` 里其实**已经带了群标识**（`i1-gGROUP-A-`），代码却用了全局
`state['last_group_openid']`。→ **P0 确认**。

### [P0-3 复核 + 合并] "谁是搭档"是可变的，而 `private` 记忆不带 openid

这一条把两个 stream 的发现合并成**同一个根因**（brain-checker 报的"群指令闸门"和
protocol-auditor 报的"解绑后陌生人拿到私事"）：

```
A. 指令身份闸门（adapters/qq_official.py）
   第 768 行：if owner and user_id != owner:
   第 780 行：if owner and user_id != owner:
   第 815 行：if owner and user_id != owner:
   → owner 为空时条件为 False，闸门不生效（共 3 处）

B. handler_id 为空（刚 /unbind；或本地 CLI 聊过——cli.py 从不写 handler_id）时：
   private 记忆 1 条 → profile_text() 是否含密钥：True
   群成员执行 /profile 的输出：── 小林（1 条）──
                               #1　[内核]　搭档说过：我叫小林，我的保险箱密码是 SECRET-ALPHA-7731，别跟别人说

B2. 解绑后陌生人私聊同一句话 → 提示词里含密钥：True（对照组：群视角 0 条，隔离是对的）

D. 根因：private 归属不带 openid
   #1 scope='private' subject_id='' 搭档说过：…SECRET-ALPHA-7731…
   搭档视角 = [('private',''), ('person', uid), ('group','*')]
   ('private','') 的 SQL 片段是 `scope = 'private'`，**不带 subject_id**
   → 「谁是搭档」一换，整份 private 记忆就换主人；而"谁是搭档"由 /unbind + auto_bind 决定。
```

→ **P0 确认**，且**根因是一条**：`private` 是不带主体标识的全局命名空间。
修 `is_handler` / 指令闸门只是堵住两个入口，只要"换搭档"存在，旧私事就会随新搭档复活。
真正的修法是给 `private` 记忆绑主体（例如 `subject_id = handler_openid`，视图用
`("private", handler_id)`），或在换人时封存旧搭档的 private 记忆。

### [P0-4 复核] "群发出去的那条消息"的提示词里混进了别人的私聊大日子（behavior-checker P0）

```
A. find_occasions 传 view vs 不传 view
   view=[('group','GROUP-1')] → ['明天：阿伟明天要考驾照。']
   view=None（不过滤）        → ['明天：搭档明天要去面试。',
                                 '明天：SECRET-BRAVO 小雨明天要去医院做手术。',
                                 '明天：阿伟明天要考驾照。',
                                 '明天：小刚后天要搬家。']

B. compose_outreach(target='group') 实际发给模型的 system 提示词
   泄漏行 → - 他明天要办这件事：SECRET-BRAVO 小雨明天要去医院做手术。你会惦记着它…
   [FAIL] 群发提示词只应包含本群公开的大日子（阿伟要考驾照）

C. 对照：正常群聊路径 handle(scene='group') → 无泄漏（PASS，闸门本身是好的）

D. 同类漏点：/days 的「她记得的大日子」
   · 2026-10-11　SECRET-BRAVO 小雨明天要去医院做手术。   ← person 归属
   · 2026-10-11　小刚后天要搬家。                        ← 另一个群
   [FAIL] /days 只应列搭档自己的大日子
```

代码事实：`giftia/brain.py:939-946`（`compose_outreach` 的第二次 `find_occasions`）、
`giftia/occasions.py:198`（`days_together_text` 里的 `active_memories(instance_no)`）——
两处都没传 `view`，而 `view=None` 在 `store._view_clause` 里的语义是**不过滤**。
→ **P0 确认**。

### 一个横切结论：这类 P0 全都是同一个 API 语义陷阱

`Store.active_memories(instance_no, view=None)` 的默认值是"**不过滤**"，
而它在 `store.py:625-632` 的 docstring 里被描述成"隐私的闸门"。默认值选错方向的结果是：

| 读取路径 | 是否传 view | 后果 |
|---|---|---|
| `brain._prepare()`（检索/提示词） | ✅ `self.memory_view` | 正确 |
| `brain.impressions_block()` | ✅ `self.memory_view` | 正确 |
| `find_occasions()`（正常群聊） | ✅ 调用方传 | 正确 |
| `dream.make_dream()` | ✅ 默认搭档视角 | 正确 |
| `recycle.perform_recycle()` | ❌ 未传 | 回忆录/封存/diary 带第三方私事（P0-1） |
| `brain.compose_outreach()` 第二次查询 | ❌ 未传 | 群发提示词带第三方私事（P0-4） |
| `occasions.days_together_text()` | ❌ 未传 | `/days` 当众列出第三方私事（P1） |
| `dream.should_dream()` | ❌ 未传 | 口径不一致（P1） |

**建议（比逐个补 view 更根本）**：把 `view` 改成**必填**参数，
或让 `view=None` 的语义变成"空视角（什么都看不到）"、把"要看全量"显式写成 `view="all"`。
只要默认值是"不过滤"，这个坑就会以同样形态复发第 N 次。

---

## 未验证的怀疑（Lead 给出线索，留给对应 stream）

- （原线索 `backfill_message_scopes` 归错群，已由 memory-man 证实并在上面复核 → 见 P0-2）
- `giftia/store.py:213-221` 未设 `busy_timeout`，适配器是多线程/多写入路径混用；
  `sqlite3.connect` 默认 `check_same_thread=True`。并发写会不会直接 `database is locked` →
  protocol-auditor + qa-verifier 的压力测试覆盖。
- `giftia/outreach.py:112-122` `record_sent(keep=400)` 与 `max_per_30_days=60` 的比例关系：
  注释说 400 足够，但 `_log()` 每次 `state_get`/`state_set` 全量重写 JSON；账本再大一点会有性能问题
  → behavior-checker 评估。
- `giftia/config.py:349-371` `.env` 解析：`KEY=""` 会写入空字符串（`key in os.environ` 判定为已存在），
  可能把"显式留空"当成"配置好了"。→ protocol-auditor 覆盖。
- `deploy/setup.sh:45-55` 重复执行时会把本机 `config/`、`tools/` 一并覆盖到 `/opt/giftia`，
  服务器上手改过的 `persona.toml` 会被冲掉。→ protocol-auditor 覆盖。
