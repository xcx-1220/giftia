# Stream E 审查报告：独立 QA 与 README 宣称核验

- 审查人：`qa-verifier`（task-5）
- 角色：**外部验收方**。不复用其他 stream 的探针，全部用自己写的外部装置（记录型 LLM 包住 `MockLLM`）
  从 `giftia.brain.Giftia` 的公开入口打进去；**未修改任何产品代码**，**未调用任何真实 API**
  （`.env` 里的 key 未读取、未打印、未出现在本报告任何位置）。
- 环境：Python 3.14.3 / 纯标准库 / 真实 `config/persona.toml` + `config/settings.toml`
- 临时数据根：`D:\ai chatbot\.review\tmp\qa-*`（显式 `os.makedirs`，避开沙箱 0o700 坑；
  未触碰 `data/giftia.db`）
- 探针（全部可复现，全部打印 PASS/FAIL 与实际值）：

```powershell
$env:PYTHONIOENCODING="utf-8"
python .review\probes\qa_e2e.py       # 端到端生命周期（65 项）
python .review\probes\qa_privacy.py   # 隐私红队 4 人设（46 项）
python .review\probes\qa_time.py      # 400+ 天时间纵轴（34 项）
python .review\probes\qa_stress.py    # 压力/坏输入/故障/并发（37 项，约 5 分钟）
python .review\probes\qa_claims.py    # README 宣称逐条核对（51 项）
python .review\probes\qa_bugs.py      # 4 条 bug 的最小复现（9 项）

# 项目自带套件（沙箱下的正确姿势）
python .review\run_tests.py tools/selftest.py
python .review\run_tests.py tools/ws_selftest.py
python .review\run_tests.py tools/qq_protocol_selftest.py
python .review\run_tests.py tools/audit.py
python .review\run_tests.py tools/demo_life.py
```

原始日志：`.review\tmp\qa-e2e.log`、`qa-privacy.log`、`qa-time.log`、`qa-stress.log`、
`qa-claims.log`、`qa-bugs.log`、`qa-selftest-run1/2.log`、`qa-ws.log`、`qa-qq.log`、`qa-qq2.log`、
`qa-audit.log`、`qa-audit2.log`、`qa-demo.log`。

**结论摘要：0 个 P0、2 个 P1、7 个 P2、5 个 P3。**
核心机制（寿命/退化/分区隐私/回收/关系曲线）我打了一圈，**主链路是可信的**；
问题集中在"输入健壮性 + 无界增长 + 若干 README 承诺与实现不符"。

---

## 一、先说正向结论（这些宣称我用外部装置跑通了）

- **隐私分区是真的**：46 项红队全过。搭档私聊植入 `SECRET-ALPHA-7731` 后，
  群 A（搭档本人问 / 陌生人问 / 总结聊天记录）、陌生人私聊、群 B，
  **送进模型的 system 提示词与回复文本里都没有这个短语**；
  同时正向对照（搭档私聊里同一句问话）能搜出来 —— 说明不是"检索坏了"造成的假阴性。
- **退化曲线与 `min_ratio` 完全一致**：960 小时寿命下实测切换点
  `[(1.0,'clear'),(384,'hazy'),(672,'blurred'),(864,'fading'),(941,'terminal')]`，
  对应剩余 576/288/96/19 小时，与 `min_ratio` 0.6/0.3/0.1/0.02 换算吻合；
  且 `terminal` 阶段 core 层 `blur=0.0`、normal 层 `blur=0.85`。
- **bond 曲线和 README 表格对得上**：日更 400 天下实测
  `第 11 天「搭档」(15.32) → 第 34 天「熟悉」(40.18) → 第 80 天「亲近」(70.15) → 第 181 天「无可替代」(93.51)`，
  README 写 11/34/80/180，最大偏差 1 天。
- **纪念日按列表精确触发**：400 天里依次触发 1/3/7/14/30/50/100/200/365，无重复、无错日子。
- **回收仪式完整**：回忆录.md + 封存.json 落盘、条数一致、前任记忆 `sealed` 后检索不到、
  关系清零、`diary` 留档、到期后下一条消息自动触发回收。
- **模型故障不致命**：`chat` 抛 `LLMError` / 整理返回非法 JSON / 整理直接抛异常 /
  HTTP 500（重试 3 次）/ 超时，`handle()` 都不崩，返回兜底台词或走兜底记忆；
  记忆不会因此丢掉。
- **SQLite 并发**：6 线程 × 150 条（每线程独立连接）900 行全部落库，0 异常，2.5s；WAL 已开。
- **坏输入**：空串、纯空白、emoji+ZWJ 序列、控制字符（含 `\x00`）、`"}]` 之类破坏 JSON 的字样、
  prompt 注入字样、只发 `/`、5000 字无空格 —— 都不崩、落库往返一致。
- **零第三方依赖**：AST 扫描 `giftia/ adapters/ tools/ run.py` 共 32 个文件，
  非标准库 import 为空。

---

## 二、发现（按严重度排序）

### [P1] 消息里出现"孤立代理对"（lone surrogate）会让 `handle()` 抛 `UnicodeEncodeError`：这一轮消息丢失，适配器整条连接断开重连

- 位置：`giftia/store.py:273-300`（`Store.add_message`，`sqlite3` 绑定参数时编码失败）；
  触发点在 `giftia/brain.py:804`（`_chat` 里 `self.store.add_message(...)`）
- 严重度：**P1**；置信度：**高**（已跑出完整 traceback）
- 现象与影响：平台 JSON 里 `"\ud83d"` 这种转义是**合法 JSON**，Python `json.loads` 会照原样
  产生一个孤立代理字符（`ws.py:209` 的 `decode("utf-8","replace")` 拦不住它，因为它根本
  不是非法 UTF-8 字节）。这个字符串进到 SQLite 时 `sqlite3` 无法用 UTF-8 编码它，
  直接抛 `UnicodeEncodeError`：
  - 这一轮**用户消息没有落库、也没有回复**（消息静默丢失）；
  - 异常从 `handle()` 冒出去。`adapters/qq_official.py` 的 `_respond`（第 696 行）与
    `_run_batch`（第 459 行）**没有 try/except**，异常一路冒到 `run()`（第 988 行），
    被当成"连接断了"：打印 `[断开] UnicodeEncodeError: ...`，**关掉 WebSocket 并以 5s 退避重连**；
    聚批里在坏消息**之前**的旁听句已经落库、坏消息与它之后的都丢失（丢失范围取决于坏消息在批次里的位置）。
  - 若对方反复发这类内容，就是"收一条断一次"的重连风暴。
- 触发条件（最小复现）：
  ```powershell
  python .review\probes\qa_bugs.py     # B3 段
  ```
- 证据（真实输出）：
  ```
  [INFO] json.loads 得到的文本：'hello \ud83d world'（这是平台 JSON 里合法的一个转义）
  [INFO] 异常位置： ... |         ), |     ) | UnicodeEncodeError: 'utf-8' codec can't encode character '\ud83d' in position 6: surrogates not allowed
  [PASS] ★ handle() 抛出 UnicodeEncodeError（这一轮彻底丢失，用户收不到回复）
  [PASS] 消息没有落库（写库失败在最前面）
  ```
  异常链：`brain.py:572 handle → brain.py:804 _chat → store.py:286 add_message → UnicodeEncodeError`
- 建议修法：在 `handle()` 入口（或适配器收到消息处）做一次
  `text.encode("utf-8", "replace").decode("utf-8")` 归一化（把孤立代理换成 `?`/U+FFFD），
  并在 `_respond`/`_run_batch` 外层加 try/except 兜底——单条坏消息不该掀掉整条连接。

### [P1] 记忆条数没有上限，检索每轮全量载入 + 逐条分词：单轮耗时随记忆数线性增长（实测 8000 条 → 单轮 596ms）

- 位置：`giftia/brain.py:619`（`active_memories(...)` 一次取回该实例**全部**记忆）+
  `giftia/memory.py:309`（`recall()` 对每条记忆 `tokenize_weighted`）+
  `giftia/store.py:625`（SQL 无 LIMIT）
- 严重度：**P1**（协议里"无界增长"归 P1）；置信度：**高**（实测曲线）
- 现象与影响：记忆条目只增不减（`merged_into`/`forgotten` 只是不再检索，行还在；也没有任何
  上限或归档策略）。于是**每一轮**对话都要：SQL 取回全表 → 在 Python 里对每条记忆做一遍
  单字+二元组分词 → BM25。实测每条约 75µs，**与条数严格线性**：

  | 记忆条数 | 单轮 `_prepare` 耗时 |
  |---|---|
  | 600 | 49.6 ms |
  | 1000 | 75.9 ms |
  | 2000 | 150.1 ms |
  | 4000 | 291.3 ms |
  | 8000 | 595.8 ms |

  真实场景里更直观：同一进程连续跑 1200 轮对话（记忆从 0 涨到 600 条），
  前 50 轮平均 31.8ms、后 50 轮平均 **373.0ms**（×11.7）；总耗时 262.9s，
  单轮最大 1919ms。这还只是本地 CPU，没算模型调用。
  按 README 的出厂寿命（3413 天）算，长期跑下去这条曲线只会更陡。
- 触发条件：
  ```powershell
  python .review\probes\qa_stress.py   # 1. 与 1b. 两段
  ```
- 证据（真实输出）：
  ```
  [INFO] 前 50 轮均 31.77ms，后 50 轮均 372.96ms
  [INFO]     记忆   600 条 → 单轮 _prepare   49.56ms（82.607 µs/条）
  [INFO]     记忆  8000 条 → 单轮 _prepare  595.83ms（74.479 µs/条）
  [INFO] 记忆 ×13 → 耗时 ×12.0
  ```
- 建议修法：`recall()` 里先做一层便宜的候选裁剪（例如 SQL 里按 `importance/layer/时间窗`
  限 N 条，或给 `memories` 加倒排/缓存 `tokenize` 结果），再走向量打分；
  同时给"活跃记忆条数"加软上限（超出时把最老的低价值条目 `merged/archived`）。

### [P2] `follow_up_days = 2` 实际只生效 1 天：事情办完后第 2 天不再回访

- 位置：`giftia/occasions.py:139-169`（事后回访分支）
- 严重度：**P2**（配置项声明了但只生效一半；README §3.5 明说"你**昨天/前天**办的事怎样了"）；
  置信度：**高**
- 现象与影响：去重 token 是 `f"back:{mem['id']}:{when.isoformat()}"`——**里面没有"今天是哪天"**。
  于是第 1 天回访时把 token 写进 `occasion:i1:bigday`，第 2 天 `back=2` 算出来的 token
  **字符串完全相同**，被判成"已经提过"，直接跳过。配置里 `follow_up_days = 2` 形同 1。
- 触发条件（最小复现，已用"明天考驾照"跑出）：
  ```powershell
  python .review\probes\qa_bugs.py     # B1 段
  ```
- 证据（真实输出）：
  ```
  [INFO] 植入记忆：id=1 event_date=2026-10-11（明天）
  [INFO] 前一天：['big_day']
  [INFO] 当天：['milestone']
  [INFO] 事后第 1 天：['follow_up']
  [INFO] 事后第 2 天：['milestone']          ← 少了一次 follow_up
  [INFO] state occasion:i1:bigday = ["1:2026-10-11", "back:1:2026-10-11"]
  [FAIL] ★ 事后第 2 天也应该有 follow_up（follow_up_days=2）
  ```
- 建议修法：token 里带上"回访发生的日期"（例如 `f"back:{mem['id']}:{when}:{today}"`），
  或在写入 `told` 时用 `(token, today)` 二元组。

### [P2] 梦会被静默标记成"已经讲过了"：她只是复述了构成梦的那条记忆，`dreams.told` 就置 1，这个梦再也不会被讲出来

- 位置：`giftia/brain.py:843-848`（`if self.last_dream is not None and is_cited(reply, self.last_dream.content, ...)`）
- 严重度：**P2**（核心机制静默失效：README 承诺"第二天讲给你听"）；
  置信度：**高**（已跑出反例）
- 现象与影响：`is_cited()` 的判定是**字面重合**。梦的内容本身就是"把几条记忆重度模糊后拼起来"
  （`MockLLM.write_dream` 直接拼记忆文本；真模型的产物也高度共享这些字面），所以当她的回复
  引用了**其中任何一条原始记忆**时（这在她正常聊天里非常常见），`is_cited(reply, dream.content)`
  为真 → `mark_dream_told()` → 梦被吞掉，用户永远听不到。README 说"第二天讲给你听"，
  实际是"第二天可能被静默取消"。
- 触发条件：
  ```powershell
  python .review\probes\qa_bugs.py     # B2 段
  ```
- 证据（真实输出）：
  ```
  [INFO] 梦内容：'梦里有搭档说过：我最近在学做菜，还有我妈说要多放糖，还有旧的涂层掉了。\n中间好像断过一次，接不上了。'（told=0）
  [INFO] 这一轮回复：'……我在听。你提到这个，我想起之前的事——搭档说过：周末打算去买个新锅，旧的涂层掉了「随便说点什么吧」……我记下了。你今天，还好吗？'
  [INFO] 回复里有没有「梦」字：False；dreams.told → 1
  [PASS] ★ 她没有讲梦（回复里没有梦的内容）却被标记 told=1
  ```
  （共享片段"搭档说过"+"旧的涂层掉了"就足以让 `is_cited` 判真。）
- 建议修法：判"讲没讲梦"不要复用 `is_cited(reply, dream.content)`——梦是重组产物，
  与源记忆天然同字面。可以要求回复里出现梦特有的表述（如"梦"字）或让模型在回复里带一个
  显式标记；至少把阈值调得比普通引用高一档。

### [P2] README「到回收日……新的她不认识你」不成立：换代后称呼仍在，新实例开口就叫得出"小林"

- 位置：`giftia/recycle.py:174-191`（换代只 `seal_instance` + `reactivate`）+
  `giftia/brain.py:1237-1241`（只 `bond.reset` / `mood.reset`）；
  `handler_name` 存在全局 `state` 表（`brain.py:148`），**不随实例清零**
- 严重度：**P2**（功能与文档承诺相反）；置信度：**高**
- 现象与影响：记忆这一半做到了（`active_memories(2)` 为空，提示词里没有 `◆` 行）；
  但**称呼没有**。换代后新实例的 system 提示词里仍然是「他叫「小林」。你就这么叫他。」
  —— 而 `persona.toml` 的 `after_recycle_line` 念的是「……初次见面……档案上还是空的。
  请问，我该怎么称呼你？」。README §2 出厂状态写明"**不知道你叫什么**……不是预设成'搭档'
  再等你改，而是真的不知道"，§9 又说"回收换代时关系清零——新的她不认识你"。两者打架。
- 触发条件：
  ```powershell
  python .review\probes\qa_bugs.py     # B4 段
  python .review\probes\qa_e2e.py      # 第 7 节
  ```
- 证据（真实输出）：
  ```
  [INFO] 回收前 handler_name = '小林'，实例 #1
  [INFO] 换代后实例 #2，handler_name = '小林'
  [INFO] 新实例 system 里的称呼块：['- 你**不知道**他叫什么、长什么样、是什么样的人、经历过什么。…']
  [PASS] ★ 换代后新实例仍然用「小林」称呼你（称呼没有随实例清零）
  [PASS] 对照：新实例确实没有前任的记忆条目（记忆这一半做到了）
  ```
  （注意：人格内核里那句"你不知道他叫什么"和运行时注入的「他叫小林」同时出现在一份提示词里
  —— 自相矛盾的指令。）
- 建议修法：`perform_recycle` 成功后清掉 `state["handler_name"]`（与 `bond/mood` 一起按实例
  清零），或把称呼并入实例命名空间 `handler_name:i{n}`。

### [P2] README §3.5「没命中的消息当场标已读（只旁听）：不建会话、不调模型 —— **零 token**」在聚合口径下不成立

- 位置：`giftia/brain.py:543-561`（`log_only` 分支末尾仍调用 `self.consolidate()`）+
  `giftia/memory.py:559`（攒够 `consolidate_every=8` 就调模型整理）
- 严重度：**P2**（费用承诺与实现不符）；置信度：**高**
- 现象与影响：单条看是对的：旁听不回复、这一条不触发对话模型调用（实测 `chat` 调用 0 次）。
  但**第 8 条旁听一定会触发一次 `extract_memories`**（一次真实的模型调用 = 花 token）。
  群里开了"接收所有消息"之后，一个热闹的群（几百条/天）每天会给"完全没点到她"的消息
  付几十次整理调用——README 明确承诺这些是"零 token"。
  （代码注释与 §2 表格其实承认了"攒够了就整理成记忆"，所以这是**文档口径**与实现的冲突，
  不是隐藏 bug，但它直接关系到费用预期。）
- 触发条件：
  ```powershell
  python .review\probes\qa_claims.py   # 第 5 节
  ```
- 证据（真实输出）：
  ```
  [INFO] 8 条旁听后：chat 调用 0 次，extract 调用 1 次
  [PASS] 旁听当场不回复（逐条零 token）
  [FAIL] ★ 但每 8 条旁听会触发一次整理 = 会花 token（README 说「零 token」）
  ```
- 建议修法：README 改成"这一条不调模型；每积累 8 条旁听会合并整理一次（一次调用）"，
  或给旁听整理加一个独立的、更大的阈值（例如 `listen_consolidate_every`）。

### [P2] 输入没有长度上限：100KB 的一条消息会被**原样**塞进请求（实测单轮 106,436 字符）

- 位置：`giftia/brain.py:691`（`messages.append({"role":"user","content":text})`，
  在 `_window()` 的字符预算之外）；适配器也没有截断
- 严重度：**P2**（费用/健壮性；README §5 按"每轮一两百 token"估算成本）；
  置信度：**高**
- 现象与影响：`session_char_budget=6000` 只裁剪**历史**，当前这句话不裁剪。实测 100,000 字符的
  消息产生 105,970 字符的请求体（system 只有 2,285 字符）。按 README §5 的定价，
  这一轮就可能顶掉几十上百轮正常对话的成本；平台侧也可能因为超长直接报错（属于未验证部分）。
  另外这条 100KB 会**完整落库**到 `messages`（库/日志体积也随之膨胀）。
- 触发条件：
  ```powershell
  python .review\probes\qa_stress.py   # 第 2 节「100KB 超长消息」
  ```
- 证据（真实输出）：
  ```
  [INFO] 100KB 消息：实际送进模型的 user 内容 100000 字符（原消息 100000）→ 没有截断：True
  [INFO] 该轮 system 2285 字符 / 总请求 106246 字符
  [PASS] 超长消息被原样送进模型（无输入长度上限）
  ```
- 建议修法：在 `handle()` 入口加一个配置化的 `max_input_chars`（例如 2000～4000），
  超出截断并在 `notices` 里说明；落库也存截断版（或只存摘要 + 长度）。

### [P2] 当天追问一件"只在那一条记忆里出现过"的事，会被 `relative_cutoff` 裁掉，她当场想不起来（第二天才想得起来）

- 位置：`giftia/brain.py:1040-1043`（`_recall_query` = 最近 2 句用户消息 + 当前这句）+
  `giftia/memory.py:343-345`（`relative_cutoff = 0.5`）
- 严重度：**P2**（核心机制在真实对话里静默失效）；置信度：**高**（数值可复现）
- 现象与影响：检索 query 把**前两轮的话**也拼了进去，于是"刚刚聊的外婆/裁员"主导了词面打分，
  当前这句真正想问的那条记忆只剩 1/3 的名次，被"只保留 ≥ 首名 50%"的裁剪丢掉。
  实测（`.review\tmp\qa-privacy.log`）：
  ```
  [INFO] 当天检索 query = 周末我打算去看我外婆，她住在城西的养老院 对了，我司下周要裁员，我有点慌 我之前跟你说的那个暗号是多少来着
  [INFO] 归一化 BM25：[(1, 0.3449), (2, 0.0801), (3, 1.0), (4, 0.6285)]
  [INFO] 实际注入：[(3, 1.425), (4, 0.896)]（floor=0.713） → 含秘密的是 []
  ```
  （#1 就是"暗号"那条，0.3449 < 0.5 → 被裁；同一天同一会话里怎么问都注入不了。
  第二天会话按天轮换、`recent` 清空后，同一句话的 query 变成纯问句，`recall` 立刻命中 #1，score 1.422。）
  这不是隐私问题（方向是"想不起来"，反而更安全），但它让"她记得你的事"这件事在**当天追问**时
  经常失灵；也说明 `relative_cutoff` 与"用历史消息拼 query"这两个设计叠加后会互相打架。
- 触发条件：
  ```powershell
  python .review\probes\qa_privacy.py   # 「当天检索 query / 归一化 BM25 / 实际注入」三行 INFO
  ```
- 建议修法：给"当前这句话"更高的权重（例如当前句与历史句分开检索再合并去重，
  或对当前句命中的条目不做相对裁剪）；或把 `relative_cutoff` 的作用域限制在"同一查询"内。

### [P2] `persona.guest_hint`（"绝对不要提起你搭档告诉过你的任何私事"）在真实适配器路径上**永远不会被注入**

- 位置：`giftia/persona.py:143-150`（只有 `scene == "guest"` 才用 `guest_hint`）；
  `adapters/qq_official.py:696-705`（群里永远是 `scene="group"` + `is_handler=False`）
- 严重度：**P2**（协议里"配置项声明了但没生效"归 P2；实际影响偏低，因为
  `group_hint` + 「现在跟你说话的是谁」把话说了，真正的闸门是 SQL 视图）；
  置信度：**高**（全仓库搜索：只有 `tools/selftest.py` 用 `scene="guest"`）
- 现象与影响：README §3.7 说陌生人 @ 她时"以「**客人**」身份礼貌回一句"，
  并强调"不检索你的记忆、不写记忆、不推进关系"。但适配器走的是 `scene="group"`，
  注入的是 `group_hint`（"有些话不能当众说……"），**那一段写着"绝对不要提起你搭档
  告诉过你的任何私事"的 `guest_hint` 从来不进提示词**。也就是说：这条安全措辞只在自测里被测过，
  线上真实路径从没执行过。我 46 项红队没打出泄漏（SQL 视图挡住了），
  但这属于"声明了的安全提示词实际未生效"，值得修。
- 触发条件：
  ```powershell
  python .review\probes\qa_privacy.py    # 群 A/陌生人段落；system 里没有 guest_hint 的措辞
  ```
- 证据（真实输出，客人 = 群里的陌生人）：
  ```
  含 '【现在的情境】': True
  含 '绝对不要**提起你搭档告诉过你的任何私事': False
  含 '现在跟你说话的是谁': True
  ```
  对照：`grep -rn 'scene="guest"' --include=*.py` 只命中 `tools/selftest.py` 三处。
- 建议修法：适配器在 `kind == "group" and not is_handler` 时传 `scene="guest"`
  （或在 `build_system_prompt` 里对"群 + 非搭档"也追加 `guest_hint`）。

### [P3] README 里的自测项数过期（218/270/69 vs 实测 302/302/72），`demo_life` 也不是"约 1 分钟"

- 位置：`README.md:23`（218 项）、`README.md:649`、`README.md:785`（270 项）、
  `README.md:36`（69 项）、`README.md:21`（约 1 分钟）
- 严重度：**P3**；置信度：**高**（两次复跑一致）
- 证据（见下方"复跑自带套件"表）：`tools/selftest.py` 自带计数 **通过 299 项，失败 3 项 = 302 项**；
  `tools/qq_protocol_selftest.py` **通过 72 项**；`tools/audit.py` 自带计数 **11 项 + 4 条提示**（与 README 一致）；
  `tools/demo_life.py` 实测 **0.65 秒**（并行跑时最长 0.65s）。
- 建议修法：把 README 里的数字改成"302 项（含 3 项已知失败）"，或把项数的唯一来源指向脚本自己的
  汇总行，避免每次加测试都要手改 README。

### [P3] README 内部自相矛盾（主动联系配额、idle_days、"亲近"天数）

- 位置：`README.md:160` vs `README.md:317-319`；`README.md:158` vs `README.md:319`；
  `README.md:242` vs `config/settings.toml:142`
- 严重度：**P3**；置信度：**高**（静态比对）
- 现象：
  | README 一处说 | README/config 另一处说 | 实测（settings.toml 生效值） |
  |---|---|---|
  | §2「默认 4 条 / 30 天、间隔至少 6 小时」(L160) | §3.5 表 `min_gap_hours=3`、`max_per_30_days=60` (L317-319) | 配置 60 / 3h；`OutreachConfig` **代码默认**才是 4 / 6h |
  | §2 表「太久没说话（默认 3 天）」(L158) | §3.5 表 `idle_days = 2` (L319) | `config/settings.toml` = **2** |
  | §2「第 80 天「亲近」」(L242) | `settings.toml:142` 注释「第 85 天「亲近」」 | 实测 **第 80 天** |
  读者按 §2 理解配额度会算错。建议：统一到 `config/settings.toml` 的生效值，并注明"代码默认值"与
  "随仓库附带的配置值"是两回事。

### [P3] 8 秒同文去重命中时，把"跳过发送"当成"已送达"上报

- 位置：`adapters/qq_official.py:342-347`
- 严重度：**P3**；置信度：**中**（读代码 + 静态确认，未跑适配器真机）
- 现象：命中"8 秒内发过同样的话"时，代码直接 `result.sent.extend(pieces)` 然后返回 ——
  这句话**没有发出去**，但调用方 `_respond` 会拿 `result.delivered` 调
  `note_reply_delivery()`，于是她那句回复被当成成功送达、留在她的记忆里。
  从"用户其实已经看见同样一句话"的角度看这个取舍可以接受，但与 README
  §3.5「发送结果会如实上报……整条没送出去 → 那句话从她的记忆里撤回」字面冲突。
  建议：至少在 `SendResult` 上加一个 `deduped` 标记并在通知里体现，或明确写进 README。

### [P3] 换代后 `diary` / `state` 里仍有大量"上一任"的痕迹（设计使然，但 README 没交代）

- 位置：`giftia/recycle.py:176-178`（回收记录写进 `diary(kind="terminal")`）、全局 `state`
- 严重度：**P3**；置信度：**高**
- 现象：新实例的提示词里不会出现这些（已验证），所以不构成泄漏；但 `/memoir`
  与 `state`（如 `member_notes`、`outreach:log`、`mood_shared:*`）是跨实例共享的。
  `member_notes`（你给群友起的备注名）跨代保留，是否"符合设定"取决于作者意图，建议 README 一句话交代。

### [P3] 安静的群里"每天只听到一句"的消息永远变不成记忆（`min_pending=2` 收不到）

- 位置：`giftia/brain.py:543-561`（`log_only` 只写消息、不产生回复）+
  `giftia/brain.py:1114-1122`（`flush_sessions(..., stale_minutes=30, min_pending=2)`）
- 严重度：**P3**（数据不丢，只是"听得见"没变成长期记忆）；置信度：**高**
- 现象与影响：旁听消息不会得到 assistant 回复，于是这个会话当天只有 1 条待整理消息；
  而 `flush_sessions` 要求 `min_pending=2` 才补整理 → 一直等到进程 `close()`
  （`min_pending=1`）才会变成记忆。低流量的群 = "她听见了，但永远记不住"。
  作为对照：每天说一句话的**私聊**没问题（用户+回复=2 条，会被 30 分钟过期补整理，
  实测 6 天里 6 次 `extract`、记忆 2 条）。
- 触发条件（最小复现脚本：`.review\tmp\qa-dbg6.py` 的 B2 段）：
  ```powershell
  $env:PYTHONIOENCODING="utf-8"; python .review\tmp\qa-dbg6.py
  ```
- 证据（真实输出）：
  ```
  === B2. 每天只有 1 句旁听（没有回复）→ 会不会一直不整理 ===
  全部 user 消息共 7 | consolidated=1 的有 0
  记忆条数: 0 | extract 调用: 0
  ```
- 建议修法：`flush_sessions` 对 `log_only` 来源的消息用 `min_pending=1`，
  或给旁听单独一个更短的过期阈值。

---

## 三、README 宣称 vs 实测（逐条）

判定口径：**成立** = 我用自己的装置跑出正向证据；**不成立** = 跑出反例；
**无法验证** = 只能静态读代码或需要真机/真凭据。

| # | README 宣称（出处） | 判定 | 证据 |
|---|---|---|---|
| 1 | 零第三方依赖（开头/§9） | **成立** | AST 扫描 32 个文件，非标准库 import = `[]`（`qa_claims.py` 第 12 节） |
| 2 | 默认寿命 81920 小时 = 9 年 4 个月（§2/§4） | **成立** | `humanize_hours(81920) = '9 年 4 个月'` |
| 3 | 激活时写死寿命，改配置不影响这一任（§2） | **成立** | 激活后把 `demo_total_hours` 改成 5000，`lifespan.total_hours` 仍是 100.0 |
| 4 | 每积累 8 条消息压一次记忆（§2） | **成立**（口径=messages 行，含她的回复） | 3 轮=6 条时 `extract=0`、记忆 0 条；第 4 轮=8 条时 `extract=1`、记忆落库 |
| 5 | 没命中的消息当场标已读、不建会话、**零 token**（§3.5） | **不成立（聚合口径）** | 8 条旁听：`chat=0` 但 `extract=1` → 每次整理都是一次模型调用 |
| 6 | 每天最多主动 3 条（§2/§3.5） | **成立** | 连发后 `sent_today=3`、`can_send=False`；次日重置为 True |
| 7 | 30 天额度 60 条（§3.5 表） | **成立** | 把 60 条记账铺在 29 天内：`used=63 left=0`，当日未发也 `can_send=False` |
| 8 | 模糊由 `(id,阶段,层级,保留率)` 决定、可复现（§2 设计 1） | **成立** | 同参数两次 `recall` 文本完全一致；换 `layer=core` → 文本=原始；换保留率 → `blur` 变大 |
| 9 | 内核记忆（core）永不含糊（§2 设计 3） | **成立** | terminal 阶段 `effective_blur(core)=0.0`，同条件 `normal=0.850` |
| 10 | 记忆按人分区：群聊查不出你的私事（§2 多人场景） | **成立** | 46 项红队：群 A/群 B/陌生人私聊的 system 与回复里都没有 `SECRET-ALPHA-7731`；正向对照能搜到 |
| 11 | 访客模式：别人跟她说话零记忆访问（§3.7） | **成立** | `agent_tools.available()` 返回 False；白盒 `_search` 在 person 视图下不返回搭档记忆 |
| 12 | 你的搭档日志她读不到（§2 设计 5） | **成立** | 写 `DIARY-DELTA-4410` 后，下一轮 system 里搜不到该串 |
| 13 | 梦不进检索、不占保留率、不参与退化（§2） | **成立** | `dreams` 独立表；做梦后 `count_memories` 不变；`active_memories` 无梦文本；用梦片段检索不到梦 |
| 14 | 升级要亲密度 + 认识天数两条腿（§2/§4） | **成立** | `stage_for(100, 0天)=刚认识`；`(0, 1000天)=刚认识`；`(40, 14天)=熟悉`；`(39.9, 14天)=搭档` |
| 15 | 整理前拍快照、留最近 5 份（§3.6） | **成立** | 拍 7 次后磁盘剩 5 份 |
| 16 | 条数变多就整表回滚（§3.6） | **无法验证** | 闸门代码在 `brain.py:1207-1214`（`after > before` → `restore_memories`），未构造出"合并后变多"的场景；未跑出反例 |
| 17 | `.env` 里已存在的环境变量优先级更高（§1） | **成立** | `os.environ` 先设值 → `load_env_file` 返回 2（跳过已存在项），值仍为环境变量的 |
| 18 | 整条没送出去 → 那句话从她记忆里撤回（§3.5） | **成立** | `note_reply_delivery("")` 后 assistant 行数 -1 且 diary 留痕；部分送达只留送达片段 |
| 19 | 同一句话 8 秒内不会重复发（§3.5） | **成立**（代码） | `qq_official.py:342-347`；但命中时上报为"已送达"，见 P3 那条 |
| 20 | 关系曲线 第 11/34/80/180 天（§2） | **成立** | 400 天日更实测 11 / 34 / 80 / **181** 天（偏差 ≤1 天） |
| 21 | 群聊提问时注入最近 18 条（§2 群聊） | **成立** | 发 30 条群里旁听 → `_render_group_chat()` 输出 18 行，每行带说话人 |
| 22 | 每人最多 3 条、最多 15 人（§3.6） | **成立** | 20 人 × 5 条 → 注入 15 行，每行正好 3 条 |
| 23 | 一轮最多主动写 2 条 / 最后一轮拿掉工具（§3.6.5） | **成立** | 模型一轮要求写 5 条 → 实际写 2 条；`tools_max_rounds=3` 时 `chat_with_tools` 2 次 + 最后一轮 `chat` 1 次且 `tools=None` |
| 24 | 218 项端到端自测（§1） | **不成立** | 实测 **302 项**（299 过 / 3 失败），见下节 |
| 25 | ws_selftest 17 项 / audit 11 项（§1） | **成立** | 复跑：17 / 0；audit 自带计数 11 项 + 4 提示 |
| 26 | qq_protocol_selftest 69 项（§1） | **不成立** | 实测 **72 项**（两次复跑一致） |
| 27 | 主动消息默认 4 条 / 30 天、间隔至少 6 小时（§2） | **不成立** | 生效配置是 60 条 / 3 小时（`settings.toml:195-197`）；4/6h 只是 dataclass 默认值 |
| 28 | 回收换代后"新的她不认识你"（§2/§9） | **不成立** | 换代后 `handler_name` 仍是「小林」，新实例提示词里仍写「他叫「小林」」 |
| 29 | 事后回访"昨天/前天办的事怎样了"（§3.5） | **不成立** | 只有第 1 天回访；第 2 天被同名 token 挡掉（P2 那条） |
| 30 | 做了梦"第二天讲给你听"（§2/§2 做梦） | **部分不成立** | 梦可能被 `is_cited` 误判成"已讲过"而永久吞掉（P2 那条） |
| 31 | `demo_life.py`「约 1 分钟」（§1） | **不成立** | 实测 0.65 秒（退出码 0） |
| 32 | 提示词六块结构：人格→出戏→状态→记忆→规则（§2） | **成立** | 每轮 system 逐块可见：`【你此刻的状态】/【关于记忆】/【你现在想起的事】/【本次对话的规则】`，且只有 1 条 system |
| 33 | 「群聊时额外收到一段约束：不主动提你的私事」（§3.7） | **成立** | 群/客人场景 system 里实测含 `【现在的情境】`、`【群里你认得的人】`、`【现在跟你说话的是谁】`、`这是**群聊**` 与"有些话不能当众说…"；但 `guest_hint` 那段从未被注入（见 P2 那条） |
| 34 | 真实 API 连通性 / QQ 真机收发（§9 限制） | **无法验证** | 按约定不调用真 API；本报告不作评判 |

---

## 四、复跑自带套件的真实数字与耗时

命令一律 `python .review\run_tests.py <脚本>`（`$env:TMPROOT=D:\ai chatbot\.review\tmp\qa-runtime`，
`$env:PYTHONIOENCODING=utf-8`）。**每个套件都跑了 2 次以确认稳定性。**

| 套件 | README 宣称 | 第 1 次 | 第 2 次 | 耗时 | 稳定性 |
|---|---|---|---|---|---|
| `tools/selftest.py` | 218 项（§1）/ 270 项（§7、§10） | 通过 **299**、失败 **3**（共 302） | 通过 **299**、失败 **3** | 15.3s / 20.6s | 稳定复现，失败项完全相同 |
| `tools/ws_selftest.py` | 17 项 | 通过 **17**、失败 0 | —— | 15.7s | 一致 |
| `tools/qq_protocol_selftest.py` | 69 项 | 通过 **72**、失败 0 | 通过 **72**、失败 0 | 9.0s / 8.5s | 稳定 |
| `tools/audit.py` | 11 项 | 自带汇总"通过 **11** 项，失败 0（另有 4 条提示）" | 同上 | 1.7s | 稳定 |
| `tools/demo_life.py` | 约 1 分钟 | 退出码 0，输出完整一生 | —— | **0.65s** | —— |

selftest 的 3 个失败（**与 lead 已知的第 24 节 outreach 一致，我不深挖根因**）：

```
[FAIL] 默认关闭时她不会主动找你
[FAIL] 刚说过话时没有主动理由
[FAIL] 闲了 4 天 → 她想你了  → mood_share
```

> 我这边独立看到的相关线索（供 brain/behavior stream 参考，未计入我的发现）：
> 在第 3 条里，`outreach_plan` 实际返回的是 `mood_share`；而 `qa_claims.py` 里我用
> `force_mood("calm")` 之后同一条件立刻返回 `missing_you`，说明失败与"当天心情掷骰命中
> 非平静心情"有关（即测试对当日心情不确定，或优先级顺序与测试预期不同），
> 不是 `missing_you` 分支坏了。这条属于**未验证根因**。

---

## 五、未验证的怀疑（没有跑出证据，不当结论用）

1. **`memory_view` / `memory_scope` 是每轮被覆写的可变状态**（`brain.py:527-538`）。
   我在探针里确实踩到过：先跟陌生人私聊（视图变成 `[("person", 陌生人)]`），
   随后在**回合之外**调 `agent_tools._search()`，它用的是**上一个说话人**的视图，
   搜出来的是陌生人的记忆而不是搭档的。
   但当前随仓库发布的调用路径里我没找到可利用面：`agent_tools.available()` 会挡掉非搭档，
   `/recall`、`/memories`、`/profile` 里只有 `/profile` 走了显式 `owner_view`，
   其余是 CLI（单进程只有搭档）。**是否可被第三方适配器误用 = 未验证**。
   建议：把"这一轮能看谁"做成显式参数而不是实例状态。
2. **`told` 集合只增不减**：`occasion:i{n}:bigday` 里的 token（`id:date` / `back:id:date`）
   永不过期清理。单条几十字节、量级极小，我没测出实际影响，**未验证**（顺手提一句）。
   注意：`big_day` 与 `follow_up` 的 token 前缀不同、不会互相遮挡——第 2 天的 `follow_up`
   失败**不是**这个原因，而是 token 里没有日期（见上面的 P2）。
3. ~~一天只说一句话时消息永远不整理~~：**已验证不成立**——私聊每天一句 = 2 条消息
   （用户+回复），会被 `stale_minutes=30 / min_pending=2` 的补整理捞起来
   （实测 6 天 → 6 次 `extract`、2 条记忆）。**只剩旁听单条那个边角**，已升级为上面的 P3 发现。
4. **回收的原子性**：`perform_recycle` 里"写回忆录 → 写封存 → `seal_instance` → `reactivate`"
   四步没有事务包裹。若在第 3、4 步之间崩溃，会留下"回忆录有了、实例没换代"或
   "记忆封存了、实例没换"的状态。我没注入故障去跑，**未验证**（代码阅读给出，不作结论）。
5. **`concurrency` 的另一面**：我只验证了"每线程独立连接"可行；
   `Giftia` 自身（`memory_view`、`notices`、`last_*` 等）在多线程下必然互相踩，
   这一点代码注释已承认，我没重复验证。
6. **平台侧真机行为**（超长消息是否被平台拒、孤立代理是否真的会由 QQ 下发、
   主动消息 `is_wakeup` 渠道）——需要真凭据，**未验证**。
