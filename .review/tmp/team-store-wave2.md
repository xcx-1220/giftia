
---

# 第 2 波（Lead 2026-10-10 验收后追加）

Lead 决策：① `mm_scope_backfill.py` 改**探针判据**（不改 store 语义）；② 授权迁移 `.review/probes/*.py` 的全部裸
`active_memories` 调用；③ `mm_store_sqlite.py` 的替身适配 `*a, **kw`；④ B4 剩余几条（`_age_hours` tz、`mark_recalled`
会话条件、`messages_by_prefix` LIKE 转义、`blur_text` floor、`max_chars`）归我；`relative_cutoff/历史 query` 与
`core 保底注入` 归 Lead。

## 1. 产品代码（`giftia/store.py`、`giftia/memory.py`）

| 条目 | 改动 | 验证 |
|---|---|---|
| `_age_hours` tz 归一化（§5.1） | `fromisoformat` 后按 `human_age` 的办法对齐 tz（aware↔naive 两个方向都归一），不再让 tz-aware 时间戳把 `recall()` 打崩 | `fix_memory_p1.py` §8 两条 PASS |
| `blur_text` 舍入（P3-2） | `int(round(...))` → `math.floor(...)`，擦除量随 ratio 单调不回退 | `fix_memory_p1.py` §8：擦除点 `[0,1,1,1,1,1]` 单调 PASS；`mm_degradation.py` 仍 0 失败 |
| `max_chars` 截断（P2-1） | ① 截断**不再**把 `blurred` 置 True（`blurred` 只由 `blur_text` 决定，内核记忆不再被误标"已模糊"）② 长度真的不超过配置值（省略号 `……` 占 **2** 个字符，已算进预算；`max_chars<2` 的极端值也不超长） | `fix_memory_p1.py` §8 三条 PASS（`len<=max_chars`、`blurred=False`、`max_chars=1` 不超长） |
| `messages_by_prefix` LIKE 转义（P3-4） | `%`/`_`/`\` 改成 `ESCAPE '\'` 转义（以前是直接删掉 → 含 `_` 的会话前缀静默失配） | `fix_memory_p1.py` §8：`i1-pus_er-` 只命中带下划线的会话；`i1-%` 不再当通配符 |
| `mark_recalled` 会话条件（P3-1） | 新增**可选** `session_id`：传了就 `WHERE mid = ? AND session_id = ?`；不传保持旧行为（兼容既有调用方，`tools/selftest.py:1244` 不受影响） | `fix_memory_p1.py` §8：同 mid 两条分属 s1/s2，按 s1 撤回只标记 1 条；不传时仍标记 2 条 |

> **调用方提示**：`mark_recalled` 的会话条件要生效，**适配器侧得把 session 传进来**——
> `adapters/qq_official.py:642` 现在仍是 `mark_recalled(mid)`（没有 session）。那条路径拿到的是撤回事件
> （`event_type` 含 RECALL，payload 里有 `group_openid` / 用户 openid），我**没有改适配器**（不在我文件范围），
> 请 Lead 或适配器负责人补上：有会话就传 `session_id`，拿不到就沿用旧的全表行为。

## 2. 探针

- **`mm_scope_backfill.py` 判据修订**（Lead 裁定）：原来要求"每条 `-g` 行都正向归属"，与契约"对不上就保持
  private"冲突。新判据 = ① 没有任何行被归属到**别的群**（跨群泄漏）；② 能确认的行必须归属正确（用完整
  openid）。文件里写了修订理由，**store 语义一行未改**。结果：`回填探针失败计数：0`。
- **`mm_store_sqlite.py` 替身适配**：`flaky_meta_set(key, value, *a, **kw)` 并原样转发（`meta_set` 多了
  `commit=`）。它现在完整跑完并如实显示修复后的行为：崩溃后 `memories=0 条, messages=[{consolidated:0}]`。
- **裸 `active_memories` 迁移**：AST 级扫描（按 `view` 关键字/第二个位置参数判断），只加
  `view=Store.ALL_SCOPES` + 必要 import，**未改任何断言**。命中 9 个文件 28 处：
  `beh_dream.py` 4、`mm_multi_query_scale.py` 3、`mm_recall_reinforce.py` 1、`mm_snapshot_injection.py` 2、
  `mm_store_sqlite.py` 1、`mm_unfiltered_paths2.py` 1、`qa_bugs.py` 1、`qa_e2e.py` 13、`qa_time.py` 2。
  另外 `mm_unfiltered_paths.py:113` 的 `view=None`（"量一下全量"的用法，不是 None 语义测试）也迁到
  `ALL_SCOPES` 并注明契约；`fix_store_p0.py:73` 是**故意漏传 view** 的 fail-loud 测试，保持原样并加了注释。
  `bc_*` / `beh_*` / `lead_v*` / `pa_ws_probe` 等文件里的裸调用**在我动手前已由各自负责人迁完**。
- **`pa_config_probe.py` 两处适配**（§8 契约的后果，已在注释里写明）：
  ① `toml_case()` 给"只测某个字段"的最小配置补一档合法 `[[degradation.stages]]`（已经自带 stages 的 2a/2f 不动），
  否则会停在"缺 stages"的 fail-loud 上、盖住被测字段；② 2e 的 keywords 用例改为"抛配置错误 或 读成字符串列表"
  都算正确（逐字匹配 `('晚','安')` 仍判失败）——检查意图不变。
  现在它跑完：**22 项通过 / 1 项失败**，唯一失败是 2d 的既有陈旧期望（它断言"拼错的键要生效"，`response_tier == 100`），我没动它。

> 迁移过程说明（如实记录）：第一版迁移脚本把 `ast` 的**字节**列偏移当字符下标用，中文行里插错位置，
> 一度把 9 个探针插成语法错误。已全部 `git checkout` 回滚重做（改字符偏移换算 + 处理关键字参数），
> 现在全部探针 `ast.parse` 通过。

## 3. 第 2 波验证汇总（真实输出）

| 命令 | 结果 |
|---|---|
| `python .review\run_tests.py tools/selftest.py` | **通过 303 项，失败 0 项** |
| `python .review\run_tests.py tools/audit.py` | 通过 10 项，失败 0 项（4 条提示） |
| `python .review\run_tests.py tools/ws_selftest.py` / `qq_protocol_selftest.py` | 17/0、72/0 |
| `fix_store_p0.py` / `fix_memory_p1.py` / `fix_config_lifespan.py` | 46/0、**51/0**（新增 §8 十一条）、49/0 |
| `mm_scope_backfill.py` | **失败计数 0**（判据修订后） |
| `mm_store_sqlite.py` | 完整跑完；崩溃后 `memories=0 / consolidated=0`（事务生效） |
| `pa_config_probe.py` | 22 通过 / 1 失败（2d 陈旧期望） |
| 全量探针清扫（39 个） | 仅 3 个仍有 traceback，且都不是我的层：`mm_consolidate_time.py`（5c 按契约 fail-loud，预期）、`beh_outreach.py:156`（`reason=None` 格式化，P1-16 行为变更后探针没跟上）、`beh_cli.py:126`（探针自身 `UnboundLocalError`） |

## 4. 交回 Lead（第 2 波）

1. **`mark_recalled` 的调用方**：`adapters/qq_official.py:642` 需要传 `session_id` 才能拿到 P3-1 的收益（见上）。
2. **`beh_outreach.py:156`**：P1-16 之后 `plan()` 可能返回 `None`，探针的 `f"{r:<11}"` 会 `TypeError`；属行为侧探针格式化，我没动。
3. **`beh_cli.py:126`**：探针自身 `UnboundLocalError: first`，与本次改动无关。
4. **`mm_unfiltered_paths.py` 仍报两条 FAIL**，属"判据过期"而非回归：
   `P1 观察：dreaming 资格按全量记忆数判定`（它比的是两个计数，不是 `should_dream()` 的行为）、
   `P0-4 封存 JSON 不得含他人私聊内容`（Lead 已裁定封存 JSON 保留全量运维档案 = 分层期望）。要不要像
   `mm_scope_backfill` 一样改判据，请裁定。
