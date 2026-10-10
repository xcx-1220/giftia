# team-recycle 修复报告（第 1 波：回收仪式与纪念日归属）

- 负责人：`team-recycle`
- 拥有并修改的文件：`giftia/recycle.py`、`giftia/occasions.py`、`giftia/dream.py`
- 探针期望更新：`.review/probes/lead_v1_p0_verify.py`（A 段）、
  `.review/probes/beh_recycle.py`（B/C 段 + 新契约）、`.review/probes/beh_occasions.py`（B2/E/F 段）
- 契约依据：`.review/FIX_BATCH1_INTERFACE.md §一`（`view` 必填；`view=None` → `TypeError`；
  `giftia.store.ALL_SCOPES = "all"` 表示显式全量）

---

## 1. 修了什么

### P0-1 回收仪式只收"搭档自己的事"（`giftia/recycle.py`）

| 产物 | 修复前 | 修复后 |
|---|---|---|
| 回忆录 `回收日志/回收-NN-<stamp>.md` | `active_memories(instance_no)` 全量 → 含 `person`/`group` | 搭档视角 `[("private",""),("person",handler_id)]`，**不含 group** |
| `diary(kind="terminal")`（`/memoir` 读的） | 同一段全量正文 | 同一段搭档视角正文 |
| 喂给 `llm.write_memoir()` 的 context | 全量条目 | 搭档视角条目 |
| 封存 `封存/instance-NN-<stamp>.json` | 全量（无标记） | **仍是全量原始数据**，新增 `"scope": "all_scopes"`、`"note"`、`"memory_count"` |

- 新增 `_handler_view(store)`：`handler_id` 从 `store.state_get("handler_id")` 取，取不到就只用 `[("private","")]`。
- `perform_recycle(..., view=None)` 新增可选参数：不传 = 搭档视角；传列表 = 白名单视角；
  传 `ALL_SCOPES` = 显式全量（诊断/迁移用）。**封存 JSON 与这个参数无关，恒定全量。**
- `RecycleReport` 新增 `sealed_count` / `sealed_scope` / `resumed` 字段，`notification()` 明确区分：
  「回忆录记忆 N 条（只看「你和她」的视角）」「原始数据 …（M 条，含全部归属的运维存档）」。
- 回忆录正文末尾也写明：全量原始数据在封存档案里（`all_scopes`）。

### P1-11 `/days` 的大日子清单按归属过滤（`giftia/occasions.py`）

- `days_together_text(..., *, view=None)`：`view` 是**可选关键字参数**（调用点 `brain.days_text()`
  不用改），不传时按搭档视角过滤；顺带把"每条解析两次日期"的列表推导改成海象运算符、只解析一次。
- `find_occasions(...)` 的 `view=None` 也改成默认搭档视角（契约要求"不要再有默认不过滤"；
  传 `None` 给 `active_memories` 会直接 `TypeError`）。调用点 `brain.outreach_plan` 一直显式传 view，不受影响。

### P1-20 回收崩溃窗口不再覆盖原始档案（`giftia/recycle.py`）

三条措施全部落地：

1. **绝不覆盖**：封存档案名改为 `封存/instance-NN-<YYYYmmdd-HHMMSS>.json`，
   写入前过 `_unique_path()`（同名就退让成 `…-2.json`）；回忆录 `.md` 同样走 `_unique_path()`。
2. **崩溃续跑换代**：写档案前写 `meta:recycling_instance=<JSON>`（含实例号、封存文件名、
   回忆录路径、条数、事由、回忆录正文、起始时间）。`perform_recycle` 开头先看这个标记：
   - 标记存在 + 封存档案真的还在 + 实例号一致 → **只补 `reactivate` 换代**（必要时补写公文），
     不重新生成任何档案；成功后清标记。
   - 标记存在但档案不在（崩在写档案之前）/ 实例号已经变过（陈旧残留）→ 清标记，走正常流程。
3. **空档案不再覆盖原件**：`active_memories` 为空 + 实例已封存时不会走进续跑分支去生成空档案——
   续跑路径完全不写文件。连续回收两次（每一任各自一个实例号）仍然各自一份档案（`beh_recycle.py` D1）。

### P2（我文件里的三条）

- `occasions._parse_event_date(raw, today=None)`：容忍 `2026/10/11`、`2026.10.11`、`2026年10月11日`、
  `10月20号`（缺年份 → 按 `today` 补，过了算明年）、`下周三/这周三/周三/周六`（相对 `today`）、
  紧凑 `20261011`；真正非法的（`2026-13-01`、`2026-2-30`、空、`None`）仍返回 `None`、**不抛错**。
  `find_occasions` 与 `days_together_text` 都把 `today` 传进去，相对写法才有基准。
- 去重 token 带上"提醒发生在哪一天"：
  `big_day` → `f"{id}:{when}:{today}"`、`follow_up` → `f"back:{id}:{when}:{today}"`。
  于是"前一天提过『明天考试』"之后，**正日子当天还会再提一次**；"事后第 1 天问过"之后，
  第 2 天还能再回访一次（`follow_up_days=2` 的窗口内）。
- `dream.should_dream(..., view=None)` 与 `make_dream` 共用 `_handler_view(store)`：
  资格判定与取材**同一口径**（修掉 memory-man P1-3 的"口径不一致 → 梦静默做不出来"；
  别人的私聊不再能把 `min_memories` 凑够）。`fragments=0` 时 `max(1, ...)`，不再 `memories[-0:]` 取全量。
  `latest_untold` 语义未改（"已讲"标记仍由 brain 侧 `mark_dream_told` 负责，属 team-behavior）。

---

## 2. 探针期望为什么改了（逐条理由）

| 探针 / 断言 | 原期望 | 新期望 | 理由 |
|---|---|---|---|
| `lead_v1_p0_verify.py` A2「封存 JSON 不含小雨的私聊」 | PASS = 不含 | PASS = `scope=all_scopes` + `note` + **小雨的记录仍在** | 修复设计**刻意**让封存保持全量（运维资产 = 前任记忆的唯一存档），过滤掉等于把前任的记忆真删了。所以这条不能再用"不含 person"当标准，改成分层期望：**回忆录/diary 必须干净，封存必须显式标注自己是全量且一条不少**。这不是放宽，是把判据换成与设计一致的那一条。 |
| `beh_recycle.py` B 段 | `active_memories(old, view=None)` | `view=ALL_SCOPES` | 接口契约 §一：`None` 抛 `TypeError`，全量必须显式写。语义等价、写法必须更新。 |
| `beh_recycle.py` C2「重跑把 5 条覆盖成 0 条」 | PASS = 缺陷存在（5 → 0） | PASS = **不变量**：原始 5 条一条没少 | 原来这几条是把 P1-20 的 bug 写成断言；bug 修掉后必须改写成"无论重跑几次都不许丢"。新增 C1b（全量标记）/C2b（不产生空档案）/C3b（标记已清）。 |
| `beh_recycle.py` C3/C4「.md 被覆盖 / `instance-01.json` 仍被覆盖成空档案」 | PASS = 被覆盖 | PASS = 原件都还在 | 同上：这两条描述的正是修复要消灭的行为。C4 原来还隐含"隔秒重试会多出一份 .md"，现在续跑根本不重复写回忆录，所以文件数是 1 而不是 2。 |
| `beh_occasions.py` B2「前一天提过 → 正日子当天不再提」 | PASS = 当天不提 | PASS = 当天**仍会**提一次（措辞从"明天"变"今天"） | 与任务书 P2 直接冲突：qa-verifier 与 behavior-checker 都报"前一天提过之后正日子当天反而一句话都没有"。去重键带 `today` 就是为修这条。 |
| `beh_occasions.py` E1「`10月20号`/`下周三`/`2026-10-11` 静默作废」 | PASS = 解析不出来 | PASS = 能解析出正确日期；非法串仍 `None` | 任务书 P2 明确要求容忍这些常见写法（模型/整理链路写进 `event_date` 的不一定严格 ISO）。新增 E1b 保证"认不出来仍静默作废、不报错"这条老承诺没丢。 |

> 没有改任何 `tools/` 下的期望值；上面六条都属于探针（`.review/probes/`），且每条都在文件里
> 写了"期望为何变了"的说明。

---

## 3. 真实输出

### 3.1 `python .review\run_tests.py .review/probes/lead_v1_p0_verify.py`

```
P0-1 回收仪式是否过滤归属（release 前必须确认）
库里有 3 条不同归属的记忆：private / person(小雨) / group(群A)
  【分层期望：为什么第 1/3 条与第 2 条的标准不同】
  …
  [PASS] 回忆录 .md 不含小雨的私聊（搭档视角）
  [PASS] 封存 JSON 是全量运维档案：scope=all_scopes + note + 小雨的记录仍在（**分层期望，非放宽**）
  [PASS] /memoir（diary 表）不含小雨的私聊（搭档视角）

  回忆录路径：…\回收日志\回收-01-20261010-105834.md
  回忆录条数（搭档视角）=1 条；封存条数（全量）=3 条；封存 scope='all_scopes'
  封存 note：含全部归属的原始记忆（private / person / group），属于运维资产，不是给搭档或任何第三方看的东西；…
  终端服务公文里有没有把“全量”说清楚：True
  回忆录泄漏行：无
```

修复前同一段是 `[FAIL] ×3`，泄漏行 `　　2026-10-10T08:55：SECRET-BRAVO-4402 小雨私聊说她跟家里吵架了。`
（P0-2 段随后仍 1 FAIL → 归 team-store，我未动。）

### 3.2 `python .review\run_tests.py .review/probes/beh_recycle.py`

```
══ beh_recycle：PASS=18 FAIL=0 ══
（修复前 15/0 —— 5 条是 A/B，C 段 4 条当时"PASS"表示缺陷存在）
C2 【P1-20 已修】重跑不再把 5 条覆盖成 0 条：原始封存档案里的 5 条一条没少  → 保留 5/5，目录合计 5 条
C2b 续跑换代不再产生新的空档案（同一实例只写一份封存 JSON）  → ['instance-01-20261010-085950.json']
C4 隔秒重试：原始回忆录与原始封存档案都保住了（没有空档案覆盖原件）  → md=1/1 json_total=5 实例=2
D1 连续回收是安全的（各自一个实例号、各自一个文件）  → 3 / ['instance-01-…5952.json', 'instance-02-…5952.json']
```

### 3.3 `python .review\run_tests.py .review/probes/beh_occasions.py`

```
══ beh_occasions：PASS=17 FAIL=0 ══
D3 【隐私】群发提示词里不得出现别人的私聊/搭档私事的大日子  → 命中 []
E1 常见写法能解析成正确的日期（『2026/10/11』『10月20号』『下周三』）  → 2026-10-11/2026-10-20/2026-10-21
E1b 非法/缺失的日期仍然静默作废（返回 None，不抛错）  → None/None
F1 前一天提过之后，正日子当天还会再提一次（去重键 = id+日子+今天）  → ['明天：明天要考驾照'] / ['今天：明天要考驾照']
F2 事后第 1 天问过之后，第 2 天还能再回访一次  → ['昨天那件事：昨天考驾照'] / ['2 天前那件事：昨天考驾照']
```

> D3 原来是"记录 bug"：群发提示词里出现别人的私聊/搭档私事就算 PASS。
> team-brain 修完 P0-4（`brain.compose_outreach` 的第二次 `find_occasions` 补上 view）后
> 这条路径不再泄漏（实测命中 `[]`），所以 D3 的期望翻转为
> **「群发提示词里不得出现任何非本群归属的大日子」**，D1 的群视角正向对照仍然保留。

### 3.4 `python .review\run_tests.py tools/selftest.py` —— 迁移完成后已可跑

```
════════════════════════════════════════════════════
通过 299 项，失败 3 项
════════════════════════════════════════════════════
  [FAIL] 你明显低落 → 她在担心你  → low/daily
  [FAIL] 默认关闭时她不会主动找你
  [FAIL] 刚说过话时没有主动理由
```

**与修复前基线（302 项 = 299 过 / 3 败）逐项一致**，总数没降、失败项没变多。
这 3 项都属于 `mood.py` / `outreach.py` 的既有失败（P1-1 / P1-15 / P1-16 那一批，
owner 是 team-behavior，lead 标为第 2 波），与 `recycle.py` / `occasions.py` / `dream.py` 无关：
我为 P2 改的日期解析与去重键在 selftest 里没有对应断言，`dream` 的口径一致性由 3.5 第 2 段覆盖。

> 迁移期背景：`view` 改必填后，`tools/selftest.py` 等旧签名调用点一度让脚本在第一条断言就
> `TypeError`。迁移完成（lead/team-brain 已补）后恢复可跑，数字见上。


### 3.5 补充验证（临时脚本，`.review/tmp/recycle_scratch_verify.py`，非交付）

`PASS=12 FAIL=0`。三段真实输出：

```
1. /days（days_together_text，不传 view）按归属过滤  ← P1-11
    她记得的「大日子」：
      · 2026-10-11　搭档明天要去面试
  [PASS] P1-11 搭档视角的大日子在（private）
  [PASS] P1-11 别人的私聊不在（person）
  [PASS] P1-11 其它群的事不在（group）

2. dream：should_dream 与 make_dream 同一视角  ← memory-man P1-3
  [PASS] 搭档自己 0 条记忆时 should_dream 必须为 False  → 全量=5 条 / 搭档视角=0 条 / min_memories=3
  [PASS] make_dream 与 should_dream 同结果（都做不出来）
  [PASS] 搭档自己攒够 min_memories 后两者一致地放行  → should_dream=True make_dream=ok
  [PASS] P2 fragments=0 时不拿全部记忆（取材应为 1 条，不是 4 条）  → source_ids=1

3. 回收：回忆录/diary 干净 + 封存 JSON 全量带标记  ← P0-1
  [PASS] 回忆录不含别人的私聊 / 群里的事
  [PASS] diary（/memoir）不含别人的私聊 / 群里的事
  [PASS] 封存 JSON 仍是全量原始数据（person + group 都在）  → 3 条
  [PASS] 封存 JSON 显式标注 scope/note
  [PASS] 终端服务公文说明了封存是全量
```

### 3.6 迁移期的一个插曲（已解除）

`view` 改必填之后、全组迁移完成之前，`tools/selftest.py` 一度在第一条断言就
`TypeError: Store.active_memories() missing 1 required positional argument: 'view'`。
该迁移面（`tools/selftest.py` 约 40 处、`tools/live_check.py`、`tools/reconcile_live.py`、
`giftia/brain.py`、各团队旧探针）**不在我的授权文件范围内**，我没有改。
lead / team-brain 补完后 selftest 恢复可跑，数字见 3.4（299/3 = 基线）。


---

## 4. 未完成 / 风险 / 交接

| 项 | 状态 | 说明 |
|---|---|---|
| `tools/selftest.py` 等旧签名调用点 | 已解除 | 迁移期一度 `TypeError`（见 3.6）；lead/team-brain 补完后一跑即过：**299 项 / 3 败 = 修复前基线** |
| `Store.ALL_SCOPES` 类属性 | 已解决 | team-store 已补；实测 `Store.ALL_SCOPES == "all"`，两种写法都可用 |
| `brain.compose_outreach` 群发提示词混入搭档私事 | 已由 team-brain 修复 | 我发去的证据（D3 命中 `['搭档明天要去面试']`）已复验：现在命中 `[]`，D3 期望已翻转为"不得泄漏" |
| `.review/probes/beh_dream.py` 旧签名 | 属 team-behavior | 该探针第 69 行 `active_memories(1)` 需迁移到新契约；我对 dream.py 的一致性验证走 3.5 第 2 段 |
| `dream` 的"已讲"判定 | 未动（按任务书） | `latest_untold` 语义保持不变，`mark_dream_told` 由 brain（team-behavior）负责 |
| 群发 target=both / 其它模块 | 未动 | 属 team-adapter / team-behavior |
| P1-21（单聊同一 msg_id 5 条 > 4） | 已报 lead，未修 | 见第 6 节；owner 是 lead（`adapters/qq_official.py`） |
| P1-22 `send()` 吞 QQError 的取舍 | 已报 lead，待决策 | 见第 6 节 |

### 风险登记（我认为值得 lead 知道的两点）

1. **`handler_id` 为空时私事桶是"无主"的**：三家（brain/dream/recycle/occasions）都把
   `("private","")` 当搭档的桶。契约 §二 用"绑定时刻遮蔽"解决换人越权，但**未绑定**状态下
   `/recycle` 仍会把历史 `private` 桶写进回忆录（交给人看的终端输出）。这是契约 §三 的
   "没有搭档时任何私聊都不是搭档"之外的一处残留语义，需要 team-brain 的 `handler_bound_at`
   机制覆盖到回收路径；我这边的 view 已经准备好接参数（`perform_recycle(view=...)`）。
2. **续跑标记是 meta 里的 JSON**：若有人手工清库/改实例号，标记会走"陈旧残留 → 清掉重来"
   分支（不会永久挡住回收）；极端情况下会重新生成一份档案（`_unique_path` 保证不覆盖原件）。

---

## 5. 改动清单（供 lead 复核 diff）

```
giftia/recycle.py     +约 190 行：_handler_view / _all_scopes / _unique_path / _resolve_sealed_path
                                   _sealed_json / _recycling_marker / _resume_report
                                   RecycleReport(+sealed_count/sealed_scope/resumed) / notification()
                                   perform_recycle(view=...)，时间戳档案名 + 标记 + 续跑
giftia/occasions.py   _parse_event_date(raw, today=None) 重写；_handler_view 新增
                      find_occasions(view=None → 搭档视角)；两个去重 token 带上 today
                      days_together_text(..., *, view=None)
giftia/dream.py       _handler_view 新增；should_dream(..., view=None) 与 make_dream 同口径
                      fragments=0 → max(1, ...)
```

---

## 6. 追加任务：lead 指定的 ws/qq 探针独立验证（同日第 2 轮）

lead 交办两件事：① 把 `pa_ws_probe.py` 里"记录 bug 的 `[FAIL]`"翻转为"记录正确行为"的断言；
② 独立跑 `pa_ws_probe` / `pa_qq_probe`，把结论补进 `FIX_REGISTER` 的 P1-21~P1-25、P0-5、ws 加固行。

### 6.1 我改的期望与理由（全部写进探针注释）

原探针的 `[FAIL]` 有两种含义纠缠在一起：**"缺陷存在"**（PASS 反而说明有问题）与
**"契约不满足"**。修复后必须把前者翻转成后者，否则探针会持续"绿着报错"。

| 位置 | 原期望 | 新期望 | 实测依据 |
|---|---|---|---|
| §1 握手 | 缺 Upgrade/Connection **仍接受** | **必须拒绝** | `WSError: 缺少 Upgrade: websocket 响应头（None）`；`ws.py:139-142` 已按 RFC6455 §4.1 校验 |
| §2 控制帧 | FIN=0/200B ping **被接受并回显 pong** | **拒绝且不回显** | `echo=[]`、`WSError: 控制帧不允许分片（opcode=9）` |
| §2 之后 | 违规帧之后**连接照常工作** | **连接不可信、读不到 AFTER** | 后续 `recv_text` 不再返回 `AFTER` |
| §4 半包超时 | 单场景混两种超时、靠 sleep 抢时序 | **4a/4b 两个独立用例** | 见 6.2 |

> §4 为什么要拆：原场景"先发 2 字节帧头再卡住 2.5s"到底触发哪种超时，取决于首读有没有抢到那 2 字节——
> 这是时序竞态，不是契约。修复后的契约是两条清晰的分支：
> ① 一个字节都没读到时 `WSTimeout`（干净空闲超时，连接仍可用）；
> ② 帧已经开始读（哪怕只有帧头）时 `WSClosed`（字节流错位，连接不可信）。
> 新的服务端脚本 `s_hold_open` / `s_partial_frame_hold` 行为确定、不抢时序。

### 6.2 真实输出

```
python .review\run_tests.py .review/probes/pa_ws_probe.py
  …
  通过 13 项，失败 0 项

  1. [PASS] 缺 Upgrade/Connection 响应头时必须拒绝（不能只看 accept）
          -> WSError: 缺少 Upgrade: websocket 响应头（None）
  2. [PASS] 违规控制帧（FIN=0 / 200 字节）被拒绝且**不回显 pong**  -> echo=[] got=<WSError: 控制帧不允许分片（opcode=9）>
     [PASS] 违规控制帧之后连接不可信/不得被当成正常连接（读不到那条 AFTER）
  3. [PASS] 100 个 4000 字节续帧（总 400KB）被完整重组
  4a[PASS] 空闲超时抛 WSTimeout、按调用方给的 timeout 生效  -> 0.31s
     [PASS] 空闲超时后连接**仍然可用**（下一次读还是干净的 WSTimeout，不是 WSClosed）
  4b[PASS] 帧内超时抛 WSClosed  -> WSClosed(读取帧中途超时，连接状态已不可信)（0.31s）
  5. [PASS] 对端 close 之后 close() 真的关闭 socket（fileno=-1、服务端 EOF=True）+ 回应了关闭帧
  6. [PASS] 长度编码/max_frame 拒绝、UTF-8 跨分片跨包不破

python .review\run_tests.py .review/probes/pa_qq_probe.py
  通过 8 项，失败 3 项
  §1 [PASS] 非 JSON 响应不会把异常抛穿事件处理（会拆掉整个 WS 会话）
  §1 [PASS] 鉴权接口返回非对象 JSON 时也是 QQError
  §1 [FAIL] _request 契约「失败一律 QQError」 -> 正常返回 SendResult(sent=[], failed=[('你好呀', "响应不是合法 JSON…")])
  §2 [PASS] 两次合法回复都会真的发出去  -> 2 个 HTTP 请求，delivered='嗯，我在。'
  §2 [PASS] 没发出去时 SendResult 不会谎报 delivered
  §3 [FAIL] 单聊里同一个 msg_id 的回复条数不超过 4（平台硬限）  -> 5 条
  §4 [FAIL] 限频命中时不丢消息（或至少如实上报为失败）  -> 被硬丢：ok=False
  §5 [PASS] 主动消息也受 send_max_per_minute/hour 约束（lead 说这条 P2 还要再修，本轮不动断言）
  §6 [PASS] poke 识别不会吃掉正常文本
  §7 [PASS] 群成员发 /bind 不认主；真搭档还能绑回来

python .review\run_tests.py tools/audit.py                → 通过 10 项，失败 0 项
python .review\run_tests.py tools/ws_selftest.py          → 通过 17 项，失败 0 项
python .review\run_tests.py tools/qq_protocol_selftest.py → 通过 72 项，失败 0 项
```

### 6.3 交回 lead 的两个结论（我没有改别人的代码/断言）

1. **P1-21 未修完（P0 级数据点）**：`pa_qq_probe` §3 实测"单聊同一 msg_id 共 **5** 条 > 平台硬限 4"。
   日志里已有 `[发送] 这条回复超过平台上限（同一 msg_id 最多 4 条），已把尾巴并成一段（4 段）`，
   但紧跟的"再加一条系统通知"仍让同一 msg_id 变成 5 条 —— 说明**追加/通知路径没有走同一个条数闸**。
   `qq_protocol_selftest` 72/0 覆盖不到这条。
2. **P1-22 的 `_request` 契约仍有一条 FAIL，需要产品级取舍**：
   `_parse_body`（qq_official.py:296-311）本身**已经**是"失败一律 QQError"，文档与实现一致；
   这条 FAIL 是因为 `send()` **在外层**把 `QQError` 吞成了 `SendResult(sent=[], failed=[...])`。
   两种写法都能防"坏响应拆连接"（P0-5 的目标达成），但吞掉之后是**静默**的：
   投递失败只出现在 `failed` 里，`respond_safely` 看不到异常。
   建议二选一：① 改探针期望，承认 `send()` 层用 `SendResult.failed` 表达失败；
   ② 让 `send()` 保留抛出。我按"不动别人的断言"处理，只报事实。

