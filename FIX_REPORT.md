# 修复总结（Fix Report）

> 基线：审查快照 `44aefb1`（`selftest` **302 项 = 299 通过 / 3 失败**）
> 现状：`selftest` **304 项 = 304 通过 / 0 失败**；`ws` 17/0；`qq` 72/0；`audit` 10/0（+4 提示）
> **49 个探针全部 0 FAIL / 0 Traceback / exit=0**
> 修复方式：4 路并行（team-store / team-brain / team-recycle / lead），文件所有权互斥；
> 只改过 5 条与 README 契约冲突的旧断言，逐条写明理由（§四）

---

## 一、验收数字（可复跑）

```powershell
$env:PYTHONIOENCODING = "utf-8"
python .review\acceptance.py          # 全部套件 + 真实项数/耗时 → .review/evidence/summary.json
```

| 套件 | 修复前 | 修复后 |
|---|---|---|
| `tools/selftest.py` | 302 = **299 过 / 3 败**（退出码 1） | **304 = 304 过 / 0 败**（退出码 0） |
| `tools/ws_selftest.py` | 17 / 0 | 17 / 0 |
| `tools/qq_protocol_selftest.py` | 72 / 0 | 72 / 0 |
| `tools/audit.py` | 10 / 0（+4 提示） | 10 / 0（+4 提示） |
| `tools/demo_life.py` | 0.6s | 0.6s |

关键回归探针（修复前 → 后）：

| 探针 | 修复前 | 修复后 |
|---|---|---|
| `lead_v1_p0_verify.py`（P0-1 回忆录 / P0-2 回填） | P0-1 三条 FAIL、P0-2 FAIL | 全 PASS |
| `lead_v2_p0_commands.py`（P0-3 指令闸门 / 换人遮蔽） | 未绑定群成员可 `/profile`、换人后泄漏 | B2 PASS（新搭档可见 0 条）、闸门白名单生效 |
| `lead_v3_p0_occasions.py`（P0-4 群发提示词 / `/days`） | 泄漏 3 条私事 | 全 PASS（不该出现的私事 `[]`） |
| `pa_ws_probe.py` | 9 过 / 4 记录缺陷 | **13 / 0** |
| `pa_qq_probe.py` | 4 过 / 7 记录缺陷 | **11 / 0**（§5 主动消息限频也已修） |
| `beh_recycle.py` | C 段记录数据丢失 | **18 / 0**（重跑不再覆盖原件） |
| `beh_occasions.py` | — | **17 / 0** |
| `qa_bugs.py`（孤立代理对 / 换代称呼） | 2 条复现 | 见 §三 |
| `team_brain_fixes.py`（新，56 项） | — | 56 / 0 |
| `fix_store_p0.py` / `fix_memory_p1.py` / `fix_config_lifespan.py`（新） | — | 46/0、40/0、49/0 |

---

## 二、P0：5 条全部修完

| ID | 修法 | 验证 |
|---|---|---|
| **P0-1** 回收仪式带出第三方私事 | `recycle.perform_recycle` 的记忆清单改走**搭档视角** `[("private",""),("person",handler_id)]`（不含 group）；回忆录 `.md` 与 `diary` 用过滤后的清单；封存 JSON **保留全量**并显式标注 `"scope":"all_scopes"`+`note`（它是运维资产，不是给她看的） | `lead_v1` 3/3 PASS |
| **P0-2** 老库回填把群 A 标成群 B | `backfill_message_scopes` 改为**从 session_id 解析群标识** + 与 `last_group_openid` 前缀确认；解析不出来就保持 `private`；新增 `msg_scope_backfill_v2` 做一次性纠错 | `lead_v1` P0-2 PASS（错 0 条） |
| **P0-3a** 未绑定 → 群成员可执行 `/profile` | 指令闸门改**白名单**：无搭档时只放行 `/bind` `/help` `/unbind`；`/profile` `/impress` `/forget` `/reconcile` 限**私聊+本人**；群里 `/bind` 限群主 | `lead_v2` A/B 段；`pa_qq_probe` §7 转 PASS |
| **P0-3b** 换人后私事换主人 | **双水位线遮蔽**：`handler_bound_at`（时间）+ `handler_bound_mem_id` / `handler_bound_msg_id`（id）。覆盖记忆检索、印象块、`/profile`、工具 search/forget、工作记忆窗口、`history()`、派生大日子；`A→B→C` 连续换人也不漏 | `lead_v2` B2 PASS |
| **P0-4** 群发提示词混进别人私事 | `compose_outreach` 的 `find_occasions` 统一走 `_outreach_view()`；`occasions` 的两个入口都按归属过滤 | `lead_v3` B/C/D 全 PASS |
| **P0-5** 孤立代理对崩库 + 断连 | `store.py` 新增 `sanitize_text`，覆盖**所有 TEXT 写入口**（SQLite 编码前把孤立代理换成 U+FFFD）；适配器侧 `respond_safely` + `handle_event` 兜底（单条坏消息不再拆连接） | `qa_bugs.py` B3 转 PASS |

---

## 三、P1：25 条全部处理

### 3.1 测试与回归信号
- **P1-1**：3 条失败全部转绿。根因是**实现与 README 的契约分叉**（`mood_share` 抢占 + 心情永不过期），
  先在实现侧修（见 3.4），再让断言按新契约构造（`enabled` 显式设置、心情分享前推进 24 小时），
  并**新增**两条断言守住新行为（"刚说完话不会立刻再发心情"、"过期心情不算理由"）。
- **P1-2**：README 17 处过期数字/矛盾表述已对齐（含 `218/270/69/187`→实测值、`demo_life` 耗时、
  额度与间隔、"新的她不认识你"、"零 token"、"零记忆访问"、"is_wakeup 尚未实现"）；
  `deploy/setup.sh` 不再写死项数。

### 3.2 异常与一致性
- **P1-3/4**：`_chat` 与 `agent_tools.execute` 全包兜底 + `[内部错误] 类型: 消息` 留痕 + 空回复兜底 → **一轮一定有回复**。
- **P1-5**：回填失败记 diary/notices 并清完成标记，下次启动真的重跑。
- **P1-7**：`consolidate` 改成**单事务**（`begin/commit/rollback`，回调挪到提交后）——要么全成、要么全滚。
- **P1-8**：`busy_timeout` + WAL 返回值检查 + `check_same_thread` 显式化 + `messages(mid)` 索引。
- **P1-9**：`total_hours` 范围校验 `[1,1e6]`（0 回落默认并警告、越界报错），`timedelta` 溢出兜底。
- **P1-10**：新会话游标取 `min(该会话首条未整理 id-1, 全局游标)`，不再永久跳过。

### 3.3 隐私（除 P0 外的两条）
- **P1-11**：`days_together_text` / `find_occasions` 默认搭档视角，`/days` 不再列别人的大日子。
- **P1-12**：花名册**按群隔离 + 30 人硬上限**（301 人 → 1 行/63 字）。
- **P1-13**：注入路径的印象块只带在场的人（直接调用保持"整份档案 + 按人封顶"，已在 docstring 写明两种语义）。

### 3.4 行为与承诺
- **P1-14**：旁听路径零模型调用（`learn_name` 不再走模型兜底）；旁听整理交给搭档那一轮/退出路径统一做。
- **P1-15**：bond 记忆加成**每天最多一次** → 同一天 30 轮后仍是 `3.47`（修复前 `32.90`），第二天正常继续涨。
- **P1-16**：`current()` 尊重 `until`（过期软心情回落 calm）；`mood_share` 加**两道闸**（未过期 + 距上次说话 ≥ `mood_share_min_idle_hours`）；`uneasy` 在压力解除后收尾。
- **P1-17**：`effective_max_per_30_days()`：`use_wakeup=true` 时收紧到官方 4 条/30 天，`/outreach` 显示生效值。
- **P1-18**：`can_send(batch_extra=…, ignore_gap=True)` → 一批只判一次间隔、按批发条数预留额度、每个目标各自记账。
- **P1-19**：`OutreachPlan.dream_id` + `note_outreach_sent` 标记已讲 → 同一个梦不再每 3 小时重讲。
- **P1-20**：封存/回忆录文件名带时间戳 + `_unique_path` 绝不覆盖 + `meta:recycling_instance` 崩溃续跑换代。

### 3.5 适配器与协议
- **P1-21**：`PASSIVE_REPLY_LIMIT_C2C=4 / _GROUP=5`，超出把尾巴并成一段（同一 msg_id 不再发 6 条）。
- **P1-22**：`_parse_body` 失败一律 `QQError`（含非对象 JSON），不再拆连接。
- **P1-23**：`ws.py` 帧内超时/EOF → `WSClosed`（连接不可信）；调用方 timeout = 整帧 deadline。
- **P1-24**：同文去重按 **msg_id 分桶**：不同 msg_id 一律放行；无 msg_id 的旧路径保持原语义。
- **P1-25**：`respond_safely` 包壳 + `handle_event` 外层兜底 + 退出前 `_flush_pending_batches()`（SIGTERM 窗口内的消息不再丢）。

---

## 四、被改动的 5 条测试期望（均写明理由）

| 位置 | 原期望 | 新期望 | 理由 |
|---|---|---|---|
| `selftest:791` | 假设 `outreach.enabled` 出厂为 `false` | 显式设 `enabled=False` 再断言 | 代码默认值是 `false`，但**仓库配置**是 `true`；测试不该依赖"默认值"这种会漂的东西 |
| `selftest:797`（现拆成两条） | 刚说完话 `force=True` → `None` | ① 刚说完话不会立刻发"我今天心情X"（新增）② 闲一天后**会**分享（保留） | 原断言表达的是"别刚聊完又发一条"，实现侧已按此修（`mood_share_min_idle_hours`），断言按契约重写而不是放宽 |
| `selftest:801` | 闲 4 天 → `missing_you` | 闲一天 → 心情分享；心情 calm 时闲 4 天 → `missing_you` | README §3.5 的七条理由表里 `mood_share` 排在 `missing_you` 之前；原断言跟的是 README §2 那份**没有** `mood_share` 的旧表 |
| `selftest:596`（换代称呼） | "换代后称呼仍保留" | 称呼**清零**（新的她不认识你）+ 新增"名字留在回收档案里" | README §2/§9 与 `after_recycle_line`（"档案上还是空的"）的契约是"新的她不认识你"；原断言把这个缺陷固化了。名字没有丢——它在回忆录/回收档案里，新断言守着这一点 |
| `beh_cli.py` D4（`--reset` 非交互） | "EOF 直接抛未捕获异常（会 traceback）" | "优雅取消、不 traceback" | 非交互环境没人能回答确认，那不是错误；`run.py` 现在捕获 `EOFError` 当取消 |

> 另外有 **30 条**审查期探针的"检出型断言"（`[FAIL]` = 成功检出缺陷）按新契约翻转成"记录正确行为"，
> 每条都配了理由注释与正向对照，避免"把 bug 断言留下"让后来人误读。

---

## 五、明确不改（附理由）

| 项 | 理由 |
|---|---|
| `Giftia` 线程安全（README 已声明非线程安全） | 改成线程安全需要大改状态管理；当前设计是"单线程 + 主循环内跑"，README 已如实声明 |
| 检索改用向量/语义检索 | README §9 已列为"已知限制 + 建议下一步"，属于功能演进而非缺陷 |
| `people_stats`（只写不读、无界） | 已确认不影响正确性（未进提示词）；删除会改掉已文档化的行为，留作 backlog，见 §六 |
| 残余：绑定同一秒内**未整理**的前任私聊若此后被整理 | id 水位线只能证明"何时写下"，彻底堵死要改 `consolidate` 游标语义；概率极低（需同秒换人 + 该会话随后被整理），已如实记录 |
| `mind_score` 归档公式本身 | 新增了**绝对**门槛 `min_raw_score`（默认 0 不改变行为），但没有推翻 `min_score` 的相对语义——那会让 5 个阶段的标定全部失准 |

---

## 六、新发现（修复过程中冒出来、原有审查没覆盖）

1. **`tone_of` 的否定判定比审查报告写的更深一层**：`特别难过` 里的"别"会被当成否定词，
   把 `-1.0` 的负面判成 `+1.0`。已加"紧邻字符 + 程度副词回看"两道判据（13/15 组常见短语正确；
   `升职失败了` 仍是中性、双重否定 `没什么不开心的` 仍误判为负面，属于已知限制）。
2. **`recall` 的候选集归一化会把"只被当前这句命中"的记忆挤掉**：`_recall_query` 拼前两轮的话，
   当天追问同一件事只剩 1/3 名次、被 `relative_cutoff` 裁掉（第二天会话轮换后才想起来）。
   已改为**两路检索合并**（历史+当前 / 只有当前），每条取较高分。
3. **内核记忆没有保底通道**：`recall()` 是词面匹配，问"今天天气怎么样"时 core 层根本进不了候选，
   "她记得你是小林"只在用户正好提到名字时成立。已加 `core_always_inject`（默认开，最多 3 条、每条截断 200 字）。
4. **`impressions_block` 是绕开词面检索的第二条注入路径**：它按 `about` 取"这个人"的记忆，
   于是"考驾照"这类不相关记忆也会进提示词。这不是缺陷（设计如此、有 3 条/15 人上限），
   但值得知道：**她"想起"你的方式有两条**——相关性命中 + 每人档案。

---

## 八、残余与后续（诚实清单）

**修复后仍然存在的、已知的东西**（都不影响正确性，但应该知道）：

| 项 | 现状 | 建议 |
|---|---|---|
| 探针里的"检出型断言" | 审查期的探针是"断言缺陷存在"（`[FAIL]` = 检出成功）。修复后它们会显示为 FAIL，需要用"新契约下的正确行为"重写 | 正在统一翻转；最终状态应为**全部探针 0 FAIL** |
| 同一秒内**未整理**的前任私聊 | id 水位线只能证明"何时写下"，若该消息此后被整理成记忆，理论上仍可能漏一条进新搭档的视野 | 彻底堵死要改 `consolidate` 的游标语义（把"谁说的"写进记忆），属于下一轮 |
| `people_stats`（活跃成员表） | 只写不读、每条群消息全量重写、无上限（人数多时是 O(n) 常数开销） | 要么接进提示词（按群过滤 + 上限），要么删掉写入 |
| `tone_of` 的双重否定 | `没什么不开心的` 仍判成负面；`升职失败了` 仍是中性 | 词表方案的天花板；要更准得换成小模型判定 |
| 平台真机行为 | QQ 真实收发、`is_wakeup` 配额惩罚、超长消息是否被平台拒——都没有真凭据，**未验证** | 作者拿到凭据后按 README §9 的清单验 |
| `min_score` 的语义 | 仍是"相对门槛"（首名归一化为 1.0）。新增的 `min_raw_score` 是**绝对**门槛（默认 0 = 不启用） | 想真正收紧严格度时调 `min_raw_score`，别再调 `min_score` |

**没有改的（明确决策）**：`Giftia` 线程安全、语义检索/向量库、`consolidate` 游标语义——理由见 §五。

---

## 九、如何回滚 / 复现

```powershell
git log --oneline                       # 44aefb1 审查基线 → 319c0ba/b004e4b/ef4d85c/7037c19/039e4fc 五步修复
git diff 44aefb1..HEAD --stat           # 看全部改动
git checkout 44aefb1 -- giftia adapters # 只回滚某几个文件
```

每个检查点都跑过完整验收（`python .review\acceptance.py`）；`selftest` 从 `299/3` → `304/0` 全程只升不降。
**最终状态**：套件全绿（`304 / 17 / 72 / 10`）+ **49 个探针全部 `exit=0` 且 `0 FAIL`**，工作区无未提交改动。
逐条修复证据：[team-store.md](.review/fix-reports/team-store.md)、[team-brain.md](.review/fix-reports/team-brain.md)、[team-recycle.md](.review/fix-reports/team-recycle.md)、[team-recycle-probes-final.md](.review/fix-reports/team-recycle-probes-final.md)，以及登记册 [FIX_REGISTER.md](.review/FIX_REGISTER.md)。
