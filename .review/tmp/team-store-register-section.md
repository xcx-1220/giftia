
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
