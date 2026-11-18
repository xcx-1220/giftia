# 艾拉 Harness 阶段 4 验收记录

2026-11-16（Asia/Shanghai）。阶段 4 在 Windows 隔离环境通过。Git 起点是 `main` 的 `b3325f1` 加已有阶段 1～3 工作区。沿用 `isla-test`、Harness `0.2.2-rc.3`、Node `24.11.1`、艾拉 bundle `0.2.0` 和示例 bundle `0.1.0`。两个重新打包的归档 SHA-256 分别与阶段 2、3 记录一致。验收用 profile 给搭档启用示例工具。 [实施计划](../HARNESS_PLAN.md) · [阶段 1 环境](2026-11-16-harness-stage1.md) · [阶段 2 角色链路](2026-11-16-harness-stage2.md) · [阶段 3 工具和权限](2026-11-16-harness-stage3.md)

## 附件与网页方案

**采用应用网关。** 在锁定版的 `sdk-minimal` profile 上复用现有 stdio JSON-RPC `session/prompt` 的文本块接口。网关负责上传、PDF 解析、鉴权、来源和浏览器状态。按页提取的文本作为带页号的 JSON 数据块与用户问题一起送入 Harness。这样沿用原有角色回调和 agent loop，浏览器只依赖同源应用接口。 [网关](../../harness/web_gateway.py) · [Harness 调用](../../harness/stage1.py)

- 隔离网页使用合成凭据和同源 Cookie。服务端从当前身份、艾拉实例、角色绑定和用途派生会话标识，并独立签发附件 ID。聊天、历史、来源页读取、重试和取消都验证当前会话；提交携带会话标识以拒绝过时页面上的操作。
- PDF 提取在子进程执行，用 `pypdf 6.17.0` 读取文本层。每份上限 2 MiB、12 页、16,000 字符和 6 秒，每会话最多保留 12 份，附件记录有效期为 24 小时。成功后丢弃原始 PDF 字节。浏览器通过鉴权接口查看编号对应的页文本。
- 文档问答使用独立上下文，只带同一附件最近六个成功问答和当前 PDF 页文本，模型请求不带工具。页码必须指向有效的非空页面。回答无依据时有固定的无结果文案。文档问答持久化在隔离的 `web.db`，无需推进 Giftia 角色或写入共同经历。普通聊天仍从 Giftia 的授权历史恢复，并投影 Harness 工具调用与结果的状态。
- PDF 提取和聊天先入持久队列，网页轮询相同会话的状态。浏览器可以取消上传、取消排队或执行中的提取、取消尚未进入提交的回复，并在失败后重试。服务重启后恢复待处理任务，已在 Giftia 提交的普通聊天沿用阶段 2 的请求 ID 去重。

当前证据覆盖本地文本 PDF、模拟回答、页号位置和来源访问。来源校验确认页号存在，真实模型引用是否支持对应陈述留待阶段 5 的使用验收。浏览器恢复已到达服务器的操作；服务端当前返回最近 120 个网页操作。Harness 本地 JSONL 保留了用于验收的 PDF 输入事件，正式环境的日志和附件保留策略在阶段 5 一起核对。 [网页源码](../../harness/web/index.html) · [持久状态](../../harness/web_state.py) · [PDF 提取](../../harness/pdf_pages.py)

## 集中验收结果

| 用例 | 实际结果 |
|---|---|
| `document` 跨页 PDF、来源和隔离 | 第 1 页读到 `ORBIT-41`，第 2 页读到 `LANTERN-72` 和 `16:30`。模型实际收到两个独立编号的页文本和当前问题，首答同时引用两页。重启后原回答和第 2 页可读，同一附件的追问输入包含之前问答。访客读取搭档来源、附带对方附件提问或取消对方附件均得到 404，错用搭档会话标识得到 409，访客历史为空。普通聊天的实际输入排除了文档、问答和资料标记，Giftia 消息、记忆和角色账本也没有文档问答。 |
| `operations` 取消和失败重试 | 使用延迟模拟请求稳定验证排队附件取消，已取消的页不可读。生成中的普通回复取消后，角色账本没有该回合。破损 PDF 失败并可发起重试，同一破损文件再次失败，合法文件可重新上传。模拟模型引用不存在的第 99 页，网页任务置为失败并保留重试入口，使用原任务 ID 重试后成功引用第 2 页。 |
| `recovery` 待处理任务跨进程重启 | 模拟模型已收到第一次请求时重启隔离服务。重新提交相同请求 ID 只生成一笔 Giftia 提交，换文本复用 ID 返回 409。模拟模型看到示例工具，Harness 写入 `tool/call`、`tool/result`，网页历史只收到工具名称及运行/完成状态。 |
| 手机网页 | Edge 的 360 CSS 像素视口通过普通消息、工具状态、上传断网失败后重试、跨页回答、点开第 2 页、继续追问、断线重连、刷新历史、无效页码失败后重试，以及退出搭档再登录访客检查。最后一次布局检查宽度仍为 360，确认附件切换按钮、引用标记和来源弹窗。 |

集中验收首次运行有两个用例在 Harness 初始化阶段超时，恢复用例已通过。为阶段 4 调整初始化等待，随后只复测 `document` 和 `operations`，两项通过。手机端视觉检查发现窄屏按钮换行、页码后的句号掉行，修正后仅复测对应布局和来源入口，复用原网页业务结果。

最终证据包含 **15 次模拟模型请求**。接口三组和浏览器组的 Giftia SQLite 与 `web.db` 均通过 `quick_check`。实际付费模型请求为 0。

## 复现与证据

从仓库根目录运行。依赖沿用阶段 1 的 Node/Harness 锁，网关需要 `pypdf`，合成 PDF 生成脚本使用 PyMuPDF。两个服务端口 `18111` 与 `18112` 仅绑定回环。为浏览器验收准备的新目录也包含合成登录凭据，请在该目录内运行生成的脚本。

```powershell
$env:Path = "$PWD\harness\.stage1\toolchain\node-v24.11.1-win-x64;$env:Path"
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $PWD.Path
python -m pip install -r harness/web-requirements.txt
python -m pip install PyMuPDF==1.27.2.2
python harness/verify_stage4.py
# 仅复测受影响的接口用例
python harness/verify_stage4.py --case document --case operations
# 为手机网页验收准备单独的 profile、凭据、模拟响应和 PDF
python harness/verify_stage4.py --prepare-browser
python harness/stage1.py --root $Stage4Lab serve-web
```

把 `$Stage4Lab` 指向 `--prepare-browser` 输出的绝对目录。用已安装的 Edge 和 Playwright CLI，在第二个 PowerShell 窗口执行：

```powershell
Set-Location $Stage4Lab
playwright-cli.cmd -s=isla-stage4 open http://127.0.0.1:18111 --browser=msedge --mobile
playwright-cli.cmd -s=isla-stage4 --raw run-code --filename browser-run.js
playwright-cli.cmd -s=isla-stage4 close
```

证据根为 `harness/.stage1/stage4-validation/`。接口证据中的 `api-trace.jsonl` 记录响应，`model-requests.jsonl` 记录模型输入，`dsh-home/sessions/**/session.v4.jsonl` 保存原生会话和工具事件。报告使用以下目录的通过结果：

- `a257633893/document/` 和 `a257633893/operations/` 保存文档、取消与重试复测。
- `e21af2ff82/recovery/` 保存集中验收中已通过的服务重启和工具事件用例。
- `34d5cc6d68/` 保存手机验收的 `browser-result.txt`、`browser-layout.txt`、`browser-phone.png`、`browser-source.png`、`browser-visitor.png`、服务日志及两页合成 PDF 与渲染图。

## 阶段 5 交接提示词

```text
请在 D:\ai chatbot 继续《艾拉原生接入 DeepSeek Harness 实施计划》的阶段 5「云端试用与切换准备」。

先核对 Git、docs/HARNESS_PLAN.md、docs/records/2026-11-16-harness-stage4.md、docs/OPERATIONS.md 和路线图 R1/R2。保留工作区改动，从 Harness 0.2.2-rc.3、isla-test、艾拉 0.2.0、示例 0.1.0 及应用网关 PDF 路径接续，复用前三阶段和阶段 4 的可用证据。

复核腾讯云隔离用户、回环端口、同机现有服务与基线资源。在隔离服务中核对空闲与文本 PDF 问答的内存、磁盘、首字时间、整轮耗时和模型调用数，用实际浏览器和 Android TWA 验证身份、页码来源及断线恢复。先使用一致性备份演练旧数据读取、迁移、重复请求、回退与切换后新增聊天的保留路径，验证 Giftia 数据、Harness 会话日志和附件能分别备份与恢复。核对正式网关认证、附件和日志保留策略，并验证真实回答的文档引用准确性。真实模型沿用已明确额度，正式入口切换按 R2 的发布安排处理。

先实施再集中验收，修复后只复测受影响的用例。在 docs/records 记录基线、命令、资源测量、实际结果和证据，更新实施计划。不要在 ~/.codex/skills 下安装 node_modules。
```
