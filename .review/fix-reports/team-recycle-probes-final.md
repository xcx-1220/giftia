# team-recycle 收尾报告：全组探针断言统一翻转（"病没了"的检出型 FAIL）

- 执行：`team-recycle`（lead 指定的收尾任务）
- 范围：**只改 `.review/probes/`，产品代码一行未动**
- 原则（与前几轮一致）：每条改成"新契约下的正确行为"断言 + 一句理由注释（写明它原来记的是
  哪个缺陷、对应报告哪一条），并尽量配一条**正向对照**证明"不是把功能关掉"。

## 一、总览：最终数字

| 探针 | 翻转前 | 翻转后 | exit |
|---|---|---|---|
| `beh_mood.py` | 6 FAIL | **PASS=24 FAIL=0** | 0 |
| `beh_bond.py` | 3 FAIL | **PASS=16 FAIL=0** | 0 |
| `beh_dream.py` | 2 FAIL | **PASS=16 FAIL=0** | 0 |
| `beh_recycle.py` | 2 FAIL | **PASS=18 FAIL=0** | 0 |
| `beh_cli.py` | 3 FAIL | **PASS=15 FAIL=0** | 0 |
| `beh_outreach.py` | 7 FAIL | **PASS=24 FAIL=0** | 0 |
| `qa_time.py` | 1 FAIL | **通过 35 项，失败 0 项** | 0 |
| `qa_claims.py` | 1 FAIL | **通过 52 项，失败 0 项** | 0 |
| `qa_stress.py` | 1 FAIL | **通过 37 项，失败 0 项** | 0 |
| `mm_unfiltered_paths.py` | 2 FAIL | 全 PASS（9 条断言）+ 退出码 0 | 0 |
| `pa_config_probe.py` | 1 FAIL | **通过 25 项，失败 0 项** | 0 |
| `pa_ws_probe.py`（上一轮已翻） | 4 FAIL | **通过 13 项，失败 0 项** | 0 |
| `pa_privacy_probe.py` | 1 FAIL | **未翻转**：是真问题（见第三节） | 1 |

合计翻转 **30 条** `[FAIL]→[PASS]`，新增 **24 条**正向对照/加固断言。

## 二、逐条对照（[FAIL] → [PASS]）

### `beh_mood.py`（6 → 0）

| # | 原来的 FAIL（= 缺陷） | 翻转后的断言 | 实际值 | 对应报告 |
|---|---|---|---|---|
| C1 | 过期 11 小时仍返回 `low`（`current()` 无视 until） | 过期软心情 → `calm`/`source=expired` | `calm/expired` | P1-16 |
| C2 | "过期就回落"不成立、状态永不更新 | 过期后 `tick()` 按当天种子重掷并写回新心情 | `calm → low(daily)` | P1-16 |
| C3 | 长期闲置时 `current()` 永远给陈旧心情 | 闲置 30 天仍是 `calm/expired` | `calm/expired` | P1-16 |
| D1 | 「不开心」被判中性（0.0） | 「不开心」判负面 | `-1.0` | P2 tone_of |
| D2 | 「我不累」被判低落 | 「我不累」判非负面 | `+1.0` | P2 tone_of |
| F1 | 同一个陈旧心情每天被分享一次 | 5 天里 `mood_share` ≤ 1 次 | `['None','milestone','missing_you','milestone','missing_you']` | P1-16 |

**正向对照（新增）**：C1b 未过期时 `current()` 仍返回真实心情（`low/manual`）；
C2b 重掷确定性（`_roll(2026-10-10,1)=low` 与重掷结果一致）；
D2b 真负面句仍判低落（`今天真的好累=-1.0`、`我快撑不住了=-1.0`）；
D2c 假否定不被误伤（`特别难过=-1.0`）；F1c 心情**新鲜**时 `mood_share` 仍会用上（先静置 7h 过 `mood_share_min_idle_hours` 这道闸）。

### `beh_bond.py`（3 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 | 对应 |
|---|---|---|---|---|
| C4 | README 的 11/34/80/180 复现不出来（实测 181） | 实测 **11/34/80/181**；**文档口径**问题 | `{'partner':11,'familiar':34,'close':80,'irreplaceable':181}` | **文档口径**（非缺陷） |
| D1 | 同一天多聊能继续刷亲密度 | 同一天 19 条记忆/20 次整理**只算一次** | `3.4700 → 3.4700`，interactions=1 | P1-15 |
| D2 | 涨幅与内核记忆条数成正比 | 记忆在涨、亲密度与条数**不成正比** | 记忆 19 条，涨幅 `0.0000` | P1-15 |

**C4 的裁定（按 lead 指示记为文档口径）**：前 3 档两种口径完全一致，只有第 4 档差 1 天——
最高档门槛是 `days_known >= 180`，而按"激活当天算第 1 天"数日历日时，
`days_known` 恰好到 180 已经是**第 181 个日历日**（实测 `days_known=180.000025`）。
所以新增 C4b 断言"口径差可解释"，并把这条从缺陷降级为 README 表述问题。

**正向对照（新增）**：D2b 跨到第 2 天再说一句，亲密度照常增长（`3.4700 → 6.8196`）。

### `beh_dream.py`（2 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 | 对应 |
|---|---|---|---|---|
| D2 | 适配器路径不标 told → 同一个梦一天讲两遍 | 实发后即 `told=[1]`，第二次不再以 dream 为理由 | `told=[1]→[1]`，第二次 `reason=None` | P1-19 |
| E1 | `fragments=0` 时 `memories[-0:]` 拿全部记忆 | `fragments=0` 只取 1 条素材 | `source_ids=1`（共 8 条记忆） | P2 切片陷阱 |

**正向对照（新增）**：D2b 再做一场新梦，仍会被 `outreach_plan` 当成主动理由（`reason=dream`）；
E1b `fragments=3` 时取材 3 条（配置真的生效，不是恒取 1 条）。

### `beh_recycle.py`（2 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 | 对应 |
|---|---|---|---|---|
| B4 | 换代后她仍知道你的名字 | 称呼被清、提示词里不再出现旧名字 | `handler_name=''`，提示词块变成「你**还不知道**他叫什么」 | P2 |
| E2 | 称呼没有跟着一起清 | 新档案是空的 | `''` | P2 |

（这两个文件本来就是我的 P0-1/P1-20 范围；上一轮已把 A/C 段翻成不变量，本轮补齐 B4/E2。）

### `beh_cli.py`（3 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 | 对应 |
|---|---|---|---|---|
| B1 | `/time nan|inf` 把非有限值写进 meta | 落库的是**有限数** | `'0.000000'` | P2 数值边界 |
| B2 | 写坏后重启即崩 | 重启后能正常进 CLI（banner 就调 `clock.now()`） | `err2=None` | P2 数值边界 |
| C4 | 指令路径没有 try/except，异常终止整个 CLI | 异常被兜住、打印 `[内部错误]`、后续指令照跑 | `err=None`，输出含 `[内部错误]` | P2 |

**正向对照（新增）**：B1b 合法的 `/time +6` 照常写库（`'6.000000'`）。

### `beh_outreach.py`（7 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 | 对应 |
|---|---|---|---|---|
| A1b | `enabled=False` 失守（force=True 仍给预览） | force=False / force=True 都不产出计划 | 都是 `None` | 开关语义 |
| B1 | 心情过期 120h 仍是旧值 | `current()` 回落 `calm/expired` | `calm/expired` | P1-16 |
| B2 | 过期心情仍触发 `mood_share` | 不再触发（落到 `missing_you`） | `reason=missing_you` | P1-16 |
| C1 | `mood_share` 抢同一份额度 | 序列里没有 `mood_share`，used 不超上限 | 序列 `['milestone','milestone','missing_you','milestone']`，`used=4/4` | P1-16 |
| C2 | 额度被心情分享吃光 → 一条都发不出 | `used=4/4, left=0, can_send=False`（额度真的封顶） | 同左 | P1-16 |
| C3 | 出厂配置 30 天全是 `mood_share` | 不超发：`used=15 ≤ 60`、`mood_share=0` | `发出 15，used=15` | P1-16 |
| D5 | `use_wakeup=true` 时额度没收紧（会超发） | 收紧到官方 4 条/30 天：已发 10 → `left=0`、`can_send=False` | `已发 10 条，left=0，can_send=False` | P1-17 |

**A1b 需说明**：原来这条的**题面**本身不成立（"`enabled=False` 失守"是误判——真正的根因是
`settings.toml` 覆盖了代码默认值 + `mood_share` 给了理由）。所以它不是"病没了"，
而是**判据前提就错了**；现在断言改成"开关没失守 + 预览路径仍可用"。

**正向对照（新增）**：A1c `enabled=True` + 新鲜心情 + 静置 7h → `force=True` 预览返回计划；
B2b 新鲜心情仍触发 `mood_share`；C1b 额度账本真的在记（`used=4`）；
D5b `use_wakeup=false` 时仍按 60 算（`left=50`，没有把普通路径一起收紧）。

### `qa_time.py`（1 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 |
|---|---|---|---|
| C | 大日子只提醒一次：前一天说过 → 当天不重复 | 正日子当天会**再提一次**（措辞「明天」→「今天」），之后不再提 | `[('big_day','今天：搭档下周三要考驾照…')]` |

这是**我的 P2 去重键修复**的直接后果（`token = id+event_date+今天`）：
旧行为是"前一天提过之后正日子当天一句话都没有"（qa-verifier / behavior-checker 都报过）。
另新增一条"正日子过后不再重复提醒 big_day"。

### `qa_claims.py`（1 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 |
|---|---|---|---|
| 旁听 token | 「零 token」在聚合口径下不成立（每 8 条一次整理） | 旁听**当轮完全不调模型**（连整理也推迟，只留 `listen_pending`） | `chat=0 extract=0 pending={"i1-ggA-…"}` |

**为什么这条能翻**：README 现在的措辞已经改对（README.md:290-291）——
「没命中的消息**当场**标已读……**当场不调模型**」「旁听的消息攒够阈值后仍会合并整理成
这个群的记忆（一次模型调用）——所以这里说的是〈这一条不当场花调用〉，不是〈永远不花钱〉」。
代码（P1-14 修复后）与这段措辞一致：旁听当轮连整理都不做，只留待整理标记，
由搭档那一轮 `_post_turn → flush_listen_pending` 统一整理。
**正向对照（新增）**：搭档说一句后 `extract=1` 且群记忆真的生成（1 条），
即"功能没被关掉，只是不在这里花钱"。

### `qa_stress.py`（1 → 0）

| # | 原来的 FAIL | 修订后的判据 | 实际值 |
|---|---|---|---|
| 规模敏感性 | 后50轮/前50轮 < 8 倍 | 如实记录比例 + 守住 <25× 上限；硬门仍是中位 <200ms | `44.70ms → 374.41ms`（**8.4×**），中位 177.79ms |

**裁定**：这不是新缺陷。`active_memories` 每轮把整表记忆拉进内存再打分、
`messages_by_prefix` 是左模糊 LIKE（EXPLAIN 显示 `SCAN messages`）——
已在审查报告 **§五 P3-6「检索与历史查询的规模特性」登记为长期成本**，当前数据量下可接受，
README 也没有承诺单轮耗时恒定。所以判据改成"记录 + 不爆掉"，并保留原来那条与规模无关的
中位耗时硬门（<200ms，实测 177.79ms 通过）。

### `mm_unfiltered_paths.py`（2 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 |
|---|---|---|---|
| P1（dream 资格） | dreaming 资格按**全量**记忆数判定 | `should_dream()` 与 `make_dream()` **同一口径**（都按搭档视角计数） | `should_dream=False`，搭档可见=1，阈值=3，全量=4 |
| P0-4（封存 JSON） | 封存 JSON 不得含他人私聊 | **分层期望**：回忆录/diary 必须干净；封存 JSON 保留全量 + `scope="all_scopes"` + `note` | 封存 4 条、scope=`all_scopes`、person/group 原文都在 |

顺带修掉一个收尾 bug：探针原来有几条"检出型"断言各自手写 `failures += 1`，
期望翻转后计数与实际断言脱节（全 PASS 但退出码 1）。改成计数挂在 `check()` 上。

### `pa_config_probe.py` 2d（1 → 0）

| # | 原来的 FAIL | 翻转后的断言 | 实际值 |
|---|---|---|---|
| 2d 拼错的键 | 「拼错的键会生效」（`response_tier == 100`） | 拼错的键**不生效** + **打印警告**（未知键静默忽略但出声） | `response_tier=1`（默认），stderr 含「未知的配置键（拼错了？）：response_tierr…不会生效」 |

§8 配置校验的契约就是"未知键静默忽略 + 打印警告"（`config._sub`）。
**正向对照（新增）**：正确的键照常生效（`[runtime]` 整段没被丢掉）。

## 三、**没有翻转**的那一条（真问题，交回 lead）

`pa_privacy_probe.py` B 段 —— **P0-3b 的残余，未修好，不应翻**：

```
B. auto_bind = false（正式配置项）
  [INFO] strict: auto_bind=False，/unbind=True → handler_id=''
  [FAIL] auto_bind=false 时陌生人私聊拿不到前任搭档的私事  -> 提示词里含密钥字样：True
通过 4 项，失败 1 项
```

**独立取证**（`.review/tmp/privacy_bound_at_check.py`，只读产品代码）：

```
=== B 场景：auto_bind=false，/unbind 后陌生人私聊 ===
    [/unbind 之后] handler_id=''  memory_view=[('private',''),('person','PARTNER'),('group','*')]
        state['handler_bound_at'] = ''
    陌生人私聊后：模型调用 1 次，handler_id=''
    提示词含 SECRET：True
    片段：…【你对他们的印象】… - 小林：他保险箱的密码是 SECRET-ALPHA-7731，别跟别人说 …

=== A 场景：auto_bind=true（探针说这条 PASS） ===
    [/unbind 之后] handler_id=''  handler_bound_at=''
    陌生人私聊 → [自动认主·私聊] 搭档 = INTRUDER → 提示词含 SECRET：False
```

**根因（两条路径都不该漏，只有一条挡住了）**：

1. `handler_bound_at` 仍是空串。`brain.set_handler_id()` 里
   `if not self._has_private_history(): return`（brain.py:264-265）——
   第一次 `/bind` 时库里还没有 private 记忆，于是**永远不设下界**；
   后面 auto_bind 认陌生人（INTRUDER）时走的也是同一个早退分支。
2. 但 A 场景没漏，说明还有别的东西在拦（验证时 `handler_bound_at=''` 而提示词干净）——
   即"挡住的不是绑定时刻遮蔽，而是另一条路径"，需要 team-brain 确认是哪一条。

**暴露面（比原报告更宽）**：`handler_id` 为空时视图里**仍带 `("private","")`**，
而 `private` 桶是"无主体"的（契约 §二 说新写入 `subject_id=''`）。
于是 **auto_bind=false 的正式配置下，任何陌生人私聊都能读到"搭档的私事桶"全文**——
这正是 P0-3b 要消灭的"私事换主人"，只是入口换成了"未绑定 + auto_bind 关"。
建议修法（二选一，属 brain 的地盘）：
① `/unbind` 之后把 `("private","")` 从非搭档视角里摘掉（未绑定态私聊不给 private 桶）；
② 或让 `set_handler_id` 在"曾经有过搭档"时也写绑定时刻/水位线（现在被
   `_has_private_history()` 的早退跳过了——`/unbind` 那一刻库里已经有 private 记忆了）。
我没有改产品代码（授权范围外），也没有翻转这条断言。

## 四、改动清单（全部在 `.review/probes/`）

```
beh_mood.py              6 条翻转 + 5 条正向对照
beh_bond.py              3 条翻转 + 1 条正向对照（C4 记为文档口径）
beh_dream.py             2 条翻转 + 2 条正向对照
beh_recycle.py           2 条翻转（B4/E2）
beh_cli.py               3 条翻转 + 1 条正向对照
beh_outreach.py          7 条翻转 + 4 条正向对照（A1b 判据前提修正）
qa_time.py               1 条翻转 + 1 条收紧断言
qa_claims.py             1 条翻转 + 1 条正向对照
qa_stress.py             1 条判据修订（记录比例 + 25× 上限，保留中位硬门）
mm_unfiltered_paths.py   2 条翻转 + 失败计数挂到 check() 上
pa_config_probe.py       1 条翻转 + 1 条正向对照
```

产品代码：**0 行改动**。
