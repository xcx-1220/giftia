# 艾拉 Harness 阶段 5 真实模型与试用接续记录

2026-11-17（Asia/Shanghai）。继续使用 Harness `0.2.2-rc.3`、`isla-test`、艾拉 `0.2.0`、示例工具 `0.1.0` 和应用网关 PDF 路径。复用上一轮云端性能、Edge 浏览器、旧热备和三类数据恢复证据。Git 起点为 `main` 的 `b3325f1` 加未提交阶段工作。 [实施计划](../HARNESS_PLAN.md) · [云端隔离与恢复记录](2026-11-16-harness-stage5.md)

## 真实 PDF 引用通过

在腾讯云创建 `/opt/giftia-harness-test/stage5-20261117`，独立的冒烟和 R1 状态分别位于 `smoke` 与 `r1`。Harness 仍通过回环转发，转发服务使用现有 DeepSeek 账户的单独受控凭据和固定官方地址 `/anthropic/v1/messages`。两组使用不同的回环凭据和持久调用账本，请求发出前记账。冒烟上限 3 次，失败也占一次。离线验证了重启后账本仍拦截第 4 次请求。模型使用 `deepseek-flash`、温度 1.15、思考关闭、上限 900 输出 tokens，Harness 自动重试关闭。 [DeepSeek Messages 文档](https://api-docs.deepseek.com/guides/anthropic_api/)

| 用例 | 对照 PDF 的核验 | 首字 / Harness 整轮 / 网关完成 | 输入 / 输出 tokens |
|---|---|---:|---:|
| 跨页 | North Lab 对应第 1 页 `ORBIT-41`；Return desk 对应第 2 页 `LANTERN-72`，两个陈述分别引用正确页面。 | 489 / 802 / 868.2 ms | 276 / 36 |
| 同附件追问 | 回复 `16:30 [p.2]`，与第 2 页的归还截止时间一致。 | 580 / 715 / 745.9 ms | 335 / 8 |
| 无答案 | 两页均无联系电话，回复「资料中未找到相关信息。」 | 823 / 978 / 1,115.4 ms | 353 / 6 |

共 **3 次实际请求、0 次失败、0 次重试**。三个流均以 `end_turn` 正常完成，累计 **964 输入、50 输出 tokens**，缓存命中 0。Harness 首字取原生会话首个正文事件，包含网关到模型的调用路径。供应商回传模型别名和消息 ID，具体结果与用量已留档。两个来源页以未授权访客请求均得 404。冒烟单元验收后已停止。 [验收脚本](../../harness/stage5_live_probe.py) · [调用账本实现](../../harness/stage5_relay.py)

## R1 试用入口

候选已冻结并在云端运行，网页和转发监听 `127.0.0.1:18141/18142`。在 Windows 仓库根目录执行：

```powershell
.\harness\open_r1.ps1
```

脚本建立 SSH 转发并打开网页，将搭档试用凭据放入剪贴板。在登录框粘贴，并保持 PowerShell 窗口打开。用 `-Role guest` 可打开访客试用，切换身份前先退出网页。用 `-Probe` 只核对登录，搭档与访客均已从本机通过该探针，初始历史各 0 条。

R1 独立上限为 **60 次实际提供商请求**。最多 7 天，从第一笔模型尝试起计。工具调用引发的后续请求、失败和重试均占额度。交接时账本为 0，尚无正常对话样本。用户可在 `harness/.stage1/stage5-validation/20261117-live/r1-feedback.md` 记录日期、身份、场景、观察及复现条件。3～7 天观察从正常使用开始，覆盖日常、重逢、纠正、分歧、告别和旧事，之后再评定人物和连续性。 [试用入口脚本](../../harness/open_r1.ps1) · [R1/R2 验收标准](../ROADMAP.md)

后续验收结论为 **R1 通过，由用户直接确认**。试用天数、对话数和分场景结果待补充。

## Android TWA

已复用正式域名关联文件可读的上一轮证据。本机本轮仍未发现连接的 Android 真机、可用 ADB 或仓库内已签名测试 APK。当前试用入口通过本机 SSH 回环提供，真机验收需要能访问隔离 HTTPS 入口的已签名测试壳。设备就绪后依次检查搭档登录、PDF 上传、第 2 页来源、断线及重启恢复，再以访客确认历史和附件隔离。 [Android 维护](../ANDROID.md)

## R2 发布检查

| 检查 | 本轮证据与发布验证 |
|---|---|
| 正式认证与身份迁移 | 复用正式搭档、访客和推送权限的实测状态码。本轮 R1 两角色以隔离凭据各自登录。切流候选需绑定已有搭档标识与访客分区，实测过期会话、角色更替、身份撤销与旧令牌拒绝，HTTPS 下核对 Secure、HttpOnly 和同源策略，再验证限流。 |
| 历史和待处理请求 | 线上旧账本只读抽样有 7 条 completed。切流窗口应暂停新提交并重新核对旧队列，给 queued/running 和新网关的 committing 定义明确的完成、终止或可重提状态。旧浏览器本地历史、服务端历史和请求标识分别核验，使用同一请求重复提交验证跨网关去重。 |
| 附件与 Harness 日志 | 现有附件记录有效期 24 小时，引用链可在授权会话核对。R2 要确定正文、页文本、Harness 本地 JSONL、WAL 与恢复快照的保留期限。验收应覆盖到期与主动删除，以及从备份恢复后，已撤销的来源仍无法通过网页、旧会话或模型重新读取。 |
| 回退后新聊天 | 复用上一轮旧版 Store 能读取候选期间新增两条合成消息的证据。R2 仍需从旧网页及设备端核对可见历史，并验证回退前后对未确认请求的交付和去重。 |

本轮核对正式源站 8111 健康，`giftia-qq` 和 `cloudflared` PID 与上一轮一致，重启数均为 0。原阶段 5 模拟单元保持运行。正式域名、Tunnel 和生产数据沿用现有部署，切流仍按 R2 另行确认。 [运行维护](../OPERATIONS.md)

## 复现与证据

沿用上一轮打包与依赖说明。使用 `stage5_package.py` 生成候选归档，在新隔离根准备后，分别运行 `stage5_live_cloud.sh BASE smoke prepare/start` 与 `stage5_live_cloud.sh BASE r1 prepare/start`。重启已有试用只用 `start`，保留 `r1` 状态和调用账本。 `stage5_live_probe.py --root BASE/smoke --pdf BASE/synthetic-reference.pdf` 仅用于新建并单独计额的三请求冒烟。

- Windows 证据位于 `harness/.stage1/stage5-validation/20261117-live/`，包括 `smoke.txt`、`r1-entry.txt`、`post-host.txt` 和候选归档。已部署归档 SHA-256 为 `e0e624deddadf62a7f8f046c2a1e99b027a6ef799d9211ca9a16c3b15777a586`。
- 云端冒烟证据位于 `smoke/api-trace.jsonl`、`smoke/dsh-home/sessions` 和 `/var/lib/giftia-harness-smoke-relay/usage.db`。R1 计数在 `/var/lib/giftia-harness-r1-relay/usage.db`，仅用元数据汇总观察期与成功率。
