# team-brain 修复报告（第 1 波：编排与隐私闸门层）

- 负责人：`team-brain`
- 拥有并修改的文件：`giftia/brain.py`、`giftia/persona.py`、`giftia/agent_tools.py`、`giftia/trigger.py`
- 新增探针：`.review/probes/team_brain_fixes.py`（56 项断言，全绿）
- 契约依据：`.review/FIX_BATCH1_INTERFACE.md §一/§二/§三/§五`
- 覆盖条目：**P0-3a、P0-3b、P0-4、P1-3、P1-4、P1-5、P1-12、P1-13、P1-14**
  ＋ team-store 的 `view` 必填契约在我文件里的 3 处调用点

---

## 0. 一句话结论

| 条目 | 状态 | 关键证据 |
|---|---|---|
| P0-3a 未绑定/解绑语义 | 已修 | `auto_bind=false` 时私聊走未确认路径（不检索/不写私事/不推关系/工具关闭）；未绑定时 `/profile` 一个字都不输出 |
| P0-3b 换人后私事换主人 | 已修 | 记忆 + 工作记忆窗口 + 派生大日子三条路都过"绑定时刻遮蔽"；Lead `lead_v2` B2 `[PASS]` |
| P0-4 群发提示词泄漏 | 已修 | Lead `lead_v3` B `[PASS]`；两处 `find_occasions` 统一走 `_outreach_view()` |
| P1-3 `handle` 只捕 LLMError | 已修 | 全异常兜底 + `[内部错误]` 留痕 + 一轮一定有回复 |
| P1-4 `execute` 承诺不抛 | 已修 | 大 `try/except`，失败回人话 + notices/diary |
| P1-5 backfill 静默失败 | 已修 | 失败记 diary/notices，并清掉"已完成"标记 → 下次启动重跑（实测重跑） |
| P1-12 花名册群隔离+上限 | 已修 | 301 人 → 花名册 **1 行 / 63 字**（原 301 行 / 2463 字）；硬上限 30 |
| P1-13 印象块在场闸门 | 已修（注入路径） | `bc_prompt` 2a 由 FAIL→PASS；私聊仍全量（2b） |
| P1-14「零 token」 | 已修 | 旁听 0 次模型调用；旁听整理改由搭档那一轮/退出路径统一做（群聊记忆照旧生成） |

测试：**`selftest` 303 项全过 / 0 败**（基线 302 项 = 299 过 3 败），`qq` 72/0、`ws` 17/0、`audit` 10/0。

---

## 1. P0-3b：绑定时刻遮蔽（换人后私事不换主人）

契约 §二要求"新搭档看不到 `happened_ts < handler_bound_at` 的私事"。实现时发现**只比这一个字段不够**，
实际落地的是"一次绑定 = 三条判据"，全部集中在 `brain.py`（没有动 SQL / `_view_clause`）：

| 判据 | 为什么需要 |
|---|---|
| `meta['handler_bound_at']`（时间戳） | 契约要求的语义："他们之间没有共同的过去" |
| `meta['handler_bound_mem_id']`（private 记忆 id 水位线） | 这个库的时间戳**只有秒级精度**，而 `happened_ts` 的语义是"事情发生在故事里的什么时候"（`consolidate` 取批内**最早**一条消息的时间）。同秒内"前任留言 → 换人"会得到 `happened_ts == floor`，只比时间戳分不开（Lead 探针 B2 最初就是这样 1 条漏过） |
| `meta['handler_bound_msg_id']`（消息 id 水位线） | 同一类问题还有**第二条通道**：搭档私聊的 `session_id` 对每一任都是 `i<n>-m-<日期>`，同一天换人时新搭档的工作记忆里摆着前任刚说的话（`recent_messages` / `history()`） |

判据（`_memory_visible` / `_since_floor`）：

```
private 记忆：id <= 水位线             → 藏着（精确，与秒级时间戳无关）
              id >  水位线 且 happened_ts < 下界 → 藏着（绑定之后写、但说的是绑定之前的事）
              其余                            → 看得见（同一任自己说的话照旧记得）
消息窗口：    id <= 消息水位线           → 藏着
              没水位线（老库）才退回 ts > 下界（严格晚于）
```

遮蔽只作用在 `private` 归属上——群里的公开的事、别人私聊里的事各有各的视角闸门，跟"谁是搭档"无关。
覆盖的读取路径：`_prepare`（检索）、`impressions_block`、`profile_text`、`agent_tools._search/_forget`、
工作记忆窗口、`history()`，以及**派生结果**（`find_occasions` 出来后按被遮的记忆 id 再滤一遍，见 P0-4）。

换人判定：

- 每一次 `set_handler_id(新人)` 都**重设**三条判据（水位线只会往前推）。
  这一条是自查时补上的：初版只在"还没有下界"时设一次，于是 **A→B→C 连续换人时 C 能看到 B 的私事**。
- 同一个人 `/unbind` 后再 `/bind` 回来（`meta['handler_prev_id']` 对得上）→ 不重设：他记得的是自己的过去。
- 首次绑定（库里没有任何 private 记忆）→ 下界留空。
- 本地 CLI（`user_id` 缺省 = `LOCAL_USER_ID = "handler"`）→ 不设下界：本机调用方就是搭档本人，
  他看的是自己留下的记忆（否则本地用户在升级后第一次说话就会"失忆"）。

### P0-3a：未绑定 = 未确认/客人

- `handle()`：有搭档时 `is_handler = (uid == owner)`；没搭档时私聊先走**核心认主**（`_auto_bind`，
  与适配器同一个 `[runtime] auto_bind` 开关）——认主之后就按搭档处理，且**必然经过上面的遮蔽**；
  群聊一律不算；`auto_bind = false` 时私聊直接落到未确认路径。
- 未确认路径（`owner == ""`）：`memory_view=[("person", uid)]`、`memory_scope=("person", uid)`、
  不推进关系/心情、工具关闭。
- `agent_tools.available()` 增加第三道判据：`handler_id` 为空 → 不可用
  （"没有搭档，就没人有资格动『你们之间』的记忆"）。`specs()`/`execute()` 都只走这一个判据。
- `profile_text()`：`handler_id` 为空时**不输出任何档案**（契约 §三.4 的"owner 为空时一律拒绝"）。

## 2. P0-4：群发提示词泄漏

`compose_outreach` 的第二次 `find_occasions(...)` 原来没传 `view`（= 默认搭档视角），
而 `outreach_plan` 传的是本群视角。现在两处共用一个 `_outreach_view(for_group)`，
并且都经过 `_find_occasions()`（顺带做 P0-3b 的大日子遮蔽）。
花名册也改成传"这一条要发到哪个群"：`_people_for_prompt(self.last_group_id if for_group else "")`。

## 3. P1-3 / P1-4 / P3 空回复：一轮一定有回复

- `_chat`：`except LLMError`（走 `FAILURE_LINES` + `[模型请求失败]`）之外补 `except Exception`
  → `FAILURE_LINES` + `[内部错误] 生成回复：类型: 消息`；`_tick_inner` / `_maybe_dream` /
  `mark_injected` / 两条 `add_message` / 回复后的记账各自包了兜底——**任何一步失败都不许把回复弄丢**。
- `handle()`：`_chat` 之外还有最后一道兜底；`_post_turn()` / 到期整理失败也只留痕，不冒穿（否则用户看不到那句回复）。
- 空回复（`""` / 纯空白）→ `FAILURE_LINES` 兜底，不再写一条空的 assistant 记录、不再让
  `note_reply_delivery("")` 因为 `"" == ""` 把空记录当成"完整送达"。
- `agent_tools.execute`：三个工具整体包在 `try/except Exception` 里，失败回
  「（刚才那个动作没做成，别当它发生过——先用你知道的继续说。）」＋ notices/diary（`title=工具失败`）。
- 留痕统一走 `_note_error()`：`notices` 里是 `[内部错误] 哪里：类型: 消息`，
  `diary(kind="terminal", title="内部错误")` 里是明细，**自己绝不抛**。

## 4. P1-5：隐私迁移失败可观测、可重试

`__init__` 的 `except Exception: pass` → 记 `notices` + `diary(title="隐私迁移失败")`，
并调 `_reset_backfill_markers()` 把 `msg_scope_backfilled` / `msg_scope_backfill_v2` 清零
（team-store 的 v2 实现把标记写在函数末尾；失败时若已落下标记，不清就等于永久放弃这次迁移）。
`import` 期不能写 `self.notices`，所以警告先攒在局部 `init_warnings` 里、建完对象再挂上。

## 5. P1-12：花名册按群隔离 + 硬上限

- 存储：`known_people` 的值升级成 `{"name", "groups", "last", "n"}`，**兼容老格式**（值就是名字字符串）。
  `known_people()` 保持老接口（uid → 名字）不变；新增 `_people_meta()` 给内部用。
- `remember_person(..., group_id=...)` 记"在哪个群见过"；名字/群没变且"最近活跃"未满 1 小时就不写库
  （避免每条群消息多一次 state 读改写）。
- `_people_for_prompt(group_id=None)`：**搭档永远第一行**，其余只取"本群见过的"（没有群上下文时
  只取"不在群里认识的"人），按最近活跃排序，`rows[:ROSTER_MAX]`（30）。`persona._render_people`
  再加一道 `ROSTER_MAX` 硬截断（第二道闸）。

## 6. P1-13：印象块的在场闸门

删掉"把 `by_person` 里剩下的人全部补进去"的兜底循环，但**用参数区分两条语义**：

- `present_only=True`（`_prepare` 在 group/guest 场景注入时）→ 只带在场的人：说话的这位、搭档、
  本条消息里点到的人（P1-13 的根因）；
- 默认 `False`（直接调用 = "查看整份档案"）→ 保持按人封顶的全量。

**偏差说明**：Lead 的指令是"删掉第二个循环"，我照做删掉了——但注入路径才用 `present_only=True`，
因为 `tools/selftest.py:1266` 明确要求**直接调用** `impressions_block("你好")` 时"小雨："也要在
（`check("印象块按人分组", "阿伟：" in imp and "小雨：" in imp)`），而小雨在本轮不在场；
`bc_prompt` 2a 检查的是**注入进 system 的那一份**，两条因此可以同时满足。
如果 Lead 要的是"直接调用也只带在场的人"，那就得改 selftest 那条期望（我没动 `tools/`）。

## 7. P1-14：「零 token」与旁听整理

- `learn_name(..., allow_llm=...)`：旁听（`log_only`）只走正则，不做模型兜底
  （「我是来看热闹的」不再多花一次调用）。
- 旁听分支不再 `self.consolidate()`，改 `_mark_listen_pending()`：state 里留
  `listen_pending = {session_id: ts}`（最多留 50 个会话）。
- 接续（保证群聊记忆照样生成）：`_post_turn` → `flush_listen_pending()` 调
  `flush_sessions(min_pending=1, stale_minutes=0, exclude_session=当前会话)`，
  整理完清标记；`close()` 的 `flush_sessions(min_pending=1)` 也在（退出路径不漏）。
- `trigger.py` 模块头把"零 token"改成准确说法：不回复、不建会话、不逐条调模型；
  攒下来的旁听由搭档那一轮/退出路径统一整理（那一步会花一次调用）。README 那句归 Lead。

## 8. team-store 契约：`view` 必填

`brain.py` 剩下三处不带 `view` 的自检/展示调用点全部显式化，并写明为什么可以看全量：
`status_text()`（只数条数）、`memories_text()`（`/memories` 排障）、`recall_text()`（`/recall` 排障）
→ `view=Store.ALL_SCOPES`。真实注入路径走 `visible_memories()`（视角 + 遮蔽）。
`agent_tools` 的 `_search` / `_forget` 也从"直接查 store"改成 `hub.visible_memories()`。

---

## 9. 验证（真实输出）

### 9.1 主套件：比基线更好

```
> python .review\run_tests.py tools/selftest.py
通过 303 项，失败 0 项
```

基线是 302 项 = 299 过 / 3 败（那 3 项在 §24 outreach，team-behavior 的活）。
`qq` 72/0、`ws` 17/0、`audit` 10/0（4 条提示）也全绿：

```
===== tools/qq_protocol_selftest.py =====  通过 72 项，失败 0 项
===== tools/ws_selftest.py ==============  通过 17 项，失败 0 项
===== tools/audit.py ====================  通过 10 项，失败 0 项（另有 4 条提示）
```

### 9.2 Lead 探针

```
> python .review\probes\lead_v2_p0_commands.py
B2. 解绑/换人后，新搭档的提示词里会不会出现前任的私事
   前任留下的 private 记忆：1 条
   新搭档 = 'INTRUDER'，遮蔽下界 = '2026-10-10T09:10:xx'
   新搭档能看到的 private 记忆：0 条（期望 0）
   [PASS] 新搭档的提示词里没有前任的私事
   （群视角对照：0 条 —— 应恒为 0）
```

```
> python .review\probes\lead_v3_p0_occasions.py
B. compose_outreach(target='group') 实际发给模型的 system 提示词
   提示词里出现的不该出现的私事：[]
   [PASS] 群发提示词只应包含本群公开的大日子（阿伟要考驾照）
C. 正常群聊提示词里的泄漏：[] → [PASS]
D. /days … [PASS] /days 只应列搭档自己的大日子 → 泄漏 []
```

> `lead_v2` 的 B 段仍会打印 `profile_text() 是否含密钥：True`，但同一行显示
> `handler_id = 'OWNER-OLD'`——**那不是"未绑定"状态**：第 52 行 `handle(..., "OWNER-OLD")` 已经
> 触发核心认主，此时她确实有搭档，`profile_text()` 给他看的是他自己的档案。真正的未绑定态
> （`auto_bind = false`）下 `profile_text()` 返回「她还没有搭档……」，见下面 9.3 的 I 段。

### 9.3 我自己的探针（`.review/probes/team_brain_fixes.py`）

```
> python .review\probes\team_brain_fixes.py
== A. P0-3b 绑定时刻遮蔽（换人 / 同一任）==
  [PASS] 同一任搭档自己说的话照旧记得（遮蔽不误伤）
  [PASS] ★ 换人后新人看不到前任的私事（记忆检索不漏）
  [PASS] ★ 换人后新人看不到前任的对话窗口（工作记忆也不漏）
  [PASS] ★ /profile 也不吐前任私事（搭档视角同样过遮蔽）
  [PASS] 新人自己的事照旧记得
  [PASS] ★ 换人后新搭档的大日子里没有前任的事
  [PASS] ★ 同一个人 /unbind 之后回来，仍看得到自己说过的话
  [PASS] ★ 第二任看不到第一任的私事
  [PASS] ★ 第三任既看不到第一任、也看不到第二任的私事
== B. 未绑定 + auto_bind=false：私聊不是搭档 ==
  [PASS] 未绑定私聊不再默认是搭档（is_handler=False）／没有认主／没有写 private 记忆
  [PASS] ★ 这种状态下工具不开放 ／ ★ 就算硬调也被拒
  [PASS] 不推进关系（亲密度没动）／ 整理出来的记忆归属不是 private
== C. P0-4 群发（compose_outreach）视角一致 ==
  [PASS] ★ 群发提示词只含本群公开的大日子
== D. P1-13 印象块在场闸门（注入路径）==
  [PASS] ★ 群聊注入的印象块只带在场的人 ／ 私聊里仍然带群成员的印象
== E. P1-12 花名册群隔离与上限 ==
  [PASS] ★ 群 A 的花名册里没有只在群 B 出现过的人
  [PASS] ★ 花名册有硬上限（≤ 30 行）／ 按最近活跃排序
      60 人之后花名册 30 行 / 238 字
== F. P1-14「零 token」与旁听整理的接续 ==
  [PASS] ★ 旁听不调模型（含名字判定的模型兜底）／ ★ 攒够阈值也不当场整理
  [PASS] ★ 搭档那一轮把旁听补整理成记忆（群聊记忆不会永远不生成）
== G. P1-3 / P1-4 异常不再冒穿 ==
  [PASS] ★ execute 在 DB 写失败时仍然返回文本（承诺成立）
  [PASS] ★ DB 写失败时 handle() 不抛异常且这一轮有回复
  [PASS] ★ 非 LLMError 也被兜住（不抛异常）／ ★ 空回复用 FAILURE_LINES 兜底
== H. P1-5 隐私迁移失败可观测、可重试 ==
  [PASS] ★ 回填失败有提示 ／ ★ 失败时没有留下「已完成」标记 ／ ★ 下一次启动真的重跑了回填
结果：PASS=56 FAIL=0
```

### 9.4 交叉验证：brain-checker 的检出探针（它们断言的是"这里有病"）

| 探针 | 修前 | 修后 |
|---|---|---|
| `bc_prompt.py` | 2a FAIL（印象块带 14 个不在场的人）、3a FAIL（跨群花名册） | **10 PASS / 0 FAIL**（2a、3a 翻绿） |
| `bc_growth.py` | 1a/1b PASS（301 人 → 301 行 / 2463 字） | 1a/1b **FAIL = 病没了**：301 人 → **1 行 / 63 字**，整条 system 2447 字 |
| `bc_trigger.py` | 4b/4c PASS（旁听调了一次名字模型 / 每 6 条旁听一次整理） | 4b/4c **FAIL = 病没了**：`name_calls 0 -> 0`、`extract_calls` 曲线全 0 |
| `bc_branches.py` | D2 PASS（前任私事进了新人提示词） | D2 **PASS = 没有泄漏**（该探针的标签写的是病象、判定条件是 `SECRET not in system`） |
| `bc_fault.py` | 3a FAIL（非 LLMError 抛穿） | 3a **PASS**（被兜住） |
| `bc_tools.py` | 6a/6b FAIL（DB 写失败抛穿） | 6a/6b 覆盖在我探针的 G 段（同一条路径，PASS）；`bc_tools.py` 本身现在跑不完——见下 |

---

## 10. 已知残余 / 不在我范围 / 需要 Lead 决策

1. **`bc_*` 检出探针有几处会因为 team-store 的 `view` 必填契约而崩**
   （例如 `bc_tools.py:127` `active_memories(h3.instance_no)` → `TypeError: missing 'view'`）。
   这是探针侧的适配（补 `view=Store.ALL_SCOPES`），不在我拥有的文件范围内，我没有改它们。
2. **`bc_branches.py` C2 由 PASS 变 FAIL**，但性质是"病没了"：C2 断言"客人拿得到本群公开记忆"，
   修前它是靠**印象块的兜底循环**（把不在场的阿伟补进提示词）才成立的；P1-13 修掉之后，
   客人那一轮的检索结果按 query 正常为空。客人的**视角**仍然是本群 `[("group", gid)]`，
   问对了事照样检索得到。
3. **`auto_bind = false` + 未绑定**才是"没有搭档"的完整语义。默认 `auto_bind = true` 下，
   核心会在第一条私聊上**认主**（与适配器同一开关、同一行为），所以"未绑定 + 私聊"在生产里
   只在关掉认主或适配器明确传 `is_handler=False` 时出现。这一条是对任务书字面的**有意偏差**：
   若核心不认主，`tools/selftest.py` 里大量"不带 user_id 直接 `brain.handle()`"的用例
   （§2/§3/§7 到期回收/§17 关系/§22 纪念日…）会全部掉进客人路径，基线 299 项必然崩。
   契约 §三.1 本身也保留了"自动认主"（"第一条私聊会先把它设为搭档……随后这一条按搭档处理"），
   隐私由 §二 的遮蔽兜住。
4. **同秒边界的残留**：绑定**之后**写下、但说的是绑定**之前**的事的记忆（延迟整理前任留下的话），
   靠 `happened_ts < 下界` 遮。若前任的话与绑定发生在**同一秒**且当时还没整理，理论上仍会漏一条
   （第二级精度所限；水位线只能证明"记忆是什么时候写下的"，证明不了"内容是谁说的"）。
   彻底堵死需要在换人时把前任**尚未整理**的私聊消息从待整理集合里摘掉——那要动 `memory.consolidate`
   的游标语义（team-store 的文件），我没做。
5. **`people_stats`（P2）仍在每条群消息全量重写、无人读取**：不在本波任务清单里，未动。
6. **`_render_group_chat` / `history()` 在群场景不过遮蔽**（群聊记录是公开的，不受换人影响）；
   但如果将来给"搭档私聊"也注入群聊记录，要记得那条路已经有 `_since_floor`。
7. **`reconcile_memories()` / `make_dream()`** 仍然会在 private 全桶上做合并/做梦取材（不对外输出内容），
   换人后的那条下界没有参与它们的选择；这是记忆维护路径，不是注入路径，未在本波范围内。
8. `imprint`（印象块）直接调用语义见 §6 的偏差说明——**需要 Lead 确认**是保留（现状）还是改 selftest 期望。
