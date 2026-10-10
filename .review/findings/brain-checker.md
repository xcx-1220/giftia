# Stream B 审查报告：编排中心（brain / persona / trigger / agent_tools）

审查者：`brain-checker`（task-2）　日期：2026-10-10
对象：`giftia/brain.py`（1535 行，任务书写的 1318 行已过时）、`giftia/persona.py`、`giftia/trigger.py`、`giftia/agent_tools.py`
方法：静态通读 + 7 个离线探针（MockLLM / 适配器 dry-run，**不联网、不用真 key**）。所有结论都有跑出来的输出。
产品代码一行未改。

## 复现命令

```powershell
$env:PYTHONIOENCODING="utf-8"
python .review\probes\bc_flow.py        # 流水线适配器分支（未绑定指令闸门/认主/转发/聚批） 22 PASS
python .review\probes\bc_branches.py    # 各分支隐私边界与落库归属                     28 PASS / 3 FAIL(检出)
python .review\probes\bc_trigger.py     # trigger 纯函数边界 + “零 token”核验            31 PASS
python .review\probes\bc_prompt.py      # system 块顺序/前缀稳定性/印象闸门/裁剪          8 PASS / 2 FAIL(检出)
python .review\probes\bc_tools.py       # 工具循环封顶/写库上限/错误路径                  18 PASS / 2 FAIL(检出)
python .review\probes\bc_fault.py       # 异常与一致性（模型失败/DB 失败/半写）            16 PASS / 1 FAIL(检出)
python .review\probes\bc_growth.py      # 无界增长（花名册 / people_stats）                4 PASS
```

> 探针里的 `[FAIL]` = **断言“这里有问题”成立**（检出），不是脚本坏了。探针自建 `.review\tmp\brain-checker-<name>-<pid>-<ts>\data\giftia.db`，
> 每次运行都是全新目录，重复跑结果一致；从不碰 `data/giftia.db`。

> 背景：`python .review\run_tests.py tools/selftest.py` 本次实测 **通过 299 项、失败 3 项**（失败都在第 24 节 `outreach`，
> 属 Stream D 范围：`默认关闭时她不会主动找你` / `刚说过话时没有主动理由` / `闲了 4 天 → 她想你了 → mood_share`）。
> 也就是说现有套件本来就不是全绿。

---

## P0

### [P0] 未绑定状态下，群里任何成员都能执行 `/profile`，把搭档的私聊记忆当众打印进群里（还能 `/forget` `/impress`）

- 位置：
  - `adapters/qq_official.py:573`（`handle_command` 在**任何**群消息上先跑，早于 @/档位判定）
  - `adapters/qq_official.py:779-782`（闸门写成 `if owner and user_id != owner`，`owner` 为空时**直接放行**）
  - `giftia/brain.py:363-390`（`profile_text()` 用的是**搭档视角** `private + person + group*`）
  - `giftia/brain.py:414-415`（`forget_memory()` 没有任何归属校验）
- 严重度：**P0**（隐私越界：别人的私聊记忆泄漏到群里；数据完整性：陌生人可让记忆失效/写入）；置信度：**高**（跑出来的）
- 现象与影响：
  - `handler_id` 为空时（刚 `/unbind`；或**本地 CLI 聊过之后再接 QQ 适配器**——`giftia/cli.py:85` 调 `brain.handle(text)`
    从不写 `handler_id`，记忆全部落在 `scope=private`；或 `QQ_OWNER_OPENID` 没配且还没私聊），
    群里任何一个成员 @ 她（全量模式下甚至不用 @）发 `/profile`，她就把**搭档视角的全部档案**（含 `scope=private` 的私聊记忆）发到群里。
  - 同一个入口还能 `/forget <编号>`（把记忆标成 `forgotten`，永久不参与检索）、`/impress <名字> <内容>`（往记忆里写东西）、
    `/name`（改她怎么称呼搭档）、`/reconcile`（触发记忆合并）。
- 触发条件（最小复现）：`handler_id == ""`，群里陌生人发 `@她 /profile`；或全量模式下群里任何一条 `/profile`。
- 证据（`python .review\probes\bc_flow.py`）：
  ```
  == 0. 前置：本地 CLI（run.py → cli.py:85 brain.handle(text)）不写 handler_id ==
    [PASS] 0a CLI 聊过之后 handler_id 仍是空
    [PASS] 0b 记忆落在 private（谁都能在群里 /profile 出来的那批）

  == 1. 未绑定（handler_id='') 时群里的指令闸门 ==
        陌生人 /profile 被当成指令处理：True
    [PASS] 1b ★ 未绑定时陌生人的 /profile 被执行（应当挡下）
    [PASS] 1c ★ 搭档视角的私聊档案被发进群里
        输出片段：['  #1\u3000[重要]\u3000搭档的秘密 SECRET-ALPHA-7731：他下周三要考驾照']
    [PASS] 1d ★ 未绑定时陌生人能 /forget 掉搭档的记忆
    [PASS] 1e ★ 未绑定时陌生人能 /impress 往记忆里写东西
  == 2. 指令不需要被 @（群里任何一条以 / 开头的消息都会进指令处理）==
    [PASS] 2a ★ 未被 @ 的群里 /profile 也被执行了（没走触发判定）
  == 2b. 已绑定后同样的指令被挡（对照组）==
    [PASS] 2a 已绑定时陌生人的 /profile 被挡回
  ```
  （对照组证明：`owner` 非空时闸门是有效的，问题只出在“未绑定 = 放行一切”。）
- 建议修法（最小改动方向）：
  1. `handle_command` 里把身份闸门改成白名单语义：`owner` 为空时**只放行 `/bind`、`/help`**，其余指令一律婉拒；
     已绑定时要求 `user_id == owner`（不要用 `if owner and ...`）。
  2. 群里的指令再加一道“被 @ 或全量模式”判定，避免任何人用一条 `/` 消息把她的档案当众念出来。
  3. `profile_text()` 加一个“只在私聊里可用”的前置条件（它本来就是搭档视角）。

### [P0] 换人之后：新搭档的第一条私聊就能拿到旧搭档的全部私聊记忆，她还会当面说出来

- 位置：`giftia/brain.py:510-538`（`is_handler = (uid == owner) if owner else (self.last_scene != "group")`；
  非 handler 才给 `person` 视图，handler 给 `private + person + group*`）
  ＋ `adapters/qq_official.py:642-648`（`auto_bind`：未绑定时第一条私聊直接 `set_handler_id(user_id)`）
  ＋ `adapters/qq_official.py:759-765`（`/unbind` 把 `handler_id` 清空；`README.md:592` 把 `/unbind` 写成“可以改主意”的正常操作，
  适配器自己的帮助文本也写「想换人：/unbind」，见 `adapters/qq_official.py:98,648,653,1092,1096`）
- 严重度：**P0**（隐私越界；前搭档的私事被新人看到并被她说出口）；置信度：**高**（跑出来的）
- 现象与影响：`/unbind` 之后，任何人的第一条私聊都会被认成搭档（auto_bind），
  而搭档视图包含 `scope=private` 的全部记忆 → 上一任的私事被注入提示词，并出现在她的回复里。
- 触发条件：`/unbind`（或重新部署时 `QQ_OWNER_OPENID` 为空、库里已有 private 记忆）→ 陌生人私聊一句相关的话。
- 证据（`python .review\probes\bc_branches.py`）：
  ```
  == 场景 D：解绑后（handler_id='')，陌生人的第一条私聊 ==
    [PASS] D1 解绑后陌生人被判成搭档
    [FAIL] D2 ★ 前搭档的私事进了新人的提示词
        提示词里的泄漏行：['- 椎名：搭档的秘密 SECRET-ALPHA-7731：他下周三要考驾照，很紧张']
        （她的回复：……我在听。你提到这个，我想起之前的事——搭档的秘密 SECRET-ALPHA-7731：……）
  ```
  `bc_flow.py` 3a 另外证明适配器层同样：`[自动认主·私聊] 搭档 = STRANGER-Z`。
- 建议修法：解绑/换人要有一个明确的“前搭档记忆怎么办”的决定，最小做法是
  在 `handle()` 里把“刚认主的第一条私聊”当作**未确认状态**：只回一句认亲台词、`memory_view` 先给 `[("person", uid)]`，
  等用户明确说一句（`/name` 或“是我”）再放开 `private`；或者 `/unbind` 时把 private 记忆改成不再随新搭档召回的归属。
  （这条与 README 的“自动认主”设计有张力，改法需要产品决策，但现在的行为是隐私越界。）

---

## P1

### [P1] “没命中的消息零 token” 不成立：旁听路径有两条会真的调模型的支路

- 位置：
  - `giftia/brain.py:541`（`learn_name()` 在 `log_only` 判定**之前**执行）→ `giftia/brain.py:253-270` 的模型兜底 → `giftia/llm.py:449-458`（真机是 `chat()` = 一次 API 调用）
  - `giftia/brain.py:560`（旁听分支末尾照常 `self.consolidate()`）→ `giftia/memory.py:559`（`len(pending) >= consolidate_every` 就调 `extract_memories`）
  - 宣称方：`README.md:290`「不建会话、不调模型 —— **零 token**」、`giftia/trigger.py:16`、`adapters/qq_official.py:592`
- 严重度：**P1**（核心机制与文档承诺相反；成本静默发生）；置信度：**高**（跑出来的）
- 现象与影响：
  1. 一条 ≤30 字且含 `我是/叫我/我叫/我的名字/名字叫` 的旁听消息（例如「我是来看热闹的」）会额外花一次模型调用做名字判定；
  2. 同一会话里旁听消息攒到 `consolidate_every`（出厂 `config/settings.toml:33` = **6**，不是 README 说的 8）就会调一次模型整理记忆。
  在全量监听的大群里，第 2 条意味着**每 6 条群消息一次 API 调用**，与“零 token”直接相反。
- 触发条件：群里未命中触发的消息 → `handle_message(..., log_only=True)`。
- 证据（`python .review\probes\bc_trigger.py`）：
  ```
  == 4. 旁听（log_only）到底花不花 token ==
    [PASS] 4a 普通旁听一句：零模型调用
        计数：chat=0 tools=0 extract=0 name=0 rewrite=0
    [PASS] 4b ★ 旁听一句「我是来看热闹的」也调了模型（名字判定）
        详情：chat=0 extract=0 name=1
    [PASS] 4c ★ 旁听到第 8 条触发一次记忆整理（extract_memories）
        每收一条旁听后的 extract_calls：[0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
        （配置 consolidate_every=6）
  ```
- 建议修法：
  1. `learn_name` 移到 `if log_only:` 之后（旁听只走正则、不做模型兜底），或给 `learn_name` 加 `allow_llm=False`；
  2. 旁听分支不要直接 `consolidate()`：改成“够阈值时只置个标记”，由搭档那一轮或定时任务统一整理；
  3. 把 `README.md:290`、`trigger.py` 文档头、适配器注释改成「不回复、不建会话；累积到阈值会花一次整理调用」。

### [P1] 写库失败（sqlite3.Error）会从 `handle()` 抛穿：`_chat` 只捕获 `LLMError`，适配器因此整条 WS 重连、这条消息永远没有回复

- 位置：
  - `giftia/brain.py:816-820`（`try: reply = self._reply_with_tools(...)` / `except LLMError`，**其它异常一律上抛**）
  - `giftia/agent_tools.py:148-150`（docstring 明确承诺「**永远不抛异常**——工具报错不该让整轮对话崩掉」，但 `_remember` 直接 `hub.store.add_memory(...)`，`_forget`/`_search` 直接读库）
  - `giftia/memory.py:621-637`（`consolidate` 中途 `add_memory` 失败无事务，见 P2）
  - `adapters/qq_official.py:937-962`（`session_loop` 调 `handle_event` **没有 try**）→ `adapters/qq_official.py:988-994`（`run()` catch → 打印 `[断开]` → 退避重连）
- 严重度：**P1**（未捕获异常路径 + 消息丢失 + 连接被打断）；置信度：**高**（注入 `sqlite3.OperationalError` 复现；异常类型不是关键，关键是 handle 只兜 LLMError）
- 现象与影响：DB 短暂锁住/磁盘满/工具写库失败时，异常穿过 `_chat` → `handle()` → 适配器 `session_loop` → `run()`，
  结果是**整条 WebSocket 断开重连**；用户那句话已经落库、但没有任何回复，也不会走 `note_reply_delivery` 的回滚。
  同一层的另一个证据：模型层抛出非 `LLMError` 时（换 provider、库内部错误）同样直接抛穿。
- 触发条件（最小复现）：工具循环里 `remember` 写库报错 / `consolidate` 写库报错 / LLM 抛非 `LLMError`。
- 证据：
  ```
  # python .review\probes\bc_tools.py
  == 6. execute 的'永远不抛异常'承诺 vs DB 写失败 ==
    [FAIL] 6a DB 写失败时 execute 仍然返回文本（承诺成立）  -> 抛出了 OperationalError: database is locked
    [FAIL] 6b DB 写失败时 handle() 不抛异常  -> OperationalError: database is locked
        handle() 抛出：OperationalError: database is locked
        抛出前后 messages 条数：2 -> 3（用户的这句话已落库但没有回复）

  # python .review\probes\bc_fault.py
  == 3. 非 LLMError 异常：handle() 只捕获 LLMError ==
    [FAIL] 3a 非 LLMError 也被兜住（不抛异常）  -> ValueError: 模型层冒出来的莫名异常
        handle() 抛出：ValueError: 模型层冒出来的莫名异常
        抛出后 DB 状态：[('user', '在吗')]
  ```
- 建议修法：
  1. `_chat` 里把 `except LLMError` 扩成 `except Exception`（LLMError 仍用 `FAILURE_LINES`，其它错误也给兜底回复 + 在 `notices` 里标明错误类型），保证“一轮一定要有回复”；
  2. `agent_tools.execute` 用一个大 `try/except Exception` 包住三个工具，失败就回一句人话（它自己的 docstring 就是这么承诺的）；
  3. 适配器 `handle_event(...)` 外层加 try/except：一条消息处理失败不该让连接重连（重连会顺带丢掉这一轮和批次状态）。

### [P1] 【你对他们的印象】的“在场闸门”被自己绕过：群里 14 个不在场的人照样进提示词

- 位置：`giftia/brain.py:322-336`（第一个循环只收在场的人，**第二个循环把 `by_person` 里剩下的人全部补进去**，`present[:max_people]` 才截断）
  - docstring 自己写的是「只带本轮真的出现过的人」（`giftia/brain.py:316`），`_filter_by_presence()` 存在的理由也是「专治把 A 的事说给 B 听」（`giftia/brain.py:287-292`）
- 严重度：**P1**（机制与自己声称的相反；群聊里会说起不在场的人，正是 `_filter_by_presence` 要防的事）；置信度：**高**（跑出来的）
- 现象与影响：群 G 里只有「成员3」说话，提示词里却带了另外 14 个人的印象（每人最多 3 条）；
  真人模型完全可能顺口提到不在场的人，等于把“别人不在场时也能被谈论”。内容上是群内公开信息（不是私聊泄漏），
  但它绕过了同一份提示词里专门设的在场闸门，也让“检索路径过滤、印象路径不过滤”自相矛盾。
- 触发条件：任何群消息（`impressions_block()` 在 `_prepare()` 里无条件注入）。
- 证据（`python .review\probes\bc_prompt.py`）：
  ```
  == 2. 【你对他们的印象】是否只带在场的人 ==
    [FAIL] 2a ★ 印象块只带在场的人（文档称'只带本轮真的出现过的人'）  -> 不在场却出现：['成员0', '成员1', '成员2', '成员4', '成员5']（共 14 人）
        印象块行数：15
        印象块前 3 行：['你在心里给每个人留了一份印象（这不是台词，是你的底子）：', '- 成员3：…', '- 成员0：…']
  ```
- 建议修法：删掉第二个兜底循环（只保留在场的人 + speaker/handler），把“不会因为这次没问到就把人忘干净”交给检索路径；
  若确实想全带，就把 docstring 和 README 改成“按人封顶、不按在场过滤”，别让两条注入路径的规则相反。

### [P1] 群 A 会拿到只在群 B 出现过的人（含搭档在别群的昵称/备注），而且花名册没有上限

- 位置：`giftia/brain.py:417-442`（`_people_for_prompt()` 读全局 `known_people()` / `all_notes()`，没有按群过滤）
  → `giftia/persona.py:152-155,205-220`（`scene in ("group","guest")` 时整份花名册注入，**没有条数上限**）
- 严重度：**P1**（跨群身份信息泄漏 + 无界增长进提示词 = 成本失控）；置信度：**高**（跑出来的）
- 现象与影响：
  1. 只在群 B 自我介绍过的「阿伟」，会在群 A 的提示词里被写成“你在群里认得这些人”，她可能在 A 群里叫一个不在场的人；
  2. 认识的人越多，每次群请求的提示词越长：**301 人 → 花名册 301 行 / 2463 字**（README 只承诺印象块“每人最多 3 条、最多 15 人”硬上限，花名册没有）；
     大群里几千人时，这部分会稳定吃掉上下文（同一份 system 每轮都发）。
- 触发条件：任何群消息；`known_people` 随群成员自我介绍/平台昵称持续增长。
- 证据：
  ```
  # python .review\probes\bc_prompt.py
  == 3. 【群里你认得的人】花名册的群隔离 ==
    [FAIL] 3a ★ 群 A 的花名册里没有只在群 B 出现过的人
        【群里你认得的人】 | 你在群里认得这些人，别叫错： | - 阿伟 | - 椎名（你的搭档） | 其他人你还不认识…

  # python .review\probes\bc_growth.py
  == 1. 花名册（known_people）无上限地进群提示词 ==
        认识 301 人 → 提示词里花名册 301 行 / 2463 字
    [PASS] 1a ★ 花名册没有条数上限，全部进提示词
        整条 system 长度：4847 字
  ```
- 建议修法：`known_people` 的 value 里记“在哪个群见过的”（或另存 `uid → {group_id: name}`），`_people_for_prompt(group_id)` 只取本群 + 搭档；
  同时给花名册加硬上限（比如最多 30 人，按最近活跃排序）。

---

## P2

### [P2] 群 id 里含 LIKE 元字符（`_` / `%`）时，“往上翻聊天记录”会翻到**别的群**，自己的工作记忆反而为空

- 位置：`giftia/store.py:382-394`（`messages_by_prefix`：`safe = prefix.replace("%","").replace("_","")` —— 把元字符**删掉**而不是转义）
  ＋ `giftia/brain.py:459-465`（`history_prefix = f"i{instance_no}-{self.scope_key}-"`）＋ `giftia/brain.py:1045-1080`（【群里刚才在聊什么】用的就是它）
- 严重度：**P2**（跨群历史串味/静默失效；记忆检索本身是参数化精确匹配，不受影响）；置信度：**高**（核心可复现）；
  **线上可达性：QQ 的 openid 是十六进制，实际打不到**——但核心自称“平台无关”，任何含 `_`/`%` 的 group_id 都会中招。
- 现象与影响：群 `G_1` 的会话前缀是 `i1-gG_1-`，被清洗成 `i1-gG1-`，于是 LIKE 命中的是群 `G1` 的记录：
  她读别人群的聊天记录当上下文，而本群的历史一条都看不到（`GROUP_MESSAGE_CREATE` 的 memory 归属是对的，只有“翻历史”这条路串）。
- 触发条件：`group_id` 含 `_` 或 `%`；适配器传入的 `group_openid` 一般不含，但核心 API 层没有约束。
- 证据（`python .review\probes\bc_branches.py` 场景 I）：
  ```
  == 场景 I：群 id 里含 LIKE 元字符（_ / %）时的'往上翻聊天记录' ==
        messages_by_prefix('i1-gG_1-') → '这是G1群的内容-爬山 ……「这是G1群的内容-爬山」……我记下了。我在。'
    [FAIL] I1 ★ 群 'G_1' 翻到了群 'G1' 的记录（LIKE 元字符被删掉）
  ```
- 建议修法：不要清洗前缀，改成 `session_id LIKE ? ESCAPE '\'` 并把 `\`/`%`/`_` 转义；或者干脆用 `substr(session_id,1,?)=?` 精确前缀比较。

### [P2] 群场景忘传 `group_id` 时，会静默回落到“搭档私聊”视图，私事直接进群回复

- 位置：`giftia/brain.py:527-538`（`if self.last_scene in ("group","guest") and group_id:` —— `group_id` 为空就掉到 `elif self.is_handler:` 分支，`memory_view` 变成 `private + person + group*`）
- 严重度：**P2**（防御缺失；核心 API 是对外文档化入口）；置信度：**高**（核心可复现）
  **线上可达性：QQ 适配器 `adapters/qq_official.py:701` 总是传 `group_id`（且 `target` 为空时在 538 行就 return 了），所以生产路径打不到。**
- 证据（`python .review\probes\bc_branches.py` 场景 E）：
  ```
    [FAIL] E1 ★ 群场景缺 group_id 时回落到搭档私聊视图（私事进群）
    [PASS] E2 此时 scope_key='m'
  ```
- 建议修法：`scene in ("group","guest")` 而 `group_id` 为空时，不要回落到搭档视图：要么直接拒答/记一条告警，
  要么退化成“无记忆的访客”视图（`memory_view=[]`）。`handle_message()` 的 docstring 现在只是“**必须传**”，没有兜底。

### [P2] `people_stats`（活跃成员表）只写不读，而且每条群消息全量重写、永不裁剪

- 位置：`giftia/brain.py:186-209`（`people_stats()` / `note_activity()`），调用点 `adapters/qq_official.py:570`（每条群消息一次）
- 严重度：**P2**（死数据 + 每条消息 O(n) 读写 + `meta` 单行无界增长）；置信度：**高**（跑出来的 + 全仓引用图）
- 现象与影响：`docs/BACKLOG.md:35` 宣称这张表让“她说话的分寸不一样”，但**没有任何读取方**（`giftia/` 里只有 brain.py 自己引用），
  每次群消息都要读 51KB JSON、解析、+1、再整体写回；1000 人时单次 3.2ms，且只增不减。
- 证据（`python .review\probes\bc_growth.py`）：
  ```
  == 2. people_stats：每条群消息都全量重写，而且没人读 ==
        1000 人的 people_stats 序列化大小：51000 字
        单次 note_activity（1000 人表）耗时：3.203 ms
    [PASS] 2b ★ people_stats 只在 brain.py 的定义/读写里出现，别的模块（含 persona.py）从不读它
        引用 people_stats 的文件：{'giftia\\brain.py'}
  ```
- 建议修法：要么把它接进提示词（那就要有上限和按群过滤），要么删掉写入；保留的话至少把计数改成单独的行式存储、并限制只保留最近 N 人。

### [P2] `consolidate()` 没有事务边界：中途写库失败会留下“一半记忆 + 消息仍未整理”

- 位置：`giftia/memory.py:589-650`（每条 `add_memory` 各自提交，`mark_consolidated` 在最后；失败时前面的记忆已落库、游标没推进）
- 严重度：**P2**（半写状态；靠相似度去重兜底，所以实际后果有限）；置信度：**高**（跑出来的）
- 现象与影响：`add_memory` 第 2 条失败时，第 1 条记忆已经永久落库、消息仍是“未整理”，异常还会一路抛到 `handle()`（见 P1）。
  好消息：重试时相似度去重（`dedupe_ratio`）挡住了重复条目，实测没有产生重复记忆。
- 证据（`python .review\probes\bc_fault.py`）：
  ```
  == 7. 记忆整理中途 DB 失败：会不会留下半写状态 ==
        add_memory 调用次数=2，consolidate 抛出：OperationalError: database is locked
        已写进记忆：['阿伟要去北京出差半个月']；仍处于未整理的消息：2 -> 2
    [PASS] 7a 半写：第一条记忆已经落库，但消息没标已整理（没有事务）
    [PASS] 7b 重试后靠去重没有产生重复条目
  ```
- 建议修法：`consolidate` 里把“写记忆 + 标消息”放进一个 `with store.conn:` 事务（`add_memory` 不要自己 commit），
  失败就整体回滚，下一轮重新整理。

### [P2] 访客（客人）路径其实**会**检索群记忆、也**会**写记忆——README 的「零记忆访问」「不写记忆」字面不成立

- 位置：`giftia/brain.py:572-577`（非 handler 也照常 `_chat` → `_prepare` → 群视图检索；返回后 `self.consolidate()`）
  vs 宣称 `README.md:407`「**不检索你的记忆、不写记忆、不推进关系**」、`README.md:409`「访客模式…**零记忆访问**」、
  `giftia/persona.py:102`（`guest：群里的其他人，无记忆无关系`）
- 严重度：**P2**（宣称与实现不一致；**隐私实质是对的**——她碰到的是 `group` 归属、不是搭档私事）；置信度：**高**（跑出来的）
- 现象与影响：客人说的话会被整理成**群归属**记忆（`scope=group`）永久留下，客人那一轮也会检索并注入本群公开记忆。
  所以“客人碰不到你们的隐私”成立，但“零记忆访问/不写记忆”不成立；写下来的内容将来会在群里被检索到。
- 证据（`python .review\probes\bc_branches.py` 场景 C/J）：
  ```
    [PASS] C2 客人拿得到本群公开记忆（说明'完全不检索'不成立）
  == 场景 J：客人那一轮到底写不写记忆（README 写「不写记忆」）==
        客人两轮之后她的记忆：[('group', 'GW', '丙说过：我是客人，第0次说话，聊的是')]
    [PASS] J1 ★ 客人说的话会被整理成记忆（README 的『不写记忆』字面不成立）
    [PASS] J2 但归属正确：全部是 group 归属，没有 private
  ```
- 建议修法：改文档（把“零记忆访问”改成“只能看到本群公开发生过的事；客人说的话会记进这个群的公开记忆”），
  或者按字面实现（客人那一轮不 consolidate、检索只给 `[]`）——两者选一个，别让文档和代码各说一套。

### [P2] `scene="guest"` 是死分支：适配器的“客人”实际传 `scene="group"`，于是 `agent_tools.available()` 里那条判据永远不生效

- 位置：`giftia/agent_tools.py:121-123`（`if getattr(hub, "last_scene", "") == "guest": return False, ...`）
  vs `adapters/qq_official.py:676-679`（客人走的是 `scene="group", is_handler=False`）；`giftia/persona.py:143-146` 的 `guest_text` 同样没被用到
- 严重度：**P2**（代码与文档/README“访客模式”不一致，判据冗余）；置信度：**高**（全仓 grep + 探针）
- 现象与影响：真正拦住客人工具的只有 `is_handler=False`（有效，实测客人那轮没有工具），所以**行为是对的**，
  但 `guest` 这条判据、`SCENE_LINES`/`guest_text` 分支是死代码，读起来会以为有两条独立防线。
- 证据：
  ```
  # python .review\probes\bc_branches.py（场景 C/H）
    [PASS] C6 客人那一轮没有给工具
    [PASS] H1 显式 guest 也拿不到私聊记忆 / H2 guest 走的是群视图 / H3 guest 没给工具
  # grep：adapters/ 与 giftia/ 里没有任何 scene="guest" 的调用点（只有探针在用）
  ```
- 建议修法：要么适配器客人分支改传 `scene="guest"`（那 `persona.py` 的 guest 文案和工具判据才生效），
  要么删掉 `guest` 这套分支、统一成 `group + is_handler=False` 并在文档里写清楚。

---

## P3

### [P3] `forget` 没有每轮上限（写有 2 条上限，删没有）

- 位置：`giftia/brain.py:743-752`（只有 `remember` 计入 `MAX_WRITES_PER_TURN`）＋ `giftia/agent_tools.py:229-244`
- 证据（`python .review\probes\bc_tools.py` 第 5 节）：一次 `execute` 连删 6 条都成功（`['放下 #1 了。', … '放下 #6 了。']`）。
- 影响：模型一轮里可以连续丢弃多条记忆（只能丢它此刻看得见的，所以不是越权，但一次对话丢 6 条不可逆状态变化偏大）。
- 修法：把 `forget` 也计进 `MAX_WRITES_PER_TURN`，或单独给一个更小的上限（比如 1）。

### [P3] `__init__` 里 `backfill_message_scopes()` 失败被 `except Exception: pass` 静默吞掉

- 位置：`giftia/brain.py:97-100`
- 影响：这是一次**隐私归位迁移**（老数据的 scope 默认值会让群里的话被当成“你的私事”）；失败时没有任何提示，
  而且不会重试。建议失败时往 `notices`/`store.add_diary` 记一条。

### [P3] 空回复会被写成一条空的 assistant 记录

- 位置：`giftia/brain.py:821-832`；`giftia/brain.py:986`（`if delivered == self.last_reply_text` 时 `"" == ""` 被当成“完整送达”）
- 证据（`python .review\probes\bc_fault.py` 5b）：`模型返回空串时仍会写一条空的 assistant 记录` → `[('user','在吗'), ('assistant','')]`。
- 影响：随包 provider（`DeepSeekLLM.chat` 对空内容抛 `LLMError`）打不到，但接口层没有禁止空串；
  一旦为空，`note_reply_delivery("")` 会因为“相等”而保留这条空记录（她“以为自己说了句空话”）。
- 修法：`_chat` 里 `if not reply.strip(): reply = FAILURE_LINES[...]`。

### [P3] 文档数字/结构与代码对不上（三处）

- `README.md:81`「每积累 **8** 条消息…」vs 出厂 `config/settings.toml:33` `consolidate_every = 6`（dataclass 默认值才是 8）。
- `README.md:73-76` 说 system「拼成六块：人格内核 → 出戏提示 → 此刻状态 → 关于记忆 → 想起的事 → 规则」，
  实际顺序是：人格内核 → 出戏 → 称呼 → 关系 → 心情 → 大日子 → 梦 → 情境 → 花名册 → 说话人 → 印象 → **工具协议** → 群聊天记录 → **此刻状态** → 关于记忆 → 想起的事 → 规则
  （实测 15 个 `【】` 块，其中人格内核自己含 5 个子块；**状态块在很靠后的位置——这对缓存是好事**，只是文档写反了）。
  证据：`bc_prompt.py` 打印的块顺序 + `system 以 persona.core_text() 原样开头`。
- `giftia/trigger.py:36-43` 注释「45 → 36%、70 → 71%」：`speak_probability(70)=0.714` 没错，但 **70 档（tier 4）在 `decide()` 里从不看概率**，
  这个数字在真实决策里没有意义；45 档附近才用得到它（实测 0.3571）。

---

## 本轮验证通过（打脸未成立的怀疑）

这些是任务书点名要打脸的机制，**实测是对的**，写下来避免后面重复怀疑：

| 宣称 | 实测 | 证据 |
|---|---|---|
| 群里检索不到搭档的私聊记忆 | 成立（被 @ / 客人 / 旁听三条路都拿不到） | `bc_branches.py` B1/C1/H1 |
| 访客不推进关系、不写搭档记忆、不占她的寿命 | 成立（bond/interactions 不变，落库 scope=group；但“零记忆访问/不写记忆”的字面宣称不成立，见 P2） | `bc_branches.py` C3/C5/J1-J2 |
| 旁听消息的 `scope/subject_id` 正确 | 成立（`("group", 群id)`，且不产生 assistant 回复） | `bc_branches.py` F1-F5 |
| 跨群/跨人工作记忆不串味（正常 id） | 成立（群 A 无群 B 内容；P2 看不到 P1 会话） | `bc_branches.py` G1-G3 |
| 聚批：前面几句旁听落库、只有最后一句触发回复 | 成立（2 user + 1 assistant，前几句进上下文） | `bc_flow.py` 5a-5d |
| 转发消息（102 / `msg_elements`）能取出正文（含嵌套） | 成立，且 tier=0 时只旁听、落群归属 | `bc_flow.py` 4a-4e |
| 陌生人私聊在已绑定时被挡（不调模型） | 成立（回 DEFLECT） | `bc_flow.py` 3c/3d |
| `tools_max_rounds` 真封顶、最后一轮真拿掉工具 | 成立（3 轮 = 2 次带工具 + 1 次 nudge 无工具；rounds=5 → 4+1；rounds=1 → 直接 chat） | `bc_tools.py` 1a-1e/2a-2c |
| 一轮最多写 2 条记忆 | 成立（4 次 `remember` 只落 2 条，超限的没执行） | `bc_tools.py` 3a/3b |
| 工具错误路径不崩、不认的人名把可用值喂回去 | 成立（未知工具/坏 JSON/非对象/空 content/坏 memory_id 都回人话） | `bc_tools.py` 4 |
| 模型失败不抛异常、用 FAILURE_LINES 兜底 | 成立（并且 `note_reply_delivery("")` 会把那条回复从记忆里撤回；部分送达按实际内容留存） | `bc_fault.py` 1a-1f/2a-2b |
| `runs.jsonl` 只记数字不记对话内容 | 成立（29 字含秘密的消息，档案里查不到秘密） | `bc_fault.py` 6a/6b |
| `trigger.tier_of` 边界 0/19/20/44/45/69/70/100 | 全部与 README 表一致；越界被夹住 | `bc_trigger.py` 1 |
| `speak_max_per_day`（自己插嘴日上限） | 成立：tier=100 时前 3 条会开口、第 4 条被挡；被 @ 不受此限 | `bc_trigger.py` 5a-5d |
| 工作记忆裁剪不会裁掉 system / 最后一条用户消息 | 成立（`messages[0]` 恒为 system、末条恒为当前输入） | `bc_prompt.py` 4a-4d |
| 人格内核永不变、在提示词最前面（前缀缓存） | 成立：两轮 system 公共前缀 1523/2163 字节，第一处差异就是【你此刻的状态】里的分钟 | `bc_prompt.py` 1a-1c |
| `handle()` 里没有裸 `except` 吞错误 | 成立（`handle`/`_chat` 没有宽泛 except；宽泛的都在 `__init__`/`_log_run`/`close`，见 P3） | `grep -n "except" giftia/brain.py` |

---

## 未验证的怀疑（没有跑出证据，别当结论用）

1. **`raw_message` 回填带 `reasoning_content` 可能被 API 拒**：`giftia/llm.py:377` 把 API 原样的 message 塞进 `ChatReply.raw_message`，
   `giftia/brain.py:740-742` 又原样回填进 `messages`。开思考模式时（`ModelConfig.thinking`）DeepSeek 的响应里带 `reasoning_content`，
   多轮工具对话回填它是否会被 400 拒绝，我没有真 key、不能验（协议要求不调真 API）。建议真机验证一次工具轮 + thinking=true。
2. **一轮工具轮里模型既给文本又给 tool_calls 时，那段文本被丢弃**：`giftia/brain.py:737-742` 只回填 `raw_message`、不看 `reply.text`。
   对最终回复没影响（最后一轮会说），但如果模型在中间轮已经说出了关键内容，用户看不到。没有构造出真实反例。
3. **`session_id` 用 `uid[:12]` / `group_id[:12]` 截断会不会撞车**：`giftia/brain.py:536/528`。QQ openid 是 32 位十六进制，
   前 12 位相同的概率极低；但核心是平台无关的，别的平台（自增数字 id、带前缀的 id）不一定。没构造出反例，仅提出。
4. **群聊里搭档自己说话会推进亲密度/心情**（`giftia/brain.py:791-792`：`is_handler` 为真就 `_tick_inner`，不管场景是不是群）。
   与 README「别人说话不会推进关系」不冲突（别人确实不推），但“在群里 @ 她也算每天第一次认真说话”是否符合设计意图，
   需要产品确认；我没有判定这是 bug。
5. **`impressions_block` 里 `about` 为空的记忆不受在场闸门约束**（`giftia/brain.py:304`：`""` 永远在 `present` 里）。
   会写字模型的 prompt 明确要求填 `about`（`giftia/llm.py:80,90`），但离线兜底和工具写的记忆可以没有 about。
   影响面取决于真模型的 about 填充率，未测。
6. **`_window` 裁剪后历史可能以 assistant 开头**（`bc_prompt.py` 4 节实测 `历史角色序列：a`）。
   DeepSeek 接口对首条非 system 消息的角色是否有硬性要求，我没有真 key，未验；目前只当观察记录。
