# Stream D 审查报告：行为引擎（mood / bond / occasions / dream / outreach / recycle / cli / run）

审查者：behavior-checker（task-4）
代码版本：`D:\ai chatbot`（2026-10-10 工作区快照，未修改任何产品代码）
探针：`.review/probes/beh_common.py` + `beh_outreach.py` / `beh_mood.py` / `beh_bond.py` /
`beh_occasions.py` / `beh_dream.py` / `beh_recycle.py` / `beh_cli.py`
统一运行方式：

```powershell
$env:PYTHONIOENCODING="utf-8"
python .review\run_tests.py .review/probes/beh_outreach.py
```

每个 P0/P1 都贴了探针真实输出；没跑出证据的一律放在文末"未验证的怀疑"。

**读探针输出的约定**：`[FAIL]` = 该条"应有行为"被证伪（复现了问题，或与 README 承诺不符），
`[PASS]` = 该条断言成立。有些 `[PASS]` 恰恰是在确认"bug 确实存在"（例如
`[PASS] D2 target=both 时群里也发得出去` 是 FAIL，而 `[PASS] F1 发送失败不扣额度` 是这次审查里少见的
正面结论）。各探针最后的 `══ name：PASS=x FAIL=y ══` 里，FAIL 数 = 被证伪的"应有行为"条数，
不是脚本自身出错（脚本本身都能跑完）。

---

## 0. 结论先行：selftest 第 24 节 3 项失败，是测试过期还是行为偏离？

基线（我自己的复跑）：`python .review\run_tests.py tools/selftest.py` → **通过 299 项，失败 3 项**，
3 项全在第 24 节，与 lead 给的一致（输出见下）。

| 测试 | 判定 | 根因 |
|---|---|---|
| `默认关闭时她不会主动找你`（selftest:791） | **测试过期** | 测试假设"出厂默认关闭"，但 `config/settings.toml:193` 写着 `enabled = true`（代码默认 `False`，被配置覆盖）。`enabled` 开关本身没有失守。 |
| `刚说过话时没有主动理由`（selftest:797） | **两者都有**（测试过期 + 行为偏离） | 实现新增了 `mood_share`（`outreach.py:181-199`），刚说完话心情就非 calm，于是立刻有理由。详细理由表（README:321-331）里有 `mood_share`，但 README:150-158 的理由表没有——**README 自己两份表打架**，测试跟着旧表写。 |
| `闲了 4 天 → 她想你了`（selftest:801） | **行为偏离**（有实质问题，见 P1-6） | 心情的 `until` 过期后 `current()` 照样返回旧心情（`mood.py:87-100` 不看 `_expired`），`mood_share` 又排在 `missing_you` 前面，于是"闲 4 天"永远先被"分享一个 4 天前的旧心情"截走。 |

> 关于 lead 探针里看到的 `near_end`：那只是因为寿命被压短了。`near_end` 的条件是
> `剩余寿命 <= mood.uneasy_hours(48h)`；selftest 第 24 节用的是 `make_settings(tmp/"out", 81920.0)`，
> 剩余寿命远大于 48h，所以返回的是 `mood_share`（selftest 自己打印的 detail 也是 `mood_share`）——
> 我的探针 A3 与它同构，输出一致。

**给 lead 的更正**：你看到的"`enabled=False` 却仍返回计划"没有发生在产品代码里——那是构造探针时的经典陷阱：
用 `dataclasses.replace(settings, outreach=...)` 造了新设置，却仍在**旧设置构造出来的 brain** 上调用
`outreach_plan()`。用新设置构造 brain 后开关是好的（探针 A1b）：

```
config/settings.toml → [outreach] enabled=True idle_days=2 max_per_30_days=60 max_per_day=3 min_gap_hours=3 target='both'
config.py 代码默认 [outreach] enabled=False（被 settings.toml 覆盖）
[FAIL] A1 复现《默认关闭时她不会主动找你》(selftest:791)  → 实际 outreach_plan() -> mood_share
enabled=False 的新 brain（同一个库） + force=False → None（应为 None）
enabled=False + force=True  → mood_share（/outreach 预览用的就是这条）
[PASS] A1b enabled 开关本身没有失守：force=False 时返回 None，force=True 才给预览
```

**另一个必须知道的点**：这 3 项的绿/红还跟**当天日期**绑定。掷骰种子是 `crc32("日期|实例号")`
（`mood.py:180`），calm 的日子（出厂权重 0.5）三项全绿，非 calm 的日子红 3 项。今天 2026-10-10 实例 #1
掷出 `low`，所以是红的：

```
2026-10-10 起 40 天：calm 20 天 / 非 calm 20 天
今天(2026-10-10) 实例#1 掷骰 = 'low'（= 今天的 selftest 为何是红的）
非 calm 的日子样例：['2026-10-10:low', '2026-10-12:tired', '2026-10-13:low', '2026-10-15:low', '2026-10-16:happy']
[PASS] A5 同一份 selftest 在 calm 的日子会全绿、非 calm 的日子红 3 项（契约分叉 + 日期相关）
```

也就是说：**它不是一个稳定的回归信号**，明天可能自己变绿。

---

## P0

### [P0] 群发提示词里混进了别人的私聊大日子（隐私闸门只在"计划层"生效，"写作层"漏了）
- 位置：`giftia/brain.py:939-946`（`Giftia.compose_outreach` 里的第二次 `find_occasions`，没传 `view`）
  + `giftia/occasions.py:110`（`store.active_memories(instance_no, view=view)`，`view=None` 时**不过滤**）
  + `giftia/store.py:600-623`（`_view_clause`：`None` = 不过滤）
- 严重度：P0（隐私越界）；置信度：**高**（提示词层面已实测泄漏；最终群消息是否说出口取决于模型，不能保证不发生）
- 现象与影响：`target=group`（或 `both` 的群那条）会用 `scene="group"` 组装 system 提示词，但里面
  `【今天不只是普通的一天】` 来自**不带 view** 的查询 → 搭档私聊里说的事、**其他人在私聊里说的事**都被
  渲染进这条"要发到群里"的提示词。`occasions.py:71-73` 的注释明确说这不允许
  （"否则你私聊里说的『下周三考试』会变成她在群里惦记的事，等于当众说出来"）。同样的剧本在 `dream`
  上被专门堵过（`brain.py:666-669`，注释"真机测试抓到过这条泄漏"），occasions 这条漏了。
- 触发条件：`[outreach] target = "group"`（出厂配置是 `both`），或适配器 `_outreach_targets()` 走到群那条；
  只要存在"别人的私聊大日子"记忆即可。
- 证据（`beh_occasions.py` D 段，提示词原样 dump）：

```
find_occasions(view=[('group','GROUP-1')]) → ['认识第 5 天（跨过 3 天）', '明天：阿伟明天要考驾照']
outreach_plan(for_group=True) → big_day / 明天：阿伟明天要考驾照          ← 计划层是对的
compose_outreach(target='group') 的 system 提示词里出现：['小雨', '搭档明天要去面试', '小刚']
    —— 提示词片段 ——
    今天不只是普通的一天】
    今天对你来说不只是普通的一天：
    - 他明天要办这件事：搭档明天要去面试你会惦记着它。可以问一句，但**别追问……
    - 他明天要办这件事：小雨明天要去医院做手术你会惦记着它。可以问一句，但**别追问……
    - 他明天要办这件事：阿伟明天要考驾照你会惦记着它。可以问一句，但**别追问……
    - 他明天要办这件事：陌生人小刚后天要搬家你会惦记着它。
[PASS] D4 对照：正常群聊路径没有漏（说明漏点在 compose_outreach 的第二次 find_occasions）  → []
```

  （对照：同一个 brain 走正常群聊 `handle(..., scene="group")` → `_prepare` 用 `self.memory_view`，
  提示词里 `[]` 命中，说明闸门本身是对的。）
- 建议修法：把 `brain.py:903-911` 那段的 `view` 复用过来（`for_group` → `[("group", self.last_group_id or "")]`，
  否则 `[("private",""),("person",handler_id),("group","*")]`），或直接在 `find_occasions` 里把 `view=None`
  视为"空视角"而不是"不过滤"。

---

## P1

### [P1] `/time nan|inf|1e24` 把非有限值写进虚拟时钟并当场崩进程；重启后连 banner 都过不去
- 位置：`giftia/cli.py:236-261`（`_handle_time` 只 try 了 `float()`）+ `giftia/clock.py:45-53`
  （`advance()` **先 `_persist()` 再 `now()`**）
- 严重度：P1（状态损坏 + 每次启动即崩，只能手工改库恢复）；置信度：高
- 现象与影响：`float("nan")` / `float("inf")` 都能解析通过，`advance()` 先把 `nan`/`inf` 写进
  `meta.virtual_offset_hours`，然后 `datetime.now() + timedelta(hours=nan)` 抛 `ValueError`。
  异常从 `_handle_time` 冒到 `_handle_command`（无 try）再冒到 `run_cli`（**指令路径没有兜底层**），
  进程直接带 traceback 退出。**更糟的是库里留下 `nan`**：下次启动 `Clock.__init__` 用 `float("nan")`
  读回，`banner()`/`status()`/`handle()` 只要碰 `clock.now()` 就抛，`/time reset` 根本敲不进去。
- 触发条件：交互里输入 `/time nan`（或 `/time inf`、`/time 1e400`、`/time 1e24`）。
- 证据（`beh_cli.py` B 段）：

```
/time nan    → ValueError: cannot convert float NaN to integer；落库的 offset='nan'
/time inf    → OverflowError: cannot convert float infinity to integer；落库的 offset='inf'
/time 1e24   → OverflowError: Python int too large to convert to C int；落库的 offset='999999999999999983222784.000000'
/time -inf   → OverflowError: cannot convert float infinity to integer；落库的 offset='-inf'

/time nan 之后：offset='nan'，异常=ValueError: cannot convert float NaN to integer
重启后 run_cli(banner=True) → ValueError: cannot convert float NaN to integer
[PASS] B2 写坏之后重启即崩（连 /time reset 都打不进去，时钟每调 now() 都抛）
```
- 建议修法：`_handle_time` 里先 `math.isfinite(hours)` 且限幅（如 |hours| <= 100 年）再调用；
  顺带把 `Clock.advance` 改成"先算出 `now()` 成功再持久化"。

### [P1] `/forget <超大整数>` 未捕获 OverflowError，直接终止整个 CLI 主循环
- 位置：`giftia/cli.py:162-168`（`int(arg)` 只做了 `isdigit()` 检查）+ `giftia/cli.py:79-88`
  （**指令路径没有 try/except，聊天路径有**）+ `giftia/store.py:657-…`（`forget_memory` 把 int 直接绑进 SQLite）
- 严重度：P1（真实输入即崩溃；CLI 的注释自己写着"CLI 不该因为一次异常整个崩掉"，但只兜了聊天路径）；置信度：高
- 现象与影响：`/forget 99999999999999999999`（20 位数字，`isdigit()` 为真）→
  `OverflowError: Python int too large to convert to SQLite INTEGER` → 从 `_handle_command` 冒到 `run_cli`
  外层，整个进程退出。适配器也暴露了同一条路（`adapters/qq_official.py:796-799` 同样 `int(parts[1])`
  后调 `forget_memory`），在那里会被 `QQBot.run` 的 `except Exception`（:988）当成"断线"处理
  → 每次尝试都触发一次重连退避，那条消息的回复永远发不出去。
- 触发条件：`/forget 99999999999999999999`。
- 证据（`beh_cli.py` A/C 段）：

```
[CRASH] '/forget 99999999999999999999' → OverflowError: Python int too large to convert to SQLite INTEGER
[PASS] C3 聊天路径的异常被 run_cli 兜住（不会退出，后面的指令还接着跑）
[PASS] C4 指令路径没有 try/except，任何指令异常都会终止整个 CLI  → RuntimeError
```
- 建议修法：`_handle_command` 外面套一层 try/except（和聊天路径一样打印 `[内部错误]` 后 `continue`）；
  `/forget` 再加个上限校验（如 `int(arg) <= 2**31`）。

### [P1] `target=both` 只发得出去私聊那条：群消息被自己的间隔限制挡掉并静默丢弃，且只扣 1 条额度
- 位置：`adapters/qq_official.py:823-869`（`_outreach_targets` + `_maybe_outreach` 的 for 循环：
  每次迭代先 `can_send` 再发再 `note_outreach_sent`）+ `giftia/outreach.py:94-109`（`can_send` 的 min_gap 判定）
- 严重度：P1（功能与 README 承诺相反 + 一条该发到群里的消息静默丢失 + 额度记账与文档不符）；置信度：高
- 现象与影响：`both` 承诺"两边都发（**每次占 2 条额度**）"（README:163-169，出厂 `target="both"`）。
  实际第一轮发完私聊后立刻 `record_sent(now)`，第二轮 `can_send` 里 `now < max(recent)+min_gap_hours`
  必然成立 → `return`，群那条直接没了；额度也只扣 1 条。
- 触发条件：`[outreach] target = "both"`（出厂值）+ 有 owner + 进过群 + 有可发理由。
- 证据（`beh_outreach.py` D 段，真适配器 `QQBot._maybe_outreach()`，只把 `_request` 换成 stub）：

```
adapter._outreach_targets() = [('c2c', 'OWNER-1'), ('group', 'GROUP-1')]
私聊可用理由=big_day　群里可用理由=big_day
实际发出的请求：['/v2/users/OWNER-1/messages']
实际扣额度：used=1 left=59（README：both 每次占 2 条）
[FAIL] D2 target=both 时群里也发得出去（README/配置承诺『两边都发』）  → 只发出 1 条：['/v2/users/OWNER-1/messages']；而群里明明有可发理由 big_day（阿伟明天要考驾照）
[FAIL] D3 target=both 每次占 2 条额度（README 承诺）  → 实际 used=1
[PASS] D4 群里那条被丢掉的原因：第一条发完立刻记录，第二次 can_send 被 min_gap 挡回  → min_gap_hours=3，next_ok=2026-10-15T11:23:12
```
- 建议修法：`_maybe_outreach` 里把 `can_send` 判定挪到循环外（只判一次），或对同一批多目标用
  "本次批次豁免 min_gap"的记账方式（一次批次算 2 条、只检查一次间隔）；`both` 的额度语义要么真的扣 2，要么改 README。

### [P1] 讲梦的适配器路径不标记"已讲"，同一个梦 3 小时后会再讲一遍，每次照样扣额度
- 位置：`giftia/brain.py:1012-1030`（`note_outreach_sent` 只写 `mood_shared:*`，**没有 `mark_dream_told`**）
  vs `giftia/brain.py:968-969`（`compose_outreach(record=True)` 里才会 `mark_dream_told`）
  + `giftia/dream.py:107-125`（`latest_untold` 只看 `told` 和 `tell_within_hours`）
- 严重度：P1（重复发送 = 消息重复 + 稀缺额度被同一件事重复消耗）；置信度：高
- 现象与影响：适配器走的是 `compose_outreach(record=False, target=...)` + 发送成功后 `note_outreach_sent`
  （`qq_official.py:850,867`）。这条路径不会把梦标成已讲，于是只要还在 `tell_within_hours=36h` 内、
  又过了 `min_gap_hours=3`，下一次轮询的理由**又是同一个梦**（`max_per_day=3` → 一天最多重讲 3 次）。
  用户视角：她一天里把同一场梦讲了三次，额度也被吃掉三条。
- 触发条件：有未讲的新梦（`dream.enabled=true`、记忆 ≥3 条）+ 真实发送成功（适配器路径）。
- 证据（`beh_dream.py` D 段）：

```
outreach_plan → reason=dream / 她想讲一个梦
（适配器路径）实发内容：……「（他没有找你。你想说点什么。）」……我记下了。我在。…
发完之后 dreams.told = [0]
同一天再过 4 小时 outreach_plan → reason=dream / 她想讲一个梦
第二次实发内容：……嗯。「（他没有找你。你想说点什么。）」……我记下了。你今天，还好吗？…（和第一次不同）
同一个梦 #1 还在，latest_untold=有，told=[0]
[PASS] D2 【重复】同一天里她把同一个梦又讲了一遍（适配器路径不标记 told）
（record=True 路径）发完之后 dreams.told = [1]
[PASS] D3 record=True 路径会标记 told（两条路径语义不一致）
```
- 建议修法：把 `mark_dream_told` 的逻辑挪到 `note_outreach_sent`（或让 `note_outreach_sent` 复用
  `last_outreach_plan` 里记下的 dream id），两条路径共用一个"已讲"记账点。

### [P1] bond 的"不可刷"承诺被内核记忆加成打破：同一天 3.47 → 32.90，`interactions` 却仍是 1
- 位置：`giftia/bond.py:162-176`（`on_memory` 每条记忆都给一次加成）+ `giftia/brain.py:1147-1175`
  （`_on_draft` 每条新记忆回调一次，仅私聊搭档路径）+ `giftia/memory.py:621-645`（每次整理写一条就回调一条）
- 严重度：P1（核心机制与文档承诺相反；README 的 11/34/80/180 天曲线只对"每天只说一句"成立）；置信度：高
- 现象与影响：`bond.py:5-7` 写着"**不可刷**。亲密度按『天』增长——同一天说一百句也只算一次。
  奖励的是『你来了』，不是『你说了多少』"，README:241-242 也按这句话给出天数曲线。但 `gain_core_memory=2.0`
  / `gain_important_memory=1.0` 是**按条**给的：一天里聊得越多 → 整理出的记忆越多 → 亲密度涨得越快。
  实测同一天 20 句（mock 每次整理产出一条内核记忆）把亲密度从 3.47 推到 32.90，而按天计数的
  `interactions` 仍然是 1。这意味着"每天都见面"的曲线可以被"一天猛聊"大幅压缩。
- 触发条件：私聊搭档、`consolidate_every` 条消息触发一次整理，且模型给出的 layer 为 core/important。
- 证据（`beh_bond.py` D 段；D 段用 `CoreMemoryLLM` 模拟"每次整理都记下一件关于他的新事"）：

```
第 1 句之后：亲密度 3.4700，记忆 1 条
同一天再说 19 句 → 亲密度 32.8986（interactions=1），记忆 19 条，她一共整理了 20 次
按 README「同一天说一百句也只算一次」应为 3.4700；实际多出来 29.4286（每条内核记忆 gain_core_memory=2.0 递减后计入）
[PASS] D1 同一天内亲密度可以靠「多聊 → 多整理出内核记忆」继续涨（不是只算一次）
interactions 仍是 1（按天计数），但亲密度已经不是「一天一次」的曲线
```
- 建议修法：把记忆加成也按天封顶（同一天最多给一次，或给一个每日上限），或把 `on_memory` 的加成改成
  只影响当天的 `gain_daily` 系数；README 也要同步说明记忆加成的存在。

### [P1] `mood_share` 抢在 `missing_you` 前 + 心情永不过期 → 每天一条"我今天心情X"，把稀缺额度烧在心情上
- 位置：`giftia/outreach.py:181-199`（`mood_share` 排第 4，`missing_you` 在 :228-244 排最后）
  + `giftia/mood.py:87-100`（`current()` 完全不看 `until` / `_expired`）
  + `giftia/brain.py:919`（`outreach_plan` 直接把 `current_mood()` 交给计划层）
- 严重度：P1（功能与文档承诺相反 + 额度被低价值理由消耗）；置信度：高
- 现象与影响：三个互相放大的问题——
  1. 心情只在她说话时才会被 `tick` 更新（`brain.py:588-600`），没人说话时旧心情**永远留在 state 里**，
     `until` 过期也没人清；`current()`/`status`/`banner`/提示词/主动计划全都读它。
  2. `mood_share` 的去重键是 `mood_shared:{日期}`（`brain.py:1026-1029`），**每天一条**，
     所以"同一个 5 天前的旧心情"可以被连续分享 5 天（探针 F1）。
  3. `mood_share` 优先级高于 `missing_you`/`milestone`/`dream`，于是真正稀缺的"太久没见"排不上队。
  按 README:160 的"默认 4 条 / 30 天"额度，4 天就被心情分享吃光，之后连"想你了"都发不出去；
  按出厂配置 `max_per_30_days=60`，30 天里有 30 条是心情分享（一半）——而 README:160 把额度描述成
  "稀缺资源，用完就没有"。
- 触发条件：出厂配置（`outreach.enabled=true`）+ 上一轮对话留下的心情不是 calm（出厂权重下约 50% 的日子）。
- 证据（`beh_outreach.py` A/B/C 段 + `beh_mood.py` F 段）：

```
闲 4 天之后 心情 key='low' label=有点低落 source=daily since=2026-10-10T08:22:45 until=2026-10-10T18:22:45 now=2026-10-14T08:22:45 已过期=True
[FAIL] A3 复现《闲了 4 天 → 她想你了》(selftest:801)  → 实际 reason=mood_share（剩余寿命 81920h，不会是 near_end）
[PASS] A4 心情=calm 时，闲 4 天确实返回 missing_you（缺的只是「心情不是 calm」）

（max_per_30_days=4，即 README 的"默认 4 条/30 天"）
    第 1 天 idle=1天 reason=mood_share  can_send=True  真发=True  已用=1/4
    第 4 天 idle=4天 reason=mood_share  can_send=True  真发=True  已用=4/4
    第 5 天 idle=5天 reason=mood_share  can_send=False 真发=False 已用=4/4
    再闲 15 天（共 23 天没说话）后：outreach_plan -> reason=mood_share can_send=False used=4 left=0
[PASS] C2 额度被心情分享花光后，即使真的「太久没见」也一条都发不出去

（出厂配置 max_per_30_days=60、日上限 3）
30 天里理由序列 = {'mood_share': 30}，真发出 30 条，used=29 left=31
[PASS] C3 出厂配置下心情分享会吃掉一半的 30 天额度（30 条全是 mood_share）

（beh_mood.py F 段）
连续 5 天，outreach_plan().reason = ['mood_share', 'mood_share', 'mood_share', 'mood_share', 'mood_share']
期间心情一直是 tired（until=2026-10-10T18:23:48，now=2026-10-15T08:23:48），从没被清过
```
- 建议修法（最小改动，三选一或组合）：
  1. `outreach_plan` 里先判 `mood.is_expired(now)`（或让 `MoodTracker.current` 在过期时返回 calm）；
  2. `mood_share` 的条件加"最近 N 小时内有真实对话"；
  3. 把 `mood_share` 排到 `missing_you`/`milestone` 之后，或让它走另一份额度（不占 30 天 4 条）。
  README 两份理由表（:150-158 与 :321-331）也要统一。

### [P1] `use_wakeup=true` 时本地额度没有收紧到官方"30 天 4 条"，会超发
- 位置：`giftia/outreach.py:60-109`（额度只看 `settings.outreach.max_per_30_days`）
  + `adapters/qq_official.py:855-856`（`use_wakeup` 只用来往 body 里塞 `is_wakeup`）
  + `config/settings.toml:195-200`（`max_per_30_days = 60`，注释"真正管用的是下面的日上限"）
- 严重度：P1（额度/合规失控：本地账本允许远超平台上限的互动召回）；置信度：高（机制层面已实测；
  平台侧具体惩罚未验证）
- 现象与影响：README:182 说"`use_wakeup = true` 则改用官方「互动召回消息」（更合规，**额度更紧**）"，
  README:439-441 明确写官方是"30 天内 4 个周期各 1 条"。代码里 `use_wakeup` 完全不影响额度计算，
  出厂 `max_per_30_days=60` 照旧放行 → 本地以为还能发，实际早已超出平台配额（超发部分大概率发送失败
  或被风控）。
- 触发条件：`[outreach] use_wakeup = true`（出厂 `false`，但 README 推荐用它换合规）。
- 证据（`beh_outreach.py` D 段末尾）：

```
use_wakeup=True max_per_30_days=60 → 30 天内已发 10 条，还剩 50，can_send=True
[PASS] D5 use_wakeup=true 时官方 4 条/30 天的额度没有被本地账本约束（会超发）  → 已发 10 条仍 can_send=True
```
- 建议修法：`use_wakeup=True` 时把有效额度取 `min(max_per_30_days, 4)`（或单独一个
  `wakeup_max_per_30_days`），并在 `history_text` 里显示真实生效值。

### [P1] 回收崩溃窗口：封存之后换代之前挂掉，重跑会把 `封存/instance-01.json` 覆盖成 0 条（原始档案丢失）
- 位置：`giftia/recycle.py:164-179`（写 `instance-{n:02d}.json` → `seal_instance` → `add_diary` →
  `lifespan.reactivate`，**没有任何"回收进行中"标记，也没有幂等保护**）
  + `giftia/lifespan.py:90-98`（换代才 +1）
- 严重度：P1（数据丢失；崩溃窗口小但真实，且恢复路径必然二次回收）；置信度：高（故障注入实测）
- 现象与影响：`perform_recycle` 的顺序是"写文件 → 封存记忆 → 写公文 → 换代"。若在 `seal_instance`
  之后、`reactivate` 之前进程被 kill（或 `reactivate` 抛错），库里记忆已 `sealed=1`（检索不到了）、
  实例号却没变。重启后寿命仍然到期 → 下一条消息再次触发回收 → 这次 `active_memories` 已经是 0 条，
  于是 `封存/instance-01.json` 被**同名覆盖**成空档案：**原始记忆的唯一存档没了**
  （同名文件里没有时间戳；`.md` 回忆录名字精确到秒，同秒重跑时也会一起被覆盖）。
- 触发条件：回收过程中进程被杀/异常（磁盘、OOM、systemd 重启），然后用户再说一句话；
  或同一秒内重复触发回收。
- 证据（`beh_recycle.py` C 段，故障注入 = 让探针进程里的 `lifespan.reactivate` 抛错）：

```
第一次回收在 reactivate 处抛错：进程在这里被 kill -9（模拟崩溃）
崩溃后：实例号仍是 1（没换代）、active_memories=0 条、封存文件 instance-01.json 里有 5 条记忆
[PASS] C1 崩溃点：记忆已被封存（检索不到），但实例没有换代
第二次回收：实例 2，封存文件 ['instance-01.json']，其中 memories=0 条
第一次封存的 5 条原始记忆现在还剩 0 条（同名文件被覆盖）
[PASS] C2 【数据丢失】重跑回收把 instance-01.json 覆盖成 0 条记忆，原始封存档案没了  → 5 → 0
回收日志文件：['回收-01-20261010-082734.md']（同秒重跑 → 文件名相同）
剩下的那份回忆录里还有原来的 5 条记忆吗：False
[PASS] C3 同一秒内重跑连回忆录 .md 也被覆盖（文件名只精确到秒）
隔 1.2 秒重试：回忆录 2 份（1 份保留着原始记忆），instance-01.json 里 memories=0 条
[PASS] C4 隔秒重试时 .md 保住了，但 instance-01.json 仍被覆盖成空档案  → md=2/1 json=0
```
  （对照：正常连续回收两次是安全的，各自一个实例号、各自一个文件 → `['instance-01.json','instance-02.json']`。）
- 建议修法：① `sealed_path` 加时间戳（或先写 `.tmp` 再 `os.replace`，并且**已存在就改名保留**）；
  ② 回收开始先写 `meta:recycling_instance=n` 标记，重启时若标记存在就"续跑换代"而不是重新回收；
  ③ `seal_instance` 与 `reactivate` 尽量贴近，或把 `active_memories` 为空的情况显式跳过文案生成。

---

## P2

### [P2] `tone_of` 的否定句/子串问题：`不开心` = 0 分（她不会担心），`我不累` = -1 分（她会担心）
- 位置：`giftia/mood.py:27-54`（`POSITIVE` 含 `开心`，`NEGATIVE` 含 `不开心`/`累`；计数是"子串是否出现"而非出现次数）
- 严重度：P2（核心判据在常见口语上给出相反结论；同时影响心情与 bond 的情绪加成）；置信度：高
- 证据（`beh_mood.py` D 段）：

```
tone_of('我不累') = -1.00   （他在说自己不累）
tone_of('不开心') = +0.00   （明显的负面情绪）
tone_of('别担心，我不累') = -1.00
tone_of('我升职失败了') = +0.00
tone_of('今天不忙，挺轻松的') = +1.00
阈值 tone_threshold=0.4：『不开心』= 0.0 达不到 → 她不会担心；『我不累』= -1.0 → 她会担心
```
- 建议修法：命中 `negative` 词前先算"否定前缀"（不/没/别），或把 `不开心` 改为优先匹配、把 `开心` 换成
  更长的词（如 `很开心`/`真开心`）；`累` 这类单字词加边界词（`好累`/`太累`/`累死`）。

### [P2] 回收换代不清 `handler_name` / `known_people`：她说"初次见面、档案上还是空的"，提示词里却已经有你的名字
- 位置：`giftia/brain.py:1237-1241`（只 `bond.reset` + `mood.reset`）；`giftia/recycle.py:179`（换代）
- 严重度：P2（与 README:8/:244"新的她不认识你"、`persona.toml` 的 `after_recycle_line`"档案上还是空的"矛盾）；置信度：高
- 证据（`beh_recycle.py` B/E 段）：

```
handler_name 状态 = '小林'（换代后没有清）
新实例提示词里的称呼块：【你怎么称呼他】 / 他叫「小林」。你就这么叫他。 / / 【你和他的关系】 / 你们现在的关
[PASS] B4 【与「新的她不认识你」冲突】换代后她仍知道你的名字  → 提示词里出现了旧称呼
跨实例共享的状态键：handler_name='小林' known_people='{"GUEST-7": "阿伟"}'
[PASS] E3 新一代的花名册/群记录还在（阿伟仍认得）
```
- 建议修法：若"新的她不认识你"是硬承诺 → `run_recycle` 里一并清 `handler_name`（至少清"名字"这个事实），
  或把开场白改成"……又是你啊"；花名册要不要留可以单独配置。

### [P2] 大日子的去重键把"明天"和"今天"当成同一条：前一天提过之后，正日子当天不会再提
- 位置：`giftia/occasions.py:117-137`（token = `f"{id}:{when}"`，只含事件日期，不含"今天是哪一天"）
- 严重度：P2（`big_day_lookahead_days:1` 的注释说"今天 + 明天的大日子都会提醒"）；置信度：高
- 证据（`beh_occasions.py` B 段）：

```
前一天 mark=True → ['明天：明天要考驾照']；第二天（正日子）→ （不提醒）
[PASS] B2 前一天提过之后，正日子当天不再提（去重键 = id+日期，把「明天」和「今天」当成同一条）
```
- 建议修法：token 里带上"提醒发生在哪一天"（`f"{id}:{when}:{today}"`），或对 delta==0 单独给一次机会。

### [P2] 过期心情污染所有读路径：状态栏、提示词、主动计划都在用 `until` 已过的心情
- 位置：`giftia/mood.py:87-100`（`current()` 不判过期）、`giftia/mood.py:15`（docstring"过期就回到平静"
  与实现不符：过期后是重新掷骰，而且同一天会掷回同一个 key）
- 严重度：P2（状态机语义与文档不符；是 P1-6 的根因之一）；置信度：高
- 证据（`beh_mood.py` C 段）：

```
T0  now=2026-10-10T08:23:48 心情=low until=2026-10-10T18:23:48
T0+11h now=2026-10-10T19:23:48 current()=low until=2026-10-10T18:23:48 （until 已过 11 小时）
过期后 tick(中性) → low（source=daily）
再快进 30 天（期间没人说话）→ current() 仍=low，until=2026-10-11T05:23:38，now=2026-11-09T19:23:38
```
- 建议修法：`current()` 增加"过期 → 返回 calm（或按日期重掷）"；docstring 改成"过期后按日期重掷一次"。

### [P2] 文档与配置三处互相打架（额度/间隔/理由表），用户无法判断真实行为
- 位置：`README.md:150-158`（5 条理由表，无 `mood_share`/`follow_up`）、`README.md:160`（"默认 4 条 / 30 天、
  间隔至少 6 小时"）、`README.md:317-319`（`min_gap_hours=3`、`idle_days=2`）、
  `config/settings.toml:193-197`（`enabled=true`、`max_per_30_days=60`、`min_gap_hours=3`、`idle_days=2`）、
  `giftia/config.py:144-157`（代码默认 `enabled=False`、`max_per_30_days=4`、`min_gap_hours=6`、`idle_days=3`）
- 严重度：P2（文档承诺与实测不一致；selftest 的三项失败就源于此）；置信度：高
- 证据：`beh_outreach.py` A 段首行输出 + README 行号（上面已列）。
- 建议修法：README §3.5 的 5 条理由表并到 §"五个旋钮"的 7 条表；额度/间隔按 `settings.toml` 实际值写；
  selftest 改成显式设置 `enabled=False` 而不是假设默认值。

### [P2] selftest 第 24 节的 3 项断言与实现/README 分叉，并且结果随日期在红绿之间摇摆
- 位置：`tools/selftest.py:788-802`
- 严重度：P2（测试资产本身不可信：永远红或随机红，掩盖真实回归）；置信度：高
- 证据：本报告第 0 节（299/3 + 日期相关性输出）。
- 建议修法：测试里显式 `dataclasses.replace(..., outreach=enabled False/True)`，理由断言里加入
  `mood_share` 分支（或先把心情设成 calm 再断言 `missing_you`）。

---

## P3

### [P3] README 的天数曲线第 4 档对不上：实测是第 181 个日历日
- 位置：`README.md:241-242` vs `giftia/config.py:106-112`（`irreplaceable.min_days = 180`）
- 证据（`beh_bond.py` C4，中性语气、无记忆加成、每天一句）：

```
第   1 个日历日（认识   0.00 天） → stranger       亲密度=  1.50
第  11 个日历日（认识  10.00 天） → partner        亲密度= 15.32
第  34 个日历日（认识  33.00 天） → familiar       亲密度= 40.18
第  80 个日历日（认识  79.00 天） → close          亲密度= 70.15
第 181 个日历日（认识 180.00 天） → irreplaceable  亲密度= 93.51
[FAIL] C4 README 宣称的第 11/34/80/180 天曲线可复现  → {'partner': 11, 'familiar': 34, 'close': 80, 'irreplaceable': 181}
```
  前三个数字与 README 完全一致（激活日算第 1 天）；第 4 档受 `min_days=180` 约束，
  `days_known` 恰好到 180 时已经是第 181 个日历日。不是逻辑错误，是口径/文案差一天。

### [P3] `dream.fragments = 0` 会拿全部记忆做梦（`memories[-0:]`）
- 位置：`giftia/dream.py:80`（`memories[-settings.dream.fragments :]`）
- 证据（`beh_dream.py` E 段）：`fragments=0 → 梦的 source_ids 条数 = 8（记忆共 8 条）`
- 建议：`fragments = max(1, int(...))` 或单独判 0。

### [P3] `should_dream` 用全量视角计数、`make_dream` 用搭档视角取材，两者口径不一致
- 位置：`giftia/dream.py:46`（`active_memories(instance_no)`，view=None = 全部人）vs `dream.py:76`（view=搭档）
- 影响：别人私聊攒了 3 条就会让 `should_dream` 判真，然后 `make_dream` 因搭档记忆不足返回 None —— 空跑一次判断，
  不产生错误行为（`_maybe_dream` 也不会有 notice）。建议两处用同一个 view。

### [P3] `--reset` 不带 `--yes` 在非交互环境直接 traceback；`--lifespan-hours` 负数被静默忽略
- 位置：`run.py:59-70`（裸 `input()`）、`run.py:40-46` + `config.py:53-60`（`override > 0` 才生效）
- 证据（`beh_cli.py` D 段）：

```
--reset（无 --yes）在非交互 shell 里 → EOFError: 
--lifespan-hours -5 → effective_hours=500.0（负数被静默忽略，退回配置值 500.0）
```
- 建议：捕获 `EOFError` 当作取消；负数给出 argparse 校验或提示。

### [P3] `_parse_event_date` 只认 ISO，非 ISO 的日期静默作废
- 位置：`giftia/occasions.py:52-56`
- 证据（`beh_occasions.py` E 段）：`_parse_event_date('10月20号') = None`、`('下周三') = None`、
  `('2026/10/11') = None`；`('20260711' 这种基本格式反而能过)`。
- 说明：正常路径有 `llm.py:533-538` 的 `date.fromisoformat` 兜底（模型填错就置空），所以影响有限；
  但任何非 ISO 的历史/手工数据会永远不触发。建议 `_parse_event_date` 容忍几种常见写法并记一条 notice。

### [P3] README 宣称的自测项数与实际不符
- `README.md:23` 写 "218 项端到端自测"，实测 `python .review\run_tests.py tools/selftest.py` 是
  **通过 299 项，失败 3 项**（共 302 项）。属 QA stream 的核对范围，这里只记一行。

---

## 已验证为"没问题"的点（避免 lead 重复劳动）

- **mood 确定性**：`_roll` 用 `zlib.crc32("日期|实例号")`（不是 `hash()`），同一天同一实例多次掷骰一致；
  两个独立进程各跑一次都得 `low`；不同实例号结果不同；实测 2000 天频率与 `[mood.weights]` 吻合（±0.05）。
  权重全 0 兜底 calm，负权重被 `max(0,·)` 吃掉。（`beh_mood.py` A/E 段）
- **三股力量优先级**：剩余 10h + "太好了！我升职了！哈哈哈！" → 仍是 `uneasy/lifespan`；负面 → `worried`；
  正面 → `happy`；中性且未过期 → 保持原心情。（B 段）
- **bond 按天结算**：同一天 100 句 = 1 句（1.5000）；访客私聊 50 句 + 群聊 50 句完全刷不动；
  `diminish` 实际涨幅与 `名义 × (1 - 当前/100)` 逐位一致（10.0 → 9.0 → 8.1）；阶段判定确实要两条腿
  （亲密度 100/0.5 天 → `stranger`；亲密度 0/500 天 → `stranger`）；回收后 `affinity=0`、`interactions=0`、
  旧实例 state 键被清空。（`beh_bond.py` A/B/C/E 段）
- **occasions 边界**：`days_known<1` 不庆祝第 1 天；`mark=True` 只提一次；跨多个里程碑只报最高；
  按实例分键；`lookahead=1` 时 +2/+3 天不提醒、-1 天走 `follow_up`；`follow_up_days=2` 时 3 天前不再问；
  **显式传 view 时群/私聊闸门是对的**（隐私泄漏只在 `compose_outreach` 那条路上）。（`beh_occasions.py`）
- **dream**：记忆不足不做梦；`min_hours_between` 生效；碎片确实被模糊且**可复现**；
  梦不写进 `memories`（不占条数、不进检索）；`tell_within_hours` 过期后不再翻出来讲。（`beh_dream.py` A/B/C）
- **outreach 记账**：`record_sent` 的 `keep=400` 默认值下，30 天用量能正确统计到 400；
  **发送失败不扣额度**（适配器 `except QQError: continue` → 不调 `note_outreach_sent`）；
  `enabled=False` + `force=False` 确实返回 None。（`beh_outreach.py` A1b/E1/F）
- **recycle 正常路径**：回忆录 → 封存（5 条原样带走）→ 实例 +1 → diary 公文；
  旧记忆对新一代完全不可见但原始行仍在；连续回收两次各自安全。（`beh_recycle.py` A/B/D）
- **cli**：`/time abc`、`/forget abc|-1|0|999`、`/mood 不存在的名字`、`/name`（空/超长 100 字 → 截断 16 字）、
  `/impress` 参数不足、`/dream now|1|NOW`、未知指令、空行、EOF、Ctrl-C 都正常；
  聊天路径的异常被 `run_cli:84-88` 兜住且后续指令继续执行。
- **run.py**：`--once` 正常返回 0；模型故障时给降级回复（退出码 0）；`--reset --yes` 在隔离目录里确实清空
  （真实 `data/giftia.db` 的 mtime 未变）。

## 未验证的怀疑

1. **"下周三/10 月 20 号"的日期推算**依赖模型自己算：`llm.py:107-110` 的提示词只给 `{today}` 的日期
   （`YYYY-MM-DD`），**没有给星期几**。模型要自己推 `下周三` 的星期，离线 mock 无法验证真实准确率；
   上线后建议抽真机样本统计。
2. **P0 隐私泄漏最终会不会被说出口**：我证明的是"别人的私聊内容进入了群发提示词"，是否真的变成群消息
   取决于模型对 `group_hint` 的遵守程度（无 key，未做真机验证）。
3. **适配器端 `/forget <超大整数>` 的后果**：我只验证了 CLI 会崩；适配器里异常会被 `QQBot.run:988`
   的 `except Exception` 当成断线（触发重连退避），**具体是否会丢掉那条消息/是否会反复重连**未实测，
   留给 Stream C 确认。
4. **`outreach:log` 跨实例共享**：换代后 30 天额度不重置（`LOG_KEY` 不带实例号）。看起来是合理的
   （额度是"她的说话次数"，不是"某一任的"），但没有文档说明，未验证设计意图。
5. **`mood_shared:{日期}` 也是跨实例的**（`brain.py:1026`）：同一天换代后，如果新一任掷出的心情 key
   与旧的那条相同，她当天就不会分享心情。影响很小，未单独构造用例。
6. **`decay` 与 `relationship.enabled` 的关系**：`bond.decay()` 不检查 `enabled`（只有 `touch` 检查），
   即"关掉关系系统"时衰减仍会在下次说话时生效（`decay_per_day` 默认 0，所以默认无影响）。未构造用例。
7. **`occasions.told` 集合只增不减**（`occasions.py:137/169`）：每条记忆×每个事件日期一个 token，
   量级受记忆条数约束，未观察到无界增长；长期（数年）是否值得清理未验证。
