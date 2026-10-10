
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
