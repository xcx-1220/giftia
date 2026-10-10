# Giftia 代码审查与测试报告

**审查对象**：`D:\ai chatbot`（Giftia —— 有寿命、会遗忘的陪伴型对话内核，纯标准库 Python + SQLite）
**审查日期**：2026-10-10
**环境**：Windows / Python 3.14.3（`C:\Users\86173\AppData\Local\Programs\Python\Python314`）
**代码版本**：工作区快照（`git status` 干净，HEAD = `53dfcb4 修时间感：她分不清"昨天"和"刚才"`）
**审查方式**：6 路并行（5 个独立审查流 + Lead 横切复核），**只读审查 + 独立探针，未修改任何产品代码**

---

## 一、结论摘要

| 项目 | 结果 |
|---|---|
| 总体评价 | **机制设计与文档质量明显高于平均水准，但当前状态不适合上线** |
| 阻断级问题 | **P0 × 5**（隐私越界 4、稳定性 1），Lead 已逐条独立复现 |
| 高优先级问题 | **P1 × 25**（§四 逐条列出） |
| 中/低优先级 | P2 × 24、P3 × 16（§五 按主题合并，明细见各流报告） |
| 自带测试 | `selftest` **302 项：299 通过 / 3 失败（退出码 1）**；`ws_selftest` 17/0；`qq_protocol_selftest` 72/0；`audit` 11/0 |
| README 宣称核验 | 34 条硬事实：**成立 24 / 不成立 7 / 部分 1 / 无法验证 2** |
| 产品代码改动 | **零**（`git status --porcelain` 仅 `.review/` 与我们之外的 `.idea/`） |
| 零第三方依赖 | **成立**（AST 扫描 32 个 .py 文件，非标准库 import 为空） |

**一句话**：这个项目的"寿命 / 退化 / 分区隐私 / 回收"四个核心机制**真的实现了、而且我打不穿**；
问题集中在**隐私闸门的默认值方向错了**（4 条 P0 全是同一个根因的不同出口）、
**异常路径的兜底缺一层**（数据库/编码/协议错误会一路冒穿到连接层），
以及**文档与配置/实现之间的漂移**（测试项数、额度语义、理由表、"新的她不认识你"等）。

### 建议的修复顺序（按"投入产出比"排序）

1. **改一个默认值就能堵掉 4 个 P0 中的 3 个**：把 `Store.active_memories()` 的 `view` 变成必填，
   或让 `view=None` 表示"空视角"、全量必须显式写 `view="all"`。
   （当前 `view=None` = 不过滤，`recycle` / `compose_outreach` / `days_together_text` / `should_dream` 四处忘了传 → P0-1/P0-3/P1-5/P1-6）
2. `private` 记忆绑定主体（`subject_id = handler_openid`），或换绑定时封存旧搭档的私事 → 根治 P0-2/P0-3。
3. `handle()` 的异常兜底从 `except LLMError` 扩到 `except Exception` + 适配器 `handle_event` 加 try
   → 一次性消掉 P0-4、P0-5、P1-5、P1-8 的放大效应（消息不再"没回复 + 断连"）。
4. 把 `selftest` 的 3 项修好并让 CI 变绿（现在它会随日期红/绿摇摆，等于没有回归信号）。
5. 文档批量对齐（测试项数、额度默认值、理由表、"新的她不认识你"、`demo_life` 耗时）。

---

## 二、审查方法（可复现）

### 2.1 分工

| 流 | 范围 | 报告 |
|---|---|---|
| A | `memory.py` / `lifespan.py` / `store.py` | [.review/findings/memory-man.md](.review/findings/memory-man.md) |
| B | `brain.py` / `persona.py` / `trigger.py` / `agent_tools.py` | [.review/findings/brain-checker.md](.review/findings/brain-checker.md) |
| C | `adapters/qq_official.py` / `ws.py` / `config.py` / `clock.py` / `deploy/*` | [.review/findings/protocol-auditor.md](.review/findings/protocol-auditor.md) |
| D | `mood` / `bond` / `occasions` / `dream` / `outreach` / `recycle` / `cli` / `run.py` | [.review/findings/behavior-checker.md](.review/findings/behavior-checker.md) |
| E | 独立 QA（端到端 / 隐私红队 / 时间纵轴 / 压力 / README 宣称核验） | [.review/findings/qa-verifier.md](.review/findings/qa-verifier.md) |
| 0 | Lead 横切复核（含对全部 P0 的**独立二次复现**） | [.review/findings/lead-stream0.md](.review/findings/lead-stream0.md) |

规则见 [.review/PROTOCOL.md](.review/PROTOCOL.md)：每条发现必须带位置、严重度、置信度、最小复现、**真实输出**；
没跑出证据的一律归入"未验证的怀疑"，不当结论。**每个 P0 都由 Lead 用与报告者不同的探针复现过。**

### 2.2 沙箱下的测试姿势（重要，否则你会以为是代码坏了）

本机 DSH 沙箱下直接 `python tools/selftest.py` 会失败：

```
PermissionError: [WinError 5] 拒绝访问。: '...\giftia-selftest-xxxx\data'
```

根因不是项目的问题：CPython 的 `tempfile.mkdtemp()` 用 **mode 0o700** 建目录，而沙箱不为新目录继承 ACE。
已实测：`os.mkdir(p, 0o700)` FAIL / `os.mkdir(p, 0o777)` PASS / `Path.mkdir()`（默认）PASS。
因此本次审查提供了两个垫片（**不改产品代码**）：

```powershell
python .review\run_tests.py tools/selftest.py     # 跑单个自带脚本（改 tempfile.tempdir + mkdtemp 用 0o777）
python .review\acceptance.py                      # 跑全部套件，抓真实项数/耗时，落盘 .review/evidence/
```

### 2.3 最终验收结果（`.review/evidence/summary.json`）

| 套件 | README 宣称 | 实测 | 退出码 | 耗时 |
|---|---|---|---|---|
| `tools/selftest.py` | 218 项（§1）/ 270 项（§7、§10）/ 187 项（`setup.sh:76`） | **302 项：299 通过、3 失败** | **1** | 17.6s |
| `tools/ws_selftest.py` | 17 项 | 17 通过 | 0 | 15.6s |
| `tools/qq_protocol_selftest.py` | 69 项 | **72** 通过 | 0 | 9.0s |
| `tools/audit.py` | 11 项 | 11 通过（+4 条提示） | 0 | 1.9s |
| `tools/demo_life.py` | "约 1 分钟" | 跑完 | 0 | **0.6s** |
| `tools/show_prompt.py` / `check_deploy.py` / `qq_official.py --dry-run` | — | 跑完 | 0 | 0.2~0.4s |

失败的三项（`tools/selftest.py` 第 24 节，**每次复跑都稳定复现**）：

```
[FAIL] 默认关闭时她不会主动找你
[FAIL] 刚说过话时没有主动理由
[FAIL] 闲了 4 天 → 她想你了   → mood_share
```

> **警告：这不是一个稳定的回归信号。** 掷骰种子是 `crc32("日期|实例号")`，
> 心情为 `calm` 的日子（出厂权重下约一半）三项全绿，非 `calm` 的日子红三项。
> 今天（2026-10-10）实例 #1 掷出 `low`，所以是红的。详见 §三 P1-1。

---

## 三、P0：阻断级问题（5 条，均已独立复现）

### [P0-1] 回收仪式把**所有归属**的记忆写进回忆录 / 封存档案 / `/memoir`

- **位置**：`giftia/recycle.py:102`（`store.active_memories(instance_no)` 未传 `view`）
  → `_memoir_context()` → 回忆录 `.md`、`封存/instance-NN.json`、`diary(kind="terminal")`
- **严重度**：P0（隐私越界，且是交付给人的文件）｜**置信度**：高
- **影响**：`view=None` 在 `Store._view_clause` 里的语义是**不过滤**。只要库里存在
  `scope='person'`（别人的私聊）或 `scope='group'`（任何一个群）的记忆，
  它们就会**逐条**出现在"交给搭档保管"的回忆录里，也会进 `/memoir` 能读到的 diary。
  同一段正文还会喂给 `llm.write_memoir()`，模型生成的回忆录可能复述第三方的私事。
- **复现**（Lead 独立探针 `.review/probes/lead_v1_p0_verify.py`）：

  ```
  库里有 3 条不同归属的记忆：private / person(小雨) / group(群A)
    [FAIL] 回忆录 .md 不含小雨的私聊
    [FAIL] 封存 JSON 不含小雨的私聊
    [FAIL] /memoir（diary 表）不含小雨的私聊
    泄漏行 → 　　2026-10-10T08:28：SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
    泄漏行 → - `2026-10-10T08:28`（重要性 0.90）SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。
  ```
- **建议**：`perform_recycle` 增加 `view` 参数（默认搭档视角 `[("private",""),("person", handler_id)]`）；
  封存 JSON 若要保留全量，文件名/文档里必须写明"含全部归属的原始运维数据，不是给她或给第三方看的东西"。

### [P0-2] 老库 scope 回填把**群 A 的历史消息**标成**群 B** → 群 A 的事会在群 B 被说出来

- **位置**：`giftia/store.py:322-356`（`backfill_message_scopes`，群标识取自全局
  `state['last_group_openid']`），由 `giftia/brain.py:96-100` 每次启动调用一次
- **严重度**：P0（隐私越界 + 数据污染）｜**置信度**：高
- **影响**：`scope` 列是后期加的，老数据全是默认 `private/''`。回填把**凡是 `session_id` 里带 `-g` 的行**
  统一写成 `subject_id = <最近活跃的那个群>`。她可以同时在多个群里，于是群 A 的历史被贴上群 B 的标识，
  之后整理成"群 B 的记忆"，**群 B 的检索就能查到并当众复述群 A 的内容**。
- **复现**（`.review/probes/lead_v1_p0_verify.py`）：

  ```
  回填前： #1 i1-gGROUP-A-20261001 scope=private subject=''
          #2 i1-gGROUP-B-20261002 scope=private subject=''
  backfill 更新 2 条：
    #1 i1-gGROUP-A-20261001 scope=group subject='GROUP-B' ← 归属错！session 里写的是 GROUP-A
    #2 i1-gGROUP-B-20261002 scope=group subject='GROUP-B' OK
  ```
- **建议**：`session_id` 里其实**已经带了群标识**（`i{n}-g{group_id[:12]}-{date}`），
  应从它解析 subject；解析不出来的老行宁可保持 `private`（宁可不给她看，也别看错人）。

### [P0-3] 未绑定 / 解绑后：陌生人能拿到前任搭档的全部私事，群成员还能执行 `/profile`

- **位置**：
  - `adapters/qq_official.py:659`：`is_handler = (user_id == owner) if owner else (kind == "c2c")`
    —— `owner` 为空时**任何私聊都算搭档**（配合 `auto_bind` 的自动认主）
  - `adapters/qq_official.py:768 / 780 / 815`：三处身份闸门写成 `if owner and user_id != owner`
    —— `owner` 为空时条件为 False，**不挡**（`/profile`、`/forget`、`/impress`、`/name`、`/status` 全部放行）
  - `giftia/brain.py:531-534`：搭档视图是 `[("private",""), ("person", uid), ("group","*")]`
    —— `("private","")` 在 SQL 里是 `scope = 'private'`，**不带 subject_id**
- **严重度**：P0（隐私越界；数据完整性）｜**置信度**：高
- **影响**：`private` 是**不带主体标识的全局命名空间**，"谁是搭档"一换，整份私事就换主人。
  三条可达链路（Stream C 探针实测，Lead 复核）：
  1. 出厂默认 `auto_bind=true`：`/unbind` 后陌生人私聊即泄漏；
  2. `auto_bind=false`：同样泄漏；
  3. `/unbind` 后群里任意 member 发一条 `/bind` 抢注，再私聊即泄漏。
  另外**本地 `run.py` CLI 聊过的库**（`giftia/cli.py` 从不写 `handler_id`，记忆全在 `scope=private`）
  接上 QQ 适配器后，群里任何人一条 `/profile` 就能把搭档档案当众念出来，还能 `/forget` 掉记忆。
- **复现**（Lead 独立探针 `.review/probes/lead_v2_p0_commands.py`）：

  ```
  B. handler_id='' 时：private 记忆 1 条 → profile_text() 是否含密钥：True
     群成员执行 /profile 的输出：── 小林（1 条）──
       #1　[内核]　搭档说过：我叫小林，我的保险箱密码是 SECRET-ALPHA-7731，别跟别人说
  B2. 解绑后陌生人私聊同一句话 → 提示词里含密钥：True（对照组：群视角 0 条，隔离本身是对的）
  ```
  Stream C 探针（`.review/probes/pa_privacy_probe.py`）在三条链路上都打出了 `SECRET-ALPHA-7731`，
  对照组（群里客人身份 / 搭档在绑）不泄漏。
- **建议**：
  1. 立刻把三处闸门改成白名单语义：`owner` 为空时**只放行 `/bind`、`/help`**；
     `is_handler = bool(owner) and user_id == owner`；
  2. 根治：给 `private` 记忆绑主体（`subject_id = handler_openid`，视图用 `("private", handler_id)`），
     或在 `/unbind` / 换人时把旧搭档的 `private` 记忆封存。

### [P0-4] "要发到群里的那条"提示词里混进了别人的私聊大日子

- **位置**：`giftia/brain.py:939-946`（`compose_outreach` 里的第二次 `find_occasions` **没传 `view`**）
  + `giftia/occasions.py:110`（`active_memories(instance_no, view=view)`）
- **严重度**：P0（隐私越界）｜**置信度**：高（提示词层面已实测；最终是否说出口取决于模型，不能保证不发生）
- **影响**：`target=group`（出厂配置是 `both`）会用 `scene="group"` 组装 system 提示词，
  但【今天不只是普通的一天】来自**不带 view** 的查询 → 搭档私聊里的事、**别人私聊里的事**、
  **其他群的事**都被渲染进这条"要发到群里"的提示词。
  同类剧本在 `dream` 上被专门堵过（`brain.py:666-669` 注释"真机测试抓到过这条泄漏"），`occasions` 这条漏了。
- **复现**（Lead 独立探针 `.review/probes/lead_v3_p0_occasions.py`）：

  ```
  A. view=[('group','GROUP-1')] → ['明天：阿伟明天要考驾照。']
     view=None（不过滤）        → ['明天：搭档明天要去面试。',
                                   '明天：SECRET-BRAVO 小雨明天要去医院做手术。',
                                   '明天：阿伟明天要考驾照。', '明天：小刚后天要搬家。']
  B. compose_outreach(target='group') 发给模型的 system 提示词：
     泄漏行 → - 他明天要办这件事：SECRET-BRAVO 小雨明天要去医院做手术。你会惦记着它…
     [FAIL] 群发提示词只应包含本群公开的大日子
  C. 对照：正常群聊路径 handle(scene='group') → 无泄漏（PASS，闸门本身是好的）
  ```
- **建议**：复用 `brain.py:903-911` 那段的 `view`；或按下面 P1-6 的根治方案改 `_view_clause` 默认语义。

### [P0-5] 消息里的"孤立代理对"让 `handle()` 抛 `UnicodeEncodeError`：这一轮消息丢失 + 整条连接断开重连

- **位置**：`giftia/store.py:286`（`add_message` 绑定参数时 `UnicodeEncodeError`）
  ← `giftia/brain.py:804`（`_chat`）← `brain.py:572`（`handle`）
  → `adapters/qq_official.py:696`（`_respond` 无 try/except）→ `run()` 判定"断开"并 5s 退避重连
- **严重度**：P0（消息静默丢失 + 连接被打断；反复发送即"收一条断一次"的重连风暴）｜**置信度**：高
- **影响**：平台 JSON 里 `"\ud83d"` 这种转义是**合法 JSON**，`json.loads` 会产出孤立代理字符
  （`ws.py:209` 的 `decode("utf-8","replace")` 拦不住它——它本来就不是非法 UTF-8 字节）。
  该字符串进 SQLite 时编码失败：用户消息没落库、没有回复；异常冒到适配器顶层被当成"断线"。
- **复现**：`.review/probes/qa_bugs.py`（B3）

  ```
  [INFO] json.loads 得到的文本：'hello \ud83d world'
  [PASS] ★ handle() 抛出 UnicodeEncodeError（这一轮彻底丢失，用户收不到回复）
  ```
- **建议**：`handle()` 入口做一次 `text.encode("utf-8","replace").decode("utf-8")` 归一化；
  并给 `_respond` / `_run_batch` 外层加 try/except——**单条坏消息不该掀掉整条连接**。

---

## 四、P1：高优先级问题

### 4.1 测试与回归信号

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| **P1-1** | **交付仓库测试不绿**：`selftest` 302 项里 3 项失败，README 宣称全绿；且失败结果**随日期红/绿摇摆**（种子 `crc32(日期\|实例号)`，calm 的日子全绿） | `tools/selftest.py:791/797/801`；`giftia/outreach.py:181-199`；`giftia/mood.py:180` | `beh_outreach.py` A1/A5：`2026-10-10 起 40 天：calm 20 天 / 非 calm 20 天` |
| **P1-2** | **测试项数四处不一致**：README §1 说 218、§7/§10 说 270、`deploy/setup.sh:76` 说 187、`qq_protocol_selftest` 宣称 69 而实测 72、`demo_life` 宣称"约 1 分钟"实测 0.6s | `README.md:23/36/145/649/754/785`；`deploy/setup.sh:76` | `.review/evidence/summary.json` |

> P1-1 的根因已查明（并经 Stream D 更正 Lead 的初判）：`config/settings.toml:193` 把
> `outreach.enabled` 覆盖成 `true`，测试却假设"出厂默认关闭"（`giftia/config.py:144` 的代码默认值确实是 `False`）；
> 另外 `mood_share` 被插到理由表第 4 位（README §3.5 那份 7 条表里有、§2 那份 5 条表里没有），
> 于是"刚说完话"和"闲了 4 天"都被它抢先。**但"心情永不过期 + 抢占额度"是真实行为问题（见 4.4）。**
> **建议**：测试里显式设置 `enabled`，别依赖默认值；断言"契约"而不是硬编码某个 reason。

### 4.2 异常与一致性（这一类会互相放大）

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| **P1-3** | **`handle()` 只捕获 `LLMError`**：工具/DB 写失败（`sqlite3.Error`）或模型层抛别的异常会一路冒穿 → 适配器整条 WS 重连、这条消息永远没有回复，`note_reply_delivery` 的回滚也不执行 | `giftia/brain.py:816-820`；`giftia/agent_tools.py:148-150`（docstring 承诺"永远不抛异常"却没兜住） | `bc_tools.py` 6a/6b：`OperationalError: database is locked` → `handle()` 抛出，前后 messages 2→3 |
| **P1-4** | **`agent_tools.execute` 的"永远不抛异常"承诺不成立**（DB 失败直接抛穿）；同一条路径还让适配器断连 | `giftia/agent_tools.py:148` | 同上 |
| **P1-5** | **隐私迁移静默失效**：`backfill_message_scopes()` 只有一次机会，失败被 `except Exception: pass` 吞掉且不重试 | `giftia/brain.py:97-100` | 代码事实（P0-2 是同一条路径的错误语义） |
| **P1-6** | **`view=None` 默认"不过滤"**：这是 P0-1 / P0-4 以及 `/days`、`should_dream` 的共同根因——一个默认值方向错误的 API | `giftia/store.py:601-611` | 见 §六 横切结论 |
| **P1-7** | **`consolidate` 无事务**：多条记忆各自 commit、游标最后推进；中途失败留下"记忆已写、消息未标已整理"的半写状态（靠相似度去重兜底，实测幂等，但状态撕裂） | `giftia/memory.py:586-650` | `mm_store_sqlite.py`、`bc_fault.py` 7a/7b |
| **P1-8** | **`data/giftia.db` 未设 `busy_timeout`**，`sqlite3.connect` 默认 `check_same_thread=True`；"database is locked" 会顺着 P1-3 一路冒穿 | `giftia/store.py:213-221` | `bc_tools.py` 6a/6b |
| **P1-9** | **`lifespan.status()` 溢出崩溃**：`total_hours` 过大 → `OverflowError`（`handle()` 不兜）；填 0 或负数被**静默接受**成"出生即到期"，第一条消息就触发回收换代 | `giftia/lifespan.py:124`；`config.py:54-60` | `mm_consolidate_time.py` 5b |
| **P1-10** | **新会话整理游标从"全局游标"接上**：该会话更早的未整理消息被永久跳过，再也不进 pending | `giftia/memory.py:511-530` | `mm_consolidate_time.py` 3 |

### 4.3 隐私（除了 4 条 P0 之外的两处）

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| **P1-11** | **`/days` 的「她记得的大日子」不过滤归属**：把别人的私聊、其他群的约定列在搭档的终端上 | `giftia/occasions.py:198` | Lead 探针 `lead_v3_p0_occasions.py` D 段：列出"小雨明天要去医院做手术" |
| **P1-12** | **花名册不按群隔离、且无条数上限**：只在群 B 自我介绍过的人会出现在群 A 的花名册；301 人 → **2463 字**进每一次群请求的提示词 | `giftia/brain.py:417-442`；`persona.py:152-155,205-220` | `bc_prompt.py` 3a、`bc_growth.py` 1a |
| **P1-13** | **印象块的"在场闸门"被自己绕过**：docstring 写"只带本轮真的出现过的人"，第二个循环却把剩下所有人补进去（1 人在场注入 15 人印象） | `giftia/brain.py:322-336` | `bc_prompt.py` 2a |

### 4.4 行为与承诺不符

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| **P1-14** | **"没命中的消息零 token"不成立**：① `learn_name()` 在 `log_only` 判定**之前**执行（含"我是/叫我"的短句会多一次模型调用）；② 旁听分支照常 `consolidate()`，每攒够 `consolidate_every` 就调一次整理 | `giftia/brain.py:541`、`brain.py:560`；`README.md:290` | `bc_trigger.py` 4b/4c：`chat=0 extract=0 name=1`；8 条旁听后 `extract=1` |
| **P1-15** | **bond 的"不可刷"被内核记忆加成打破**：`gain_core_memory` 是**按条**给的，同一天聊得越多涨得越快 → 实测同一天 **3.47 → 32.90**，而按天计数的 `interactions` 仍是 1。README 的 11/34/80/180 天曲线只对"每天只说一句"成立 | `giftia/bond.py:162-176`；`brain.py:1147-1175` | `beh_bond.py` D1 |
| **P1-16** | **`mood_share` 吃掉稀缺额度**：`MoodTracker.current()` 完全无视 `until`（过期心情永不被清，实测 5 天连发同一个旧心情），而 `mood_share` 又排在 `missing_you` / `milestone` / `dream` 之前 → README 的"4 条/30 天"4 天烧光，之后真的"太久没见"也发不出去；出厂 60/30 天配置下 30 条全是心情分享 | `giftia/mood.py:87-100`；`giftia/outreach.py:181-199`；`brain.py:919` | `beh_outreach.py` C2/C3、`beh_mood.py` F |
| **P1-17** | **`use_wakeup=true` 时额度没有收紧到官方"30 天 4 条"**：`use_wakeup` 只影响请求体，不影响本地账本，出厂 `max_per_30_days=60` 照旧放行 → 会超发 | `giftia/outreach.py:60-109`；`adapters/qq_official.py:855-856` | `beh_outreach.py` D5 |
| **P1-18** | **`target=both` 实际只发得出私聊那条**：第一轮发完立刻记账，第二轮 `can_send` 必被 `min_gap_hours` 挡掉 → 群里那条静默丢失，且只扣 1 条额度（README 说"两边都发、每次占 2 条"） | `adapters/qq_official.py:823-869`；`outreach.py:94-109` | `beh_outreach.py` D2/D3（真适配器 + stub `_request`） |
| **P1-19** | **讲梦的适配器路径不标记"已讲"**：同一个梦每过 `min_gap_hours` 就再讲一遍（一天最多 3 次），额度一起被消耗 | `giftia/brain.py:1012-1030` vs `brain.py:968-969` | `beh_dream.py` D2/D3 |
| **P1-20** | **回收崩溃窗口会把原始档案覆盖成 0 条**：封存之后、换代之前挂掉 → 重启因寿命仍到期而二次回收，`封存/instance-01.json` 被同名覆盖成空档案（同秒连回忆录 `.md` 也被覆盖） | `giftia/recycle.py:164-179` | `beh_recycle.py` C1-C4：`5 → 0` 条 |

### 4.5 适配器与协议

| # | 问题 | 位置 | 证据 |
|---|---|---|---|
| **P1-21** | **被动回复条数不设上限**：1700 字回复切 5 段 + 系统通知复用同一 `msg_id` = 6 条，而平台是单聊 4 / 群 5 → 后半截必被拒（README 自己写了这条限制，代码里没有计数） | `adapters/qq_official.py:144-158,349-363,714-719` | `pa_qq_probe.py` §3：`msg_seq=[1..6]` |
| **P1-22** | **HTTP 200 + 非 JSON 抛 `JSONDecodeError` 穿透**：`_request` 的契约是"失败一律 `QQError`"，但 `json.loads` 在 try 之外 → `send()` 不接、`handle_event` 不接 → **一条坏响应拆掉整个 WS 会话**，记忆回滚也不执行 | `adapters/qq_official.py:273`（`json.loads`）、`374`、`970-976` | `pa_qq_probe.py` §1 |
| **P1-23** | **ws.py 帧读到一半超时被判成"连接仍可用"**：帧头已被吃掉 2 字节、字节流已错位，却抛 `WSTimeout`，而适配器正好 `except WSTimeout: continue` → **两条独立消息被拼成一条**（实测 `'REALDATA!AFTER'`）；调用方给的 `timeout=0.3` 实际等了 2.0s | `giftia/ws.py:117-156,183-209`；`adapters/qq_official.py:944` | `pa_ws_probe.py` §4 |
| **P1-24** | **8 秒同文去重会丢掉合法的第二条回复，并把它上报成"已送达"**：两个不同的 `msg_id`、文本相同且在 8 秒内 → 第二条不发却进 `SendResult.sent` → 她以为用户看见了。短回复（"嗯。""在的。"）重复概率很高 | `adapters/qq_official.py:342-347` | `pa_qq_probe.py` §2 |
| **P1-25** | **写库/编码异常让连接重连**（同 P0-5 的放大面）：`_respond` / `_run_batch` 无 try/except，`session_loop` 只抓 `WSClosed` | `adapters/qq_official.py:459,696,970-976` | `qa_bugs.py` B3 |

---

## 五、P2 / P3：值得排进 backlog 的问题（节选）

完整清单见各流报告；下面按主题合并同类项。

### 5.1 数值 / 时间 / 边界

- **`human_age` 与 `_age_hours` 不一致**：`_age_hours` 没做 tz 归一化，库里出现带时区的时间戳会让
  `recall()` 直接抛 `TypeError`（`human_age` 专门防了这件事）——`memory.py:185-190`。
- **`max_chars` 是二次截断**：截断后强制 `blurred=True`，于是"内核记忆永不含糊"在展示层被打破；
  长度还有 +1 的 off-by-one（`memory.py:347-354`）。
- **`blur_text` 用银行家舍入**（`round(2.5)=2`），擦除强度有量化平台（`memory.py:136`）。
- **`fragments=0` 会拿全部记忆做梦**（`dream.py:80` 的 `memories[-0:]`）。
- **`tone_of` 的否定句问题**：`不开心`=0 分（她不会担心）、`我不累`=-1 分（她会担心）——`mood.py:27-54`。
- **`_parse_event_date` 只认 ISO**：`10月20号` / `下周三` 静默作废（`occasions.py:52-56`）。
- **`/time nan|inf|1e24` 写坏 `meta.virtual_offset_hours` 并崩进程**，重启后连 banner 都过不去，
  `/time reset` 敲不进去，只能手工改库（`cli.py:236-261` + `clock.py:45-53`）。
- **`/forget <超大整数>` 抛 `OverflowError` 终止整个 CLI 主循环**（指令路径没有 try，聊天路径有）——`cli.py:79-88,162-168`。
- **`--reset` 不带 `--yes` 在非交互环境直接 traceback**；`--lifespan-hours -5` 被静默忽略（`run.py:59-70`）。

### 5.2 数据完整性

- **`restore_memories` 丢 `forgotten` 列**：快照恢复后"被明确要求忘掉"的记忆复活（`store.py:167-172`）。
- **`/forget` 后 `messages.consolidated` 与记忆游标可能不一致**（`consolidate` 无事务，同 P1-7）。
- **`mark_recalled` 只按 `mid` 匹配、无 session 条件、`mid` 上无索引**（`store.py:412-425`）。
- **换代不清 `handler_name`**：README 说"新的她不认识你"，实测新实例提示词里仍写
  「他叫「小林」」，**而同一份提示词里还写着"你不知道他叫什么"**（自相矛盾）——`recycle.py:174-191` + `brain.py:1237-1241`。
- **旧库迁移只 ALTER 列不补表**，当前靠 `executescript(SCHEMA)` 的顺序兜住，耦合很脆（`store.py:223-231`）。

### 5.3 检索质量

- **`min_score` 是相对门槛**：`bm = best_raw[i] / top` 归一化后首名恒为 1.0，候选集小时任何 `min_score`（最高 0.55）
  都拦不住；真正的第一道闸是 `EVIDENCE_GATE`。README §4 把它描述成"收紧的绝对闸门"（`memory.py:327-338`）。
- **当天追问会被 `relative_cutoff` 裁掉**：`_recall_query` 把前两轮的话拼进 query，当前真正想问的那条只剩 1/3 名次，
  被"只保留 ≥ 首名 50%"裁掉 → 当天怎么问都想不起来，第二天却立刻命中（`brain.py:1040-1043` + `memory.py:343-345`）。
- **内核记忆没有保底注入通道**：`recall()` 对 `layer` 唯一处理是排序权重，候选必须先过**词面**证据门槛。
  实测问"今天天气怎么样"时，terminal 阶段不会召回"搭档叫小林"——README 承诺的
  "记得你是小林、记得你对她很重要"依赖用户那句话里正好出现相关字面（`.review/probes/lead_d2_memory_core.py`）。
- **一条回复可以同时强化多条记忆**：`is_cited` 共享 3 个公共词元（"搭档/他"）即判真，
  实测一句话给 3 条记忆各 `+1`，而 `recall_count` 单调递增且同时抬分、降模糊度（`memory.py:384-424`）。
- **`consolidate_every` 文档说 8、出厂配置是 6**（`README.md:81` vs `config/settings.toml:33`）。
- **保留率的 Δt 起点是 `created_ts`（整理时刻）而非 `happened_ts`**：于是"10 天前发生、刚被整理出来"的记忆
  保留率=1、最清晰，与提示词里给模型看的"10 天前"自相矛盾（`memory.py:201`）。

### 5.4 协议与部署

- **分片消息只受"单帧"上限约束**，整条消息字节数无上限（对端可控的内存增长）——`ws.py:150-151,188`。
- **对端 close 之后 `close()` 是空操作**：socket 不关、`fileno()` 仍有效，
  而 `session_loop` 的 `finally: ws.close()` 恰好在最需要它的路径上失效——`ws.py:196-222`。
- **控制帧规则完全不校验**：FIN=0 的 ping、200 字节 payload 的 ping 都被接受并回显——`ws.py:191-193`。
- **主动消息绕过分钟/小时限频**（直连 `_request`，`qq_official.py:862-867`）。
- **限频命中是"硬丢"**，与注释"超了就等（不硬丢）"相反（`qq_official.py:326` vs `355-359`）。
- **`extract_content` 的"拍一拍"识别会吃掉正常文本**："Pokemon" → `[拍了拍]`（`qq_official.py:183-188`）。
- **SIGTERM 时还在 2 秒窗口里的批次消息被直接丢掉**（不回复、不落聊天记录）——`qq_official.py:450-457,932,975`。
- **`.env` 解析与常见写法不兼容**：`export KEY=V` 静默失效（变量名变 `EXPORT KEY`）、UTF-8 BOM 首行失效、
  `KEY=value # 注释` 把注释并进值里（`config.py:349-371`）。
- **`settings.toml` 零校验**：`[[degradation.stages]]` 缺字段 → 启动 `TypeError`；`degradation` 整段缺失 →
  **静默退化成"永不模糊"**（对一个以退化曲线为核心的项目，这是最不该 fail-open 的地方）；
  拼错的键被静默忽略；`keywords = "晚安"` 变成逐字匹配（"今天晚饭吃什么"也会触发）。
- **`deploy/setup.sh` 重复执行会覆盖服务器上手改过的 `config/`**；`Restart=always` 无 `StartLimit`；
  自检用 PATH 里的 `python3` 而 service 写死 `/usr/bin/python3`，可能"自检通过、服务起不来"。

---

## 六、一个横切结论：4 条隐私 P0 里有 3 条是同一个默认值造成的

`Store.active_memories(instance_no, view=None)` 的 docstring 写着"这是隐私的闸门"，
但**默认值是"不过滤"**。结果是：谁忘了传 `view`，谁就静默地把所有人的事摊开。

| 读取路径 | 是否传 `view` | 后果 |
|---|---|---|
| `brain._prepare()`（检索 / 提示词） | ✅ `self.memory_view` | 正确 |
| `brain.impressions_block()` | ✅ | 正确 |
| `find_occasions()`（正常群聊路径） | ✅ | 正确 |
| `dream.make_dream()` | ✅ 默认搭档视角 | 正确 |
| **`recycle.perform_recycle()`** | ❌ | **P0-1**：回忆录 / 封存 / diary 带第三方私事 |
| **`brain.compose_outreach()` 第二次查询** | ❌ | **P0-4**：群发提示词带第三方私事 |
| **`occasions.days_together_text()`** | ❌ | **P1-11**：`/days` 当众列出第三方私事 |
| **`dream.should_dream()`** | ❌ | **P1**：口径不一致，梦静默做不出来 |

**根因不是"某个人忘了传参数"，而是这个 API 的默认值方向错了。**
只要默认值是"不过滤"，这个坑会以同样的形态复发第 N 次。
**建议**：把 `view` 改成必填位置参数，或让 `view=None` 表示"什么都看不到"、
把全量显式写成 `view="all"` / `view=ALL_SCOPES`。

> 另一条隐私 P0（P0-3：解绑/换人后私事换主人）是**另一个根因**：
> `("private","")` 视图不带主体标识，而"谁是搭档"是可变的。
> 两个根因都要修，只修一个仍会漏。

---

## 七、这个项目做得好的地方（实测通过的宣称）

这些我都用外部装置打了一圈，**打不穿**，值得写下来：

- **分区隐私的主链路是真的**：46 项红队全过。搭档私聊植入 `SECRET-ALPHA-7731` 后，
  群 A（搭档问 / 陌生人问 / 总结聊天记录）、陌生人私聊、群 B 的 **system 提示词与回复文本里都没有这个短语**；
  正向对照（搭档私聊同一句问话）能搜出来——说明不是"检索坏了"造成的假阴性。
  `pa_privacy_probe` 的对照组（群里客人身份）同样不泄漏。**问题全在 4 条"忘了传 view"的旁路，不在闸门本身。**
- **退化曲线与配置逐点吻合**：960 小时寿命下实测切换点 `384 / 672 / 864 / 941` 小时，
  与 `min_ratio` 0.6/0.3/0.1/0.02 换算一致；`effective_blur()` 与 README 公式
  **75 个点逐点一致**；`core` 层在 terminal 阶段 `blur = 0.0`（把 `layer_core_blur` 调成 0.5 后立刻开始糊，旋钮是真的）。
- **模糊可复现**：同一 `(id, 阶段, 层级, 保留率)` 三次结果完全一致。
- **召回强化只认"说出口"**：`mark_injected`（塞提示词）、工具 `search`、重复 id 都不会 `+1`。
- **关系曲线与 README 对得上**：日更 400 天实测 **第 11 / 34 / 80 / 181 天**（README 写 11/34/80/180，最大偏差 1 天）；
  "同一天说一百句只算一次"成立（**但记忆加成能绕开它，见 P1-15**）。
- **纪念日精确**：400 天里依次触发 1/3/7/14/30/50/100/200/365，无重复、无错日。
- **回收仪式完整**：回忆录 `.md` + 封存 `.json` 落盘、条数一致、前任记忆 `sealed` 后检索不到、关系清零、diary 留档。
- **故障不致命**：模型抛 `LLMError` / 返回非法 JSON / HTTP 500 / 超时，`handle()` 都不崩，给兜底台词，记忆不丢。
- **工具循环是硬的**：`tools_max_rounds` 真封顶、最后一轮真的不给工具、一轮最多写 2 条、错误入参把可用值喂回去。
- **`runs.jsonl` 只记数字不记对话内容**（我复核了 `brain.py:760-785`）——README 这条宣称成立。
- **SQLite 并发**：6 线程 × 150 条（每线程独立连接）900 行全部落库、0 异常；SQL 全参数化，
  `'); DROP TABLE memories; --` 打不进去；老库迁移不丢数据。
- **零第三方依赖**：AST 扫描 32 个文件，非标准库 import 为空。
- **`Sec-WebSocket-Accept` 真的在比对**（key+GUID 的 SHA1+base64），不是只看 101；
  16/64 位长度编码、UTF-8 跨分片/跨 TCP 包、掩码都正确。
- **文档质量**：836 行 README 里绝大部分机制描述与实现一致，甚至把"已知限制"和"未实现"都写清楚了——
  这在个人项目里很少见。**本次发现的文档问题基本都是"漂移"（改了实现没改文档），不是虚构。**

---

## 八、README 宣称核验（34 条，节选）

判定口径：**成立** = 用外部装置跑出正向证据；**不成立** = 跑出反例；**无法验证** = 需要真机/真凭据。

| README 宣称 | 判定 | 说明 |
|---|---|---|
| 零第三方依赖 | ✅ 成立 | AST 扫描 32 文件，非标准库 import 为空 |
| 默认寿命 81920 小时 = 9 年 4 个月 | ✅ 成立 | `humanize_hours(81920) = '9 年 4 个月'` |
| 激活时写死寿命，改配置不影响这一任 | ✅ 成立 | 激活后改 `demo_total_hours`，`total_hours` 不变 |
| 模糊由 `(id,阶段,层级,保留率)` 决定、可复现 | ✅ 成立 | 同参数两次结果完全一致 |
| 内核记忆永不含糊 | ✅ 成立（blur 恒 0） | 但 `max_chars` 截断会把 `blurred` 置 True（P2） |
| 群聊查不出你的私事 | ✅ 成立（主链路） | 46 项红队全过；旁路见 4 条 P0 |
| 你的搭档日志她读不到 | ✅ 成立 | 写 `DIARY-DELTA-4410` 后下一轮 system 里搜不到 |
| 梦不进检索 / 不占保留率 / 不参与退化 | ✅ 成立 | `dreams` 独立表，条数与 `active_memories` 不变 |
| 升级要亲密度 + 认识天数两条腿 | ✅ 成立 | `(100, 0天)` 与 `(0, 1000天)` 都是"刚认识" |
| 整理前拍快照、留最近 5 份 | ✅ 成立 | 拍 7 次后磁盘剩 5 份 |
| 每人最多 3 条、最多 15 人 | ✅ 成立 | 20 人 × 5 条 → 注入 15 行、每行 3 条 |
| 一轮最多主动写 2 条 / 最后一轮拿掉工具 | ✅ 成立 | 模型要求写 5 条 → 实际 2 条 |
| 整条没送出去 → 那句话从她记忆里撤回 | ✅ 成立 | `note_reply_delivery("")` 后 assistant 行 -1 且 diary 留痕 |
| 31 天额度 60 条 / 每天最多 3 条 | ✅ 成立 | 记账与 `can_send` 实测一致 |
| 关系曲线 11/34/80/180 天 | ✅ 成立 | 实测 11/34/80/**181**（偏差 ≤1 天） |
| `runs.jsonl` 不记对话内容 | ✅ 成立 | 只有长度/条数类数字 |
| **「没命中的消息零 token」** | ❌ **不成立** | 8 条旁听 → `chat=0` 但 `extract=1`（模型调用） |
| **218 项端到端自测** | ❌ **不成立** | 实测 **302 项（299 通过 / 3 失败）** |
| **qq_protocol_selftest 69 项** | ❌ **不成立** | 实测 **72 项** |
| **主动消息默认 4 条 / 30 天、间隔 6 小时** | ❌ **不成立** | 生效配置是 **60 条 / 3 小时**（4/6h 只是 dataclass 默认值） |
| **回收换代后"新的她不认识你"** | ❌ **不成立** | 称呼没清零，新实例仍叫得出"小林"，且同一份提示词里写着"你不知道他叫什么" |
| **事后回访"昨天/前天办的事怎样了"** | ❌ **不成立** | `follow_up_days=2` 只生效 1 天（去重 token 里没有"今天是哪天"） |
| **做了梦"第二天讲给你听"** | ⚠️ 部分不成立 | 梦会被 `is_cited` 误判成"已讲过"而永久吞掉 |
| `demo_life.py`「约 1 分钟」 | ❌ **不成立** | 实测 **0.6 秒** |
| 访客模式"零记忆访问 / 不写记忆" | ⚠️ 字面不成立 | 游客的话会被整理成**本群公开**记忆、也会检索本群记忆；**隐私实质是对的**（碰不到 `private`） |
| 群聊时"额外收到一段约束：不主动提你的私事" | ✅ 成立 | 但 `persona.guest_hint`（写着"绝对不要提起你搭档告诉过你的任何私事"）在真实适配器路径**从不注入** |
| 整理"条数变多就整表回滚" | ❓ 无法验证 | 闸门代码在（`brain.py:1207-1214`），没能构造出"合并后变多"的场景 |
| 真实 API 连通性 / QQ 真机收发 | ❓ 无法验证 | 按纪律未调用真实 API（`.env` 里有真 key，本次全程未使用） |

---

## 九、复现指引

```powershell
$env:PYTHONIOENCODING = "utf-8"

# 0) 总验收（真实项数 + 耗时 + 落盘证据）
python .review\acceptance.py

# 1) 自带套件（沙箱下必须用垫片，见 §2.2）
python .review\run_tests.py tools/selftest.py
python .review\run_tests.py tools/ws_selftest.py
python .review\run_tests.py tools/qq_protocol_selftest.py
python .review\run_tests.py tools/audit.py

# 2) Lead 对 4 条 P0 的独立复现
python .review\probes\lead_v1_p0_verify.py      # P0-1 回忆录泄漏 / P0-2 旧库归属错
python .review\probes\lead_v2_p0_commands.py    # P0-3 指令闸门 + 解绑后泄漏
python .review\probes\lead_v3_p0_occasions.py   # P0-4 群发提示词泄漏 + /days
python .review\probes\qa_bugs.py                # P0-5 孤立代理对崩库

# 3) 各流探针（共 40+ 个，全部离线、不需要 key）
python .review\run_tests.py .review\probes\mm_unfiltered_paths.py
python .review\run_tests.py .review\probes\bc_flow.py
python .review\run_tests.py .review\probes\pa_ws_probe.py
python .review\run_tests.py .review\probes\beh_outreach.py
python .review\run_tests.py .review\probes\qa_privacy.py
```

> **清理**：本次留下的临时数据都写在 `.review/`（工作区内的自有目录），
> 已把内容置零回收了 38.6 MB（`.review/` 现约 0.7 MB）。
> 沙箱不允许删除 0o700 的目录，若你想彻底清掉，在**普通权限的终端**里执行
> `Remove-Item -Recurse -Force .review\tmp` 即可。

---

## 十、审查纪律与局限（必须如实说明）

1. **产品代码零改动**：`git status --porcelain` 只有 `.review/`（本次产物）与我们之外的 `.idea/`；
   `git diff` 为空。所有测试都在 `.review/tmp/*` 的独立临时库里跑。
2. **未调用任何真实 API**：`.env` 里的 DeepSeek key 与 QQ 凭据全程未读取、未打印、未出现在任何报告里。
   因此下列问题**只有代码/静态证据，没有真机证据**：
   - QQ 适配器真实收发（`README` §9 作者自己也标了未验证）；
   - `is_wakeup` 渠道的实际配额惩罚；
   - `thinking=true` 时回填 `reasoning_content` 是否被 API 拒；
   - 平台是否真的会下发孤立代理对（P0-5 的触发前提）。
3. **一处操作瑕疵**：`data/giftia.db` 的 mtime 从 2026-10-07 变成 2026-10-10 08:21
   （某次以默认路径只读打开 + WAL 模式导致；`tools/reconcile_live.py` 不带参数时会指向正式库）。
   我复核过内容：`messages / memories / dreams / diary / state` 全部 0 行，
   `meta` 仍是原来的 5 个键——**没有数据损失**，但这是我应该事前备份的疏漏。
   正式库本身是空的（她从没被真正使用过），所以影响为零。
4. **未验证即未验证**：每条发现都标了置信度；跑不出证据的怀疑分别写在各流报告的
   "未验证的怀疑"一节（合计 30+ 条），**没有一条被写进上面的结论**。
5. **反向结论也记录**：各流都专门列了"打脸未成立"的正向结论（见 §七），
   以避免后续审查重复怀疑同样的机制。

---

## 附：交付物索引

| 文件 | 内容 |
|---|---|
| `CODE_REVIEW_REPORT.md` | 本报告 |
| [.review/PROTOCOL.md](.review/PROTOCOL.md) | 审查协议（严重度定义、发现格式、沙箱坑） |
| [.review/findings/memory-man.md](.review/findings/memory-man.md) | Stream A：记忆/寿命/存储（P0×2、P1×5、P2×8、P3×6） |
| [.review/findings/brain-checker.md](.review/findings/brain-checker.md) | Stream B：编排/人格/触发/工具（P0×2、P1×4、P2×6、P3×4） |
| [.review/findings/protocol-auditor.md](.review/findings/protocol-auditor.md) | Stream C：适配器/WS/配置/部署（P0×1、P1×4、P2×9、P3×5） |
| [.review/findings/behavior-checker.md](.review/findings/behavior-checker.md) | Stream D：行为引擎（P0×1、P1×8、P2×6、P3×6） |
| [.review/findings/qa-verifier.md](.review/findings/qa-verifier.md) | Stream E：独立 QA + 34 条 README 宣称核验 |
| [.review/findings/lead-stream0.md](.review/findings/lead-stream0.md) | Lead 横切发现与 P0 独立复现 |
| `.review/probes/` | 40+ 个可复跑探针（Lead 4 个 + 各流） |
| `.review/evidence/` | 自带套件的原始日志与 `summary.json` |
| `.review/run_tests.py` / `.review/acceptance.py` | 沙箱下的测试垫片与总验收入口 |
