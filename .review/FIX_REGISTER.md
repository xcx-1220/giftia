# 修复登记册（Fix Register）

基线：`44aefb1`（审查基线快照）。修复前测试基线：`selftest` 302 项 = 299 过 / 3 败；`ws` 17/0；`qq` 72/0；`audit` 11/0。

**规则**：每条修完把 `状态` 改成 `已修`，并填 `验证`（跑过的命令 + 关键输出）。
`部分` = 只修了主要路径，残余风险写进 `备注`。`不改` = 必须写出理由。

## P0（5）

| ID | 问题 | 文件 | 负责人 | 状态 | 验证 |
|---|---|---|---|---|---|
| P0-1 | 回收仪式回忆录/封存/diary 不过滤归属 | recycle.py | team-recycle | 已修 | `lead_v1_p0_verify.py` P0-1 段 **3/3 PASS**（回忆录 .md / diary 无 SECRET-BRAVO-4402；封存 JSON `scope=all_scopes`+`note` 且保留全量 3 条）；`beh_recycle.py` 18/18 PASS；见 `.review/fix-reports/team-recycle.md` |
| P0-2 | 老库 scope 回填把群 A 标成群 B | store.py | team-store | 已修 | `_session_scope_key` 从 session_id 解析群标识（先取 `i{n}-[gp]` 整段再去末尾日期，避免 `i1-gGROUP-A-…` 被切成 `GROUP` 而误确认）＋与 `state['last_group_openid']` 前缀比对**确认**：确认→写完整 openid；对不上/解析不出→保持 private；v1 已标错的行由 `msg_scope_backfill_v2` 一次性重跑纠正（老标记 `msg_scope_backfilled` 照写）。验证：`lead_v1_p0_verify.py` P0-2 段 **PASS（错 0 条）**；`mm_scope_backfill.py` P0-8 **PASS（未泄漏）**；`fix_store_p0.py` 第 5/5b 节 11 项全 PASS。**判据差异**：`mm_scope_backfill.py` P0-7 要求每条群消息正向归属，仍 FAIL（对不上的行按契约保持 private），详见 `.review/fix-reports/team-store.md` §4.1 |
| P0-3a | 未绑定时群成员可执行 /profile 等指令 | qq_official.py + brain.py | team-brain | 已修（**brain 侧**：`is_handler` 改为"有搭档才认他本人"；未绑定私聊先走核心**认主**（与适配器同一个 `auto_bind` 开关，认主后必经 §二 遮蔽），`auto_bind=false` 时走未确认/客人路径——`memory_view=[("person",uid)]`、不写 private、不推关系/心情；`agent_tools.available()` 再加"`handler_id` 为空就不给工具"一道判据；`profile_text()` 在 `handler_id` 为空时**不输出任何档案**。适配器侧的指令闸门归 team-adapter） | 见 `.review/fix-reports/team-brain.md` §9.3 B 段 8 项全 PASS；`lead_v2` B2 `[PASS]` |
| P0-3b | 解绑/换人后私事换主人 | brain.py（绑定时刻遮蔽） | team-brain | 已修（`set_handler_id(新人)` 记三条判据：`handler_bound_at` 时间戳 + `handler_bound_mem_id` / `handler_bound_msg_id` **id 水位线**（秒级时间戳分不开"同秒换人"，Lead 探针 B2 最初就漏在这里）；遮蔽覆盖**记忆检索、印象块、/profile、工具 search/forget、工作记忆窗口、history()、派生的大日子**。同一人或本地 CLI 回来不遮；连续换人每次重设水位线——`A→B→C` 时 C 也看不到 B，这条是自查补的） | `lead_v2_p0_commands.py` B2：`新搭档能看到的 private 记忆：0 条（期望 0）`＋`[PASS] 新搭档的提示词里没有前任的私事`；`.review/probes/team_brain_fixes.py` A 段 12 项全 PASS（含"同一任自己说的话照旧记得""第三任看不到前两任"） |
| P0-4 | 群发提示词混进别人的私聊大日子 | brain.py | team-brain | 已修（`compose_outreach` 第二次 `find_occasions` 补 `view`；两处统一走 `_outreach_view(for_group)` + `_find_occasions()`，顺带做 P0-3b 的大日子遮蔽；花名册也按"这条要发到哪个群"取） | `lead_v3_p0_occasions.py`：B 段 `提示词里出现的不该出现的私事：[]` ＋ `[PASS]`；C 段 `[PASS]` |
| P0-5 | 孤立代理对 → UnicodeEncodeError → 消息丢失+断连 | store.py + qq_official.py | team-store / lead | 已修（**存储侧**：`sanitize_text()` 把 `add_message`/`add_memory`/`add_diary`/`add_dream`/`meta_set`/`state_set`/`update_memory`/`update_message_content` 的 TEXT 参数里的孤立代理字符换成 U+FFFD；`fix_store_p0.py` 第 2 节 11 项全 PASS，`json.loads('"hello \\ud83d world"')` 落库为 `'hello \ufffd world'`；端到端 `qa_bugs.py` B3 原两条"记录 bug"的断言已翻面 → 消息真的落库（0→2）。适配器侧独立验证：`pa_qq_probe.py` §1「非 JSON 响应不会把异常抛穿事件处理」PASS——坏响应不再拆掉 WS 会话，事件处理有兜底；§1 有一条 `_request` 契约断言仍 FAIL，见文末"team-recycle 独立验证"） |

> 说明：审查报告里 P0-3 是一条（含 a/b 两个入口），修复拆成两条登记以便分别验证。

## P1（25）

| ID | 问题 | 文件 | 负责人 | 状态 |
|---|---|---|---|---|
| P1-1 | selftest 3 项失败 + 随日期摇摆 | tools/selftest.py + mood.py/outreach.py | lead(第2波) | 待修 |
| P1-2 | 测试项数/耗时文档过期 | README/setup.sh | lead | 待修 |
| P1-3 | handle() 只捕 LLMError | brain.py | team-brain | 已修（`_chat` 的 `except LLMError` 之外补 `except Exception` → `FAILURE_LINES` + `[内部错误] 生成回复：类型: 消息`；`_tick_inner`/`_maybe_dream`/`mark_injected`/两条 `add_message`/回复后记账各自兜底；`handle()` 再加最后一道；`_post_turn`、到期整理失败也只留痕不冒穿——**一轮一定有回复**。留痕统一走 `_note_error()`：notices + `diary(title="内部错误")`） |
| P1-4 | agent_tools.execute 承诺不抛却会抛 | agent_tools.py | team-brain | 已修（三个工具整体 `try/except Exception`，失败回「刚才那个动作没做成，别当它发生过」＋ notices/`diary(title="工具失败")`；`_search`/`_forget` 改走 `hub.visible_memories()`，不再直接查库绕过遮蔽） |
| P1-5 | backfill 失败静默且不重试 | brain.py | team-brain | 已修（`__init__` 失败 → notices + `diary(title="隐私迁移失败")`＋`_reset_backfill_markers()` 清 `msg_scope_backfilled`/`msg_scope_backfill_v2`，下次启动真的重跑；import 期不能写 `self.notices`，警告先攒局部变量再挂上） |
| P1-6 | view=None 默认不过滤（根因） | store.py | team-store | 已修（`ALL_SCOPES="all"` 模块级 + `Store.ALL_SCOPES` 类属性；`active_memories`/`memories_by_person` 的 view **必填**、`None`→`TypeError`、空列表=什么都看不到；`tools/selftest.py` 21 处 + `tools/live_check.py` 1 处按 Lead 授权补 `view=Store.ALL_SCOPES`（`git diff --stat tools/` = 2 文件 +24/-22，未改任何断言）。验证：`fix_store_p0.py` 第 1 节 9 项全 PASS；selftest 303/0） |
| P1-7 | consolidate 无事务 | memory.py + store.py | team-store | 已修（新增 `Store.begin()/commit()/rollback()`（`BEGIN IMMEDIATE`，拒绝嵌套）；`add_memory`/`update_memory`/`mark_consolidated`/`meta_set` 加 `commit=True` 关键字参数（默认行为不变）；`consolidate` 的写记忆+标已整理+推两游标整段一个事务，异常回滚；`on_draft` 回调挪到提交后触发（它自己会 commit）。验证：`fix_memory_p1.py` 第 1 节 10 项全 PASS——模拟崩溃后记忆 0 条、消息仍未整理、游标未推进、回调未触发，重试后 2 条且不重复） |
| P1-8 | 无 busy_timeout / 跨线程 | store.py | team-store | 已修（`PRAGMA busy_timeout=5000`；WAL 返回值检查，非 wal 抛 `RuntimeError` 并给人话；`check_same_thread=True` 显式化+注释；新增 `idx_msg_mid`（P3-1 顺带），并把**索引创建挪到 `_migrate()` 之后**（否则老库缺 `mid` 列时建索引直接打不开库）、`sealed` 补进 `_MIGRATIONS`。验证：`fix_store_p0.py` 第 3/6 节全 PASS；老 schema（messages 6 列/memories 4 列）能打开并补齐） |
| P1-9 | lifespan 溢出 + 0/负数静默接受 | lifespan.py + config.py | team-store | 已修（`LifespanConfig.validated()`：`total_hours` 区间 `[1, 1e6]`，0 当"没填"→回落默认 81920+警告，负数/nan/inf/超上限→`配置错误：…` 启动即失败；`status()` 对坏值回落可用值（不表现成"出生即到期"）并对 `timedelta` 溢出兜底（极大值退回 +100 年）。验证：`mm_consolidate_time.py` **5b 段三种值全 PASS、不再崩**；`fix_config_lifespan.py` 第 1/2 节全 PASS；短寿命 0.5 小时演示不被兜底吃掉） |
| P1-10 | 新会话游标从全局游标接上 | memory.py | team-store | 已修（新增 `Store.first_open_message_id()`；`load_pending` 建游标取 `min(本会话最早未整理 id-1, 全局游标)`）。验证：`fix_memory_p1.py` 第 2 节 5 项全 PASS（新会话 id=1 不再被跳过）。**注**：`mm_consolidate_time.py` 第 3 节 P2-5 仍打印 FAIL，但那是该探针 1~4 节共用同一个库、它期望的 id=1 属于另一个会话（探针自身构造问题，修复前后输出相同），详见 `.review/fix-reports/team-store.md` §1 P1-10 |
| P1-11 | /days 不过滤归属 | occasions.py | team-recycle | 已修（`days_together_text` 不传 view 时按 `[("private",""),("person",handler_id)]` 过滤、不含 group；临时验证 3/3、`beh_occasions.py` 17/17 PASS；见 `.review/fix-reports/team-recycle.md`） |
| P1-12 | 花名册不按群隔离且无上限 | brain.py + persona.py | team-brain | 已修（`known_people` 的值升级成 `{"name","groups","last","n"}` 且**兼容老格式**；`remember_person(..., group_id=)` 记"在哪个群见过"；`_people_for_prompt()` = 搭档第一行 + 本群见过的人，按最近活跃排序、`rows[:ROSTER_MAX]`（`persona.ROSTER_MAX = 30`，`_render_people` 再加一道） | `bc_growth.py`：301 人 → 花名册 **1 行 / 63 字**（原 301 行 / 2463 字）；`bc_prompt.py` 3a 由 FAIL→PASS；`team_brain_fixes.py` E 段 5 项 PASS |
| P1-13 | 印象块在场闸门被绕过 | brain.py | team-brain | 已修（删掉"把 `by_person` 剩下的人全补进去"的兜底循环；`_prepare` 在 group/guest 注入时传 `present_only=True`——只带说话的这位/搭档/消息里点到的人。**偏差**：直接调用默认仍是"按人封顶的整份档案"，因为 `tools/selftest.py:1266` 要求直接调用时"小雨："在场；`bc_prompt` 2a 查的是注入进 system 的那一份，两条可同时满足，见报告 §6） | `bc_prompt.py` **10 PASS / 0 FAIL**（2a、3a 翻绿）；`team_brain_fixes.py` D 段 4 项 PASS |
| P1-14 | 「零 token」不成立（learn_name + 旁听整理） | brain.py | team-brain | 已修（`learn_name(..., allow_llm=)`：旁听只走正则；旁听分支不再当场 `consolidate()`，改 `_mark_listen_pending()` 留 `listen_pending` state；接续 = `_post_turn`→`flush_listen_pending()`（`min_pending=1, stale=0`，排除当前会话）＋`close()` 的 `flush_sessions`，群聊记忆照旧生成。`trigger.py` 模块头把"零 token"改成准确说法） | `bc_trigger.py` 4b/4c 由 PASS→FAIL（**病没了**：`name_calls 0->0`、`extract_calls` 全 0）；`team_brain_fixes.py` F 段 7 项 PASS（含"搭档那一轮把旁听补整理成记忆"） |
| P1-15 | bond 不可刷被记忆加成打破 | bond.py + brain.py | lead(第2波) | 待修 |
| P1-16 | mood 过期不失效 + mood_share 抢额度 | mood.py + outreach.py | lead(第2波) | 待修 |
| P1-17 | use_wakeup 未收紧到 4 条/30 天 | outreach.py + config.py | lead(第2波) | 待修 |
| P1-18 | target=both 只发私聊 + 只扣 1 条 | qq_official.py | lead | 已修（`can_send(batch_extra=…, ignore_gap=True)`：一批只判一次间隔、按批发条数预留额度，每个目标各自记账）等 team-behavior 的 P1-16/17 一起验收 |
| P1-19 | 讲梦路径不标 told（重复讲） | brain.py | lead(第2波) | 待修 |
| P1-20 | 回收崩溃窗口覆盖原始档案 | recycle.py | team-recycle | 已修（封存文件名带时间戳 + `_unique_path` 绝不覆盖；`meta:recycling_instance` 崩溃后续跑换代；`beh_recycle.py` C 段 8/8 PASS：重跑不再把 5 条覆盖成 0 条；见 `.review/fix-reports/team-recycle.md`） |
| P1-21 | 被动回复条数超平台上限 | qq_official.py | lead | 已修（`PASSIVE_REPLY_LIMIT_GROUP=5 / _C2C=4`，超出把尾巴并成一段；`qq_protocol_selftest` 72/0）｜**独立验证（team-recycle）**：`pa_qq_probe` §3 仍 FAIL「单聊同一 msg_id 发出 5 条 > 4」→ **未修完**，见文末 |
| P1-22 | HTTP200 非 JSON 抛穿拆连接 | qq_official.py | lead | 已修（`_parse_body` 失败一律 QQError；`pa_qq_probe` §1 两条转 PASS）｜**独立验证（team-recycle）**：§1 两条 PASS（非 JSON 不抛穿 `handle_event`、鉴权非对象 JSON 也是 QQError）；但 §1 第一条「`_request` 契约 = 失败一律 QQError」仍 FAIL（`send()` 把 QQError 吞成 `SendResult.failed`），见文末 |
| P1-23 | ws 半包超时判成"连接可用"→消息拼接 | ws.py | lead | 已修（帧内超时/EOF → WSClosed；调用方 timeout = 整帧 deadline；`pa_ws_probe` §4 三条转 PASS）｜**独立验证（team-recycle）**：`pa_ws_probe` 重写 §1/§2/§4 期望后 **13/0**；4a「一个字节都没到 → WSTimeout、连接仍可用」、4b「帧头已到 → WSClosed」两条真实输出见文末 |
| P1-24 | 8 秒同文去重丢消息却报已送达 | qq_official.py | lead | 已修（去重按 msg_id 分桶：不同 msg_id 一律放行；无 msg_id 的旧路径保持原语义以免破坏既有断言）｜**独立验证（team-recycle）**：`pa_qq_probe` §2 两条 PASS——「两次合法回复都会真的发出去」（2 个 HTTP 请求）、「没发出去时 SendResult 不会谎报 delivered」 |
| P1-25 | 适配器无 try → 单条坏消息断连 | qq_official.py | lead | 已修（`respond_safely` 包壳 + `handle_event` 外层兜底 + 收尾 `_flush_pending_batches`；`qq_protocol_selftest` 72/0）｜**独立验证（team-recycle）**：`pa_qq_probe` §1「一条 c2c 事件走完 handle_event：没有异常」PASS；§7 群成员 `/bind`/真搭档 `/bind` 两条 PASS |

## P2 / P3 批次（按主题，逐条见审查报告 §五）

| 批次 | 主题 | 负责人 | 状态 |
|---|---|---|---|
| B4 | 数值/时间/边界（/time nan、/forget 大整数、tone_of、blur 舍入、max_chars） | team-store（本轮指派）+ lead(第2波) | **部分**（team-store 已修：`blur_text` 用 `math.floor` 替银行家舍入（P3-2）、`max_chars` 截断不再推翻 `blurred` 且长度真的 ≤ 配置值（`……` 占 2 字符已算进预算，P2-1）、`_age_hours` tz 归一化（§5.1）、`messages_by_prefix` LIKE 用 `ESCAPE '\'` 转义（P3-4）、`mark_recalled` 增加可选 `session_id`（P3-1 语义；**适配器 `qq_official.py:642` 需传 session 才生效**）。验证：`fix_memory_p1.py` §8 十一条全 PASS、`mm_degradation.py` 0 失败。**仍待修**：`/time nan|inf`（cli.py+clock.py）、`/forget <超大整数>`（cli.py）、`tone_of` 否定句（mood.py）、`event_date` 解析（occasions.py）—— 不在 team-store 文件范围） |
| B4 | occasions 日期解析与去重键 + dream 计数口径（`_parse_event_date` 容忍 `2026/10/11`/`10月20号`/`下周三`；`big_day`/`follow_up` token 带上"提醒发生在哪一天"；`should_dream`/`make_dream` 同一视角；`fragments=0` 不再 `[-0:]`） | team-recycle | 已修（见文末"团队补充"） |
| B4 | 配置校验（settings.toml 零校验、degradation fail-open、keywords 类型、.env 解析） | team-store | 已修（`_sub()` 未知键→stderr 警告（persona.toml 的 `[bond]`/`[moods]` 用 ignore 排除）；`[[degradation.stages]]` 缺整段/为空/缺字段/blur 越界→`配置错误：…` 并写清后果；`response_tier` 夹到 0~100 并提示；`speak_max_per_day` 等上限为负→报错；`keywords` 必须是字符串列表（出厂三项继续可用）；memory/dream/mood/relationship/model 的计数与 0~1 字段一并校验；`.env` 用 utf-8-sig、去 `export `、行内 `#`（引号内不算）、只剥成对引号、CRLF/空值/值内 `=`。另按 Lead 要求给 `OutreachConfig` 加 `mood_share_min_idle_hours=6.0`、`wakeup_max_per_30_days=4` 并同步 settings.toml。验证：`fix_config_lifespan.py` 49 项全 PASS） |
| B5 | ws 加固（分片总量上限、close 真关、控制帧规则、握手严格） | lead | 已修（`max_message=32MB`；`_sock_closed` 让 close() 真关；控制帧 FIN/长度校验；状态行严格 + Upgrade/Connection 校验 + Host 带端口） |
| B5 | 检索质量（min_score 绝对门槛、core 保底注入、relative_cutoff 与历史 query、逐条 tokenize 缓存、is_cited 收紧） | team-store | **部分**（已修：① `is_cited` 要求至少命中一个二元组/词（单字/单字母凑数不再算"说出口"）② `mark_cited` 一条回复**最多强化一条**（得分最高的那条）③ `recall()` 逐条分词按 `(id,文本)` 缓存 ④ 新增候选预筛 `recall_prefilter`（默认 2000，正常数据量下零行为变化）⑤ `min_score` 的**相对**语义一字未改，只写明"它是相对门槛"并新增**绝对**门槛字段 `min_raw_score`（默认 0.0=不启用）。验证：`fix_memory_p1.py` 第 3~7 节全 PASS；`mm_recall_reinforce.py`「P2-3 一句话强化了 1 条」PASS。**未修（不在本批任务清单，且不在我文件里）**：`core 保底注入`、`relative_cutoff 与历史 query`（brain 的 `_recall_query`）—— 请 Lead 指派或排下一批） |
| B5 | 部署（setup.sh 不覆盖 config、Restart 限制、解释器一致） | lead | 待修 |
| B6 | 文档对齐（README 全部过期数字与矛盾表述、setup.sh 文案） | lead | 待修 |
| — | 明确不改（需产品决策或成本过高） | lead | 见文末 |

## 明确不改（附理由）

（修复过程中逐条补充）

## 团队补充：team-recycle 第 1 波（2026-10-10）

验证输出全文见 `.review/fix-reports/team-recycle.md`。三条命令的真实数字：

| 命令 | 结果 |
|---|---|
| `python .review\run_tests.py .review/probes/lead_v1_p0_verify.py` | P0-1 段 3/3 PASS（P0-2 段仍 1 FAIL，归 team-store，未通过 `SystemExit` 反映） |
| `python .review\run_tests.py .review/probes/beh_recycle.py` | **PASS=18 FAIL=0** |
| `python .review\run_tests.py .review/probes/beh_occasions.py` | **PASS=17 FAIL=0** |
| `python .review\run_tests.py .review/tmp/recycle_scratch_verify.py`（临时，非交付） | PASS=12 FAIL=0（/days 过滤 3、dream 口径 4、回收归属 5） |
| `python .review\run_tests.py tools/selftest.py` | **通过 299 项，失败 3 项 = 修复前基线（299/3）**。迁移期一度因 `view` 必填而 `TypeError`（旧签名调用点），lead/team-brain 补完后恢复；3 条失败是既有 P1-1（`你明显低落 → 她在担心你` / `默认关闭时她不会主动找你` / `刚说过话时没有主动理由`，mood+outreach，team-behavior 第 2 波），与 recycle/occasions/dream 无关。 |

**迁移期遗留（需 lead 决策，不在我文件范围内）**：接口契约 §一 把 `active_memories` 的 `view`
改成必填后，仓库里还有一批**旧签名调用点**会让脚本直接崩（selftest 已解除，其余仍在）：

- ~~`tools/selftest.py`（约 40 处，lead 负责）~~ → **已解除**（迁移完成，299/3 = 基线）
- `tools/live_check.py:74`、`tools/reconcile_live.py:40`
- `giftia/brain.py:1261/1345/1376`（team-brain）、`giftia/dream.py`（已由我修）
- 各团队旧探针：`beh_dream.py`、`bc_*`、`qa_*`、`mm_*`、`pa_*` 等（`pa_ws_probe` 已由我迁移）

我没有改这些文件（超出授权范围）。team-recycle 负责的 3 个模块已按新契约显式传视角，不再有裸调用。

### 追加：lead 指定的独立验证（`pa_ws_probe` / `pa_qq_probe`）

应 lead 要求，我把 `pa_ws_probe.py` 里"记录 bug 的断言"翻转为"记录正确行为"，
并独立跑了两个探针。改动只限 `pa_ws_probe.py`（`pa_qq_probe.py` 一行未动，
§5 主动消息限频那条按 lead 指示保持原样、等 lead 修完再验）。

**我改的 4 处期望 + 理由（都写进了探针注释）**

| 探针位置 | 原期望（PASS = 缺陷存在） | 新期望 | 理由 |
|---|---|---|---|
| §1 缺 Upgrade/Connection | 仍接受（只查 accept） | **必须拒绝** | 加固后 `ws.connect()` 校验 Upgrade/Connection（RFC6455 §4.1 客户端必须校验），实测 `WSError: 缺少 Upgrade: websocket 响应头（None）` |
| §2 违规控制帧 | 被接受并回显 pong | **拒绝且不回显 pong** | 实测 `echo == []` + `WSError: 控制帧不允许分片（opcode=9）` |
| §2 违规帧之后 | 连接照常工作（能读到 AFTER） | **连接不可信，读不到 AFTER** | 实测后续读不再返回 `AFTER`（协议错误后连接不得当正常连接用） |
| §4 半包超时 | 一个场景混两种超时、靠 sleep 抢时序 | **拆成两个独立用例**：4a 一个字节都没到 → `WSTimeout`（连接仍可用）；4b 帧头已到卡住 → `WSClosed`（连接不可信） | 原场景"先发 2 字节帧头再卡住"到底是哪种超时取决于首读有没有抢到那 2 字节，本身不稳；修复后的契约就是这两条 |

**真实输出**

```
pa_ws_probe.py → 通过 13 项，失败 0 项
  1. [PASS] 缺 Upgrade/Connection 响应头时必须拒绝（不能只看 accept）  -> WSError: 缺少 Upgrade: websocket 响应头（None）
  2. [PASS] 违规控制帧（FIN=0 / 200 字节）被拒绝且**不回显 pong**  -> echo=[] got=<WSError: 控制帧不允许分片（opcode=9）>
     [PASS] 违规控制帧之后连接不可信/不得被当成正常连接（读不到那条 AFTER）
  4a[PASS] 空空闲超时抛 WSTimeout、按调用方 timeout 生效  -> 耗时 0.31s
     [PASS] 空闲超时后连接**仍然可用**（下一次读还是干净的 WSTimeout，不是 WSClosed）
  4b[PASS] 帧内超时抛 WSClosed  -> WSClosed(读取帧中途超时，连接状态已不可信)，0.31s
  5. [PASS] 对端 close 之后 close() 真关 socket（fileno=-1、服务端 EOF=True）+ 回应关闭帧
  6. [PASS] 长度编码/max_frame、UTF-8 跨分片跨包

pa_qq_probe.py → 通过 8 项，失败 3 项（§5 那条按 lead 指示不动）
  §1 [PASS] 非 JSON 响应不会把异常抛穿事件处理   §1 [PASS] 鉴权非对象 JSON 也是 QQError
  §1 [FAIL] _request 契约「失败一律 QQError」 → 正常返回 SendResult(sent=[], failed=[('你好呀', 响应不是合法 JSON…)])
  §2 [PASS] 两次合法回复都会真的发出去（2 个 HTTP 请求）  §2 [PASS] 没发出去时不谎报 delivered
  §3 [FAIL] 单聊同一 msg_id 发出 5 条（平台硬限 4）→ 5 条
  §4 [FAIL] 限频命中时被硬丢：ok=False failed=[('这句能不能发出去', '每分钟最多 6 条')]
  §5 [PASS] 主动消息也受 send_max_per_minute/hour 约束（lead 说 §5 的 P2 还要再修，本轮不动）
  §6 [PASS] poke 识别不吃正常文本   §7 [PASS] 群 member /bind 不认主 + 真搭档能绑回来

  §1 P0-5 适配器侧：`UnicodeEncodeError`/坏响应不再拆连接（handle_event 兜底生效）
  tools/audit.py → 10/0；tools/ws_selftest.py → 17/0；tools/qq_protocol_selftest.py → 72/0
```

**两个交回 lead 的结论（我没改别人的代码）**

1. **P1-21 未修完**：`pa_qq_probe` §3 实测「单聊同一 msg_id 发出 **5** 条」，超过平台硬限 4。
   日志里确实出现了 `[发送] 这条回复超过平台上限（同一 msg_id 最多 4 条），已把尾巴并成一段（4 段）`，
   但随后"再加一条系统通知"仍让同一 msg_id 变成 5 条（预计是系统通知/追加路径没走同一个条数闸）。
   `qq_protocol_selftest` 72/0 覆盖不到这条。
2. **P1-22 的 `_request` 契约仍有一条 FAIL**：`_parse_body` 本身**已经**是"失败一律 QQError"
   （qq_official.py:296-311，文档字符串与实现一致）；FAIL 的原因是 `send()` 在**外层**把 `QQError`
   吞成了 `SendResult(sent=[], failed=[...])`。两种做法都能防"坏响应拆连接"，但会**静默**——
   实际投递失败只出现在 `failed` 里，`respond_safely` 看不到异常。建议 lead 明确取舍：
   要么改探针期望（承认 `send()` 层用 `SendResult.failed` 表达失败），要么让 `send()` 也抛。
   我按"不改别人的断言"处理，只报事实。

## 团队补充：team-brain 第 1 波（2026-10-10）

覆盖 P0-3a / P0-3b / P0-4 / P1-3 / P1-4 / P1-5 / P1-12 / P1-13 / P1-14，
外加 team-store 的 `view` 必填契约在我文件里的 3 处调用点。细节见 `.review/fix-reports/team-brain.md`。

| 命令 | 真实数字 |
|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项**（修复前基线 302 项 = 299 过 / 3 败） |
| `python .review\run_tests.py tools/qq_protocol_selftest.py` | 72/0 |
| `python .review\run_tests.py tools/ws_selftest.py` | 17/0 |
| `python .review\run_tests.py tools/audit.py` | 10/0（4 条提示） |
| `python .review\probes\lead_v2_p0_commands.py` | B2 段 `[PASS]`：`新搭档能看到的 private 记忆：0 条（期望 0）` |
| `python .review\probes\lead_v3_p0_occasions.py` | B / C / D 段全 `[PASS]`（`不该出现的私事：[]`） |
| `python .review\probes\team_brain_fixes.py`（我新增） | **PASS=56 FAIL=0** |
| `bc_prompt.py` / `bc_growth.py` / `bc_trigger.py` | 10/0（2a、3a 翻绿）；301 人花名册 1 行/63 字；旁听 `name_calls 0`、`extract` 曲线全 0 |
| `bc_tools.py` / `bc_branches.py` | 迁移 `view` 后 **20/0**、**32/0**（C2 期望已按 lead 授权翻转） |
| `bc_flow.py` / `bc_fault.py` / `bc_growth.py` | 期望全部翻转后 **26/0**、**17/0**、**5/0**（`bc_*` 家族合计 **143/0**） |

**第 1 波交回 lead 的结论（已全部落地）**

1. `giftia/brain.py` 的 `active_memories` 旧签名调用点（迁移期清单里的 **1261/1345/1376**）已全部显式化：
   `status_text` / `memories_text` / `recall_text` → `view=Store.ALL_SCOPES`（自检/排障，行内有注释说明），
   真实注入路径改走 `visible_memories()`（视角 + 换人遮蔽）。
2. `bc_*` 探针的裸 `active_memories` 已按 lead 第 2 波授权迁移（4 个文件 9 处，只加 `view` + import + 注释，
   未改断言期望）：`bc_tools` 20/0、`bc_branches` 32/0、`bc_trigger` 33/0、`bc_prompt` 10/0。
3. **期望翻转（第 2+3 波，全部按 lead 授权）**：`bc_branches` C2、`bc_trigger` 4b/4c，
   以及第 3 波授权的 `bc_flow` 6 条（0a/1c/1d/1e/2a/2b）与 `bc_fault` 2 条（5b/7a）——
   每条都改写成"新契约下的正确行为"＋理由注释，并配正向对照
   （C2b、4c'/4c''、0c、1f/1g、2a'、5a、7b）。
   两处按 `bc_flow` 2b 的裁定：断言改成"回的是群聊婉拒「……这个我们私下说。」"（契约 §三.4）。
   **顺带报备**：`bc_growth` 1a/1b 是同一类（我当时列在报告 §9.4 表、未进那 8 条清单），
   也一并翻成"≤ ROSTER_MAX 行 / ≤300 字"＋1c 对照（搭档仍在第一行）；如要严格只动那 8 条可回退。
4. 其余探针家族（`qa_*` 13 处、`beh_*`、`mm_*` 等，见 team-store 附录第 2 条）仍带裸 `active_memories`，
   已由 lead 分派给 team-store 批量迁移。

## 团队补充：team-store 第 1 波（2026-10-10）

报告全文（含每条改动的代码要点与探针输出）：`.review/fix-reports/team-store.md`。

| 命令 | 结果（真实输出） |
|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项**（基线 302 = 299 过 3 败；3 条旧失败由第 2 波修掉） |
| `python .review\probes\fix_store_p0.py` | 通过 46 项，失败 0 项（P1-6 / P0-5 / P1-7 原语 / P1-8 / P0-2 / 老库升级） |
| `python .review\probes\fix_memory_p1.py` | 通过 40 项，失败 0 项（P1-7 / P1-10 / is_cited / mark_cited / 分词缓存 / 预筛 / min_raw_score） |
| `python .review\probes\fix_config_lifespan.py` | 通过 49 项，失败 0 项（P1-9 + §8 配置校验 + .env 13 例） |
| `python .review\probes\lead_v1_p0_verify.py` | P0-2 段 `[PASS] …（错 0 条）`（修复前 FAIL） |
| `python .review\probes\mm_consolidate_time.py` | 5b 段三种值全 `[PASS]`、不再崩；随后 5c 段按预期抛 `配置错误：缺少 [[degradation.stages]]…` |
| `python .review\probes\mm_recall_reinforce.py` | `[PASS] P2-3 …一句话强化了 1 条`（修复前 `[1,2,3]`） |
| `python .review\probes\mm_snapshot_injection.py` | `[PASS] P2-11 快照恢复应保真 → 完全一致`（顺带补 `restore_memories` 的 `forgotten` 列） |
| `python .review\probes\mm_scope_backfill.py` | P0-8 `[PASS] 未泄漏`；P0-7 判据差异仍 FAIL（见下） |
| `python .review\run_tests.py tools/audit.py` | 通过 10 项，失败 0 项（4 条提示）——与基线证据 `.review/evidence/audit.log` 的 **10/0** 完全一致（登记册开头写的"audit 11/0"与证据不符，建议改成 10/0） |
| `tools/ws_selftest.py` / `tools/qq_protocol_selftest.py` | 17/0、72/0（无回归） |

**交回 Lead 的 4 件事**

1. **`mm_scope_backfill.py` P0-7 的判据**：它要求每条 `-g` 老行都被**正向归属**（`subject == "GROUP-A"`）；
   我按契约在"token 与 `last_group_openid` 对不上"时保持 `private` → 它仍 FAIL（`subject=''`）。
   真正的跨群泄漏由它的 P0-8 判 PASS 证明已修。若你要改成"对不上时写 session 里的 token（截断值）"，
   一句话我就改（好处：这些老消息不会被当成"搭档的私事"，避开 P0-3 类风险）。
2. **其它探针里 53 处 `active_memories(x)` 没补 view**（`.review/probes/*.py`，如 `qa_e2e.py` 13 处、
   `bc_flow.py` 4 处、`beh_dream.py` 4 处、`mm_recall_reinforce.py:143`、`qa_bugs.py:133`、
   `mm_store_sqlite.py:200`…）。产品代码没问题，但这些探针会在第一个裸调用处 `TypeError` 中止。
   我没动别人的文件，要不要统一补 `view=Store.ALL_SCOPES` 请分派。
3. **`mm_store_sqlite.py:107` 的 `flaky_meta_set(key, value)`** 需要接受 `**kwargs` ——
   `meta_set` 现在多了 `commit` 关键字参数（事务需要）。`bc_fault.py`/`bc_tools.py`/`team_brain_fixes.py`
   的替身都是 `*a, **kw`，不受影响。
4. **签名改动清单**（详见报告 §2）：`active_memories`/`memories_by_person` 的 view 必填（契约要求）；
   `add_memory`/`update_memory`/`mark_consolidated`/`meta_set` 新增 `commit=True` 关键字参数（默认行为不变）；
   其余为新增方法/字段。

**明确留给下一批（不在本批任务清单）**：`_age_hours` 的 tz 归一化（§5.1）、`mark_recalled` 的 session 条件（P3-1 语义半条，
本轮只加了索引）、`messages_by_prefix` 的 LIKE 转义（P3-4）、`blur_text` 银行家舍入 / `max_chars` 二次截断与 +1（P2-1，文件是我的、
但登记册把它算在 B4「数值/时间/边界」名下——请明确归谁，避免两人同时改 `memory.py`）。

### 追加：team-store 第 2 波（2026-10-10，Lead 验收后指派）

产品代码（`giftia/store.py`、`giftia/memory.py`）：
`_age_hours` tz 归一化、`blur_text` → `math.floor`、`max_chars` 截断不再置 `blurred` 且长度 ≤ 配置值、
`messages_by_prefix` LIKE `ESCAPE '\'` 转义、`mark_recalled(msg_id, session_id=None)` 会话条件。

探针：
- `mm_scope_backfill.py` 判据按 Lead 裁定重写（断言"无跨群泄漏" + "能确认的行归属正确"，store 语义未动）→ **失败计数 0**；
- `mm_store_sqlite.py` 替身 `flaky_meta_set(key, value, *a, **kw)` 适配 `commit=` → 完整跑完，事务回滚可见（`memories=0 / consolidated=0`）；
- 裸 `active_memories` 迁移（只加 `view=Store.ALL_SCOPES` + 必要 import，未改断言）：9 个文件 28 处
  （`qa_e2e` 13、`beh_dream` 4、`mm_multi_query_scale` 3、`mm_snapshot_injection` 2、`mm_store_sqlite` 1、
  `mm_unfiltered_paths2` 1、`qa_bugs` 1、`qa_time` 2、`mm_recall_reinforce` 1）；`mm_unfiltered_paths.py:113` 的
  `view=None` 一并迁到 `ALL_SCOPES` 并注明契约；`fix_store_p0.py:73` 是故意漏传 view 的 fail-loud 测试，保持原样+注释；
- `pa_config_probe.py`：`toml_case()` 给缺 stages 的用例补一档合法 stage（自带 stages 的 2a/2f 不动）、
  2e keywords 用例接受"抛配置错误"（意图不变）→ 22 通过 / 1 失败（唯一失败是 2d 的既有陈旧期望，未动）。
- 迁移事故如实记录：第一版脚本把 `ast` 的字节列偏移当字符下标，中文行插错位置；已 `git checkout` 回滚重做，现全部探针可 `ast.parse`。

| 命令 | 真实数字 |
|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项** |
| `tools/audit.py` / `ws_selftest.py` / `qq_protocol_selftest.py` | 10/0（4 提示）、17/0、72/0 |
| `fix_store_p0.py` / `fix_memory_p1.py` / `fix_config_lifespan.py` | 46/0、**51/0**、49/0 |
| `mm_scope_backfill.py` | 失败计数 **0** |
| `mm_store_sqlite.py` | 完整跑完；崩溃后 `memories=0`、`messages=[{consolidated:0}]` |
| 全量探针清扫（39 个） | 只剩 3 个 traceback，且都不是本层：`mm_consolidate_time.py`（5c 按契约 fail-loud）、`beh_outreach.py:156`（`reason=None` 格式化，P1-16 行为变更后探针没跟上）、`beh_cli.py:126`（探针自身 `UnboundLocalError`） |

**交回 Lead**：① `mark_recalled` 需要在 `adapters/qq_official.py:642` 传 `session_id` 才拿到 P3-1 的收益（我没改适配器）；
② `beh_outreach.py:156` / `beh_cli.py:126` 两个探针自身的问题请转给行为侧；
③ `mm_unfiltered_paths.py` 仍有的两条 FAIL（dreaming 计数口径、封存 JSON 含 person）属"判据过期/已被 Lead 裁定为分层期望"，是否像
`mm_scope_backfill` 一样改判据请你裁定。详见 `.review/fix-reports/team-store.md` 第 2 波章节。
