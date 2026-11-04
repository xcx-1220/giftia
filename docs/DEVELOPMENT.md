# 开发与使用

所有命令从仓库根目录执行。需要 Python 3.11+；Python 运行时仅用标准库。前端检查需要 Node.js，无需安装 npm 包。项目机制见[项目说明](PROJECT.md)，线上操作见[运行维护](OPERATIONS.md)。

## 本地启动

```powershell
python tools/demo_life.py
python tools/show_prompt.py
python run.py --mock
```

前两个工具用临时环境演示寿命和提示；`run.py` 使用项目 `data/`，Mock 只替换模型，不禁用数据写入、寿命和回收。需要完全独立体验时使用独立项目副本；不要把本地旧库上传覆盖生产，也不要让两个实例操作同一个正式库。

真实对话：在项目根 `.env` 中设置 `DEEPSEEK_API_KEY`，然后运行 `python run.py`。`.env` 被忽略；已有环境变量优先于文件。不要把真实凭据写入命令示例或提交。

```powershell
python run.py --once "在吗" --no-banner
python adapters/qq_official.py --web-port 8111 --web-only
```

网页需要 `AILA_WEB_SECRET`，访客另设 `AILA_WEB_FRIEND_PASS`。本机访问 `http://127.0.0.1:8111`；公网通过 HTTPS/Tunnel。上述两个命令可能调用真实模型并写入本地数据，不能当离线自测运行。启动参数用 `--help` 查看。

`--lifespan-hours`、`/time`、`/recycle` 和 `--reset` 用于演示或管理，会改变状态；`--reset --yes` 清空重来，不属于普通启动流程。短寿命演示优先使用 `demo_life.py`。

## 配置层次

| 文件 / 来源 | 用途 |
|---|---|
| `config/settings.toml` | 模型、记忆、寿命、关系、心情和主动联系默认值 |
| `config/persona.toml` | 人物 core、兴趣、问候、关系/心情表达 |
| 同名 `*.local.toml` | 个人覆盖，只写需覆盖的键；参考仓库 `.example`，部署应保留 |
| `.env` / 环境变量 | 凭据与接入参数；配置变化通常需要重启进程 |

本地人物默认是未发布的 B 候选；线上版本见基线记录。维护参数以源码与实际合并配置为准，不复制旧文档里的临时值。寿命已写入数据库的实例不随默认值修改而变更。

调优时区分：`top_k` 是数量预算，`min_score` 是相对门槛，`min_raw_score` 提供绝对分数门槛，`relative_cutoff` 是相对最佳候选的裁剪。改变退化和检索应在合成样本上验证，不能把“阈值更高”直接等同于更准确。

## CLI / QQ 指令

以下管理指令在 CLI 和相应 QQ 管理路径使用，**网页没有斜杠指令分发**。权限及参数以 `giftia/cli.py` 与适配器为准。

| 指令 | 用途 |
|---|---|
| `/status`、`/bond`、`/mood` | 寿命、关系、心情；`/mood 名字` 会修改心情 |
| `/name [称呼]`、`/people`、`/profile [名字]` | 称呼、认识的人与搭档视角档案 |
| `/days`、`/dream [now]` | 纪念日与梦；`now` 会生成梦 |
| `/outreach` | 主动联系预览，不实际发送，但真实模式下可能生成内容 |
| `/prompt 文本`、`/recall 文本`、`/memories` | 查看提示、召回与原始记忆；输出可能含私密内容 |
| `/remember`、`/reconcile` | 整理/合并记忆，可能调用模型并写库 |
| `/impress 名字 内容`、`/forget 编号` | 补充印象、标记遗忘 |
| `/diary [内容]`、`/memoir` | 搭档日志与回收产物；日记不注入角色 |
| `/time +24`、`/time reset` | 改虚拟时钟 / 归零 |
| `/recycle`、`/recycle yes` | 预览确认 / 提前回收换代 |
| `/quit` | 退出并执行适用的整理收尾 |

QQ 的绑定、群聊、触发聚批和发送逻辑保留在适配器。恢复渠道前按路线图 Q1 核验平台，不把归档中的频控数字当当前平台保证。

## 离线检查

```powershell
python -X utf8 -B tools/selftest.py
python -X utf8 -B tools/quality_selftest.py
python -X utf8 -B tools/isla_eval_selftest.py
python -X utf8 -B tools/web_selftest.py
python -X utf8 -B tools/ws_selftest.py
python -X utf8 -B tools/qq_protocol_selftest.py
python -X utf8 -B tools/audit.py
node tools/web_quality_selftest.js
node tools/chat_time_selftest.js
```

套件使用临时/合成数据，不需要真实模型或 QQ 凭据；Web/协议自测需要本机回环套接字。沙箱阻止套接字时先明确环境限制，不能把无法执行写成通过。检查项数以脚本实际输出为准，最近结果与审计已知失败见[基线记录](records/2026-11-04-baseline.md)。

按变更风险选择检查：纯文档改动校验链接、内容保留、状态一致性与 Git 差异；人物文案检查配置解析、最终提示和真实样本；状态/身份改动运行相关机制回归；部署器改动补 Linux 隔离发布模拟。历史 `.review/checks/fix_readme.py` 等是特定旧版本修补脚本，不对当前文档直接重跑。

## 人物与模型评测

```powershell
python tools/chat_quality_eval.py --limit 24 --output .review/tmp/quality-mock.json
python tools/isla_character_eval.py --limit 12 --output .review/tmp/isla-mock.json
```

这两个工具默认 Mock；`--live` 才使用真实模型。`tools/live_check.py` 和 `tools/judge_check.py` 涉及真实 API，不能混进默认离线检查。实验范围、预算、数据隔离和验收统一按路线图第 7 节，不直接照抄历史命令追加付费请求。

原始输出放本地忽略目录；长期保存的脱敏汇总进入 `docs/records/`，注明版本、有效配置、请求数、失败和局限。不得把人工写的示例当模型实测。
