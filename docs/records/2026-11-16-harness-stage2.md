# 艾拉 Harness 阶段 2 验收记录

2026-11-16（Asia/Shanghai）。阶段 2 已在 Windows 隔离环境通过。Git 起点为 `main` 的 `b3325f1` 加阶段 1 工作区，沿用 `isla-test`、Harness `0.2.2-rc.3`、Node `24.11.1` 和阶段 1 合成 SQLite 夹具。此次 bundle 为 `giftia-isla-harness-bundle@0.2.0`，打包 SHA-256 `0f1a14887c72b60ac896f163a7172a0839521f16060917bebabb314547967579`。腾讯云继续保留已验收的阶段 1 环境。 [实施计划](../HARNESS_PLAN.md) · [阶段 1 的环境与运行边界](2026-11-16-harness-stage1.md)

## 一轮普通聊天

1. 回环网关从合成身份令牌得到主体，自行解析当前搭档、实例号和 `ordinary-chat` 用途。HTTP 请求只接受文本和请求 ID。角色服务将主体、用途、实例、绑定水位线和本次生成尝试关联到服务端分配的 Harness 会话。
2. Python 主线程复用 `Giftia` 的视角选择、关系及心情、B 人物提示、近期窗口、长期召回和来源记账。Cordis bundle 从私有回环回调取得授权上下文，通过原生提示注册和会话消息注入送入 Harness。近期用户与艾拉原话保留对应的 `user` 和 `assistant` 角色。
3. Harness agent loop 执行模型和工具循环。`remember`、`search`、`forget` 共用 Giftia 工具定义与执行规则，回调再次核对活动会话和搭档权限。每轮最多两次 `remember`，最后一个生成步骤收起工具。访客模型请求不包含这三项工具。
4. 网关收到 Harness 完成事件和最终文本后，在同一笔 SQLite 事务提交用户消息、回复、角色变化、工具写入和 `harness_turns` 账本。失败整笔回滚。仍在授权视角内的重复请求可直接取回已提交结果。请求 ID 换文、旧视角或已撤回的结果返回 409。

每次生成尝试从 Giftia 当前授权的数据新建会话。自动压缩关闭，Harness JSONL 保留作隔离验收记录，旧会话不会被重新装入模型。撤回同时标记该轮两条消息，并排除依赖被撤回证据的长期记忆。撤回和遗忘使用当前视角的历史水位线，保守丢弃此前窗口，以免旧回复里的转述残留。遗忘成功后，bundle 在下一次模型调用前结束旧会话，再从更新后的数据生成本轮回复。重复撤回同一轮保留之后的新上下文。

到期轮由 Harness 完成最后一条普通回复。账本记录待回收状态，提交后由现有回收逻辑写合成档案并换代。若进程在实例号推进后停止，重启会补完绑定、称呼、关系和心情重置。新实例拒绝旧请求结果并重新建立授权窗口。

## 集中验收

| 用例 | 实际证据 |
|---|---|
| `dialogue` 普通聊天、人物和关系 | 模拟服务收到 B 人物核心文本、已有长期记忆、近期用户和艾拉消息。实测 `deepseek-flash`、温度 `1.15`、`max_tokens=900`、思考关闭。同日重复提交复用结果，跨虚拟日的有效对话推进关系。缺少凭据或在请求正文伪造身份会被拒绝。 |
| `tools` 新写、搜索及额度 | Harness 执行三次记忆写入请求、一次搜索和最后的无工具生成。库中只新增两条记忆，证据指向本轮用户消息。实际模型请求收到工具结果，最后一轮工具列表为空。 |
| `privacy` 访客隔离和撤回 | 两个访客与搭档复用了同一个请求 ID，账本按授权主体分开。两位访客各自只看到自己的原话，无法看到搭档私有称呼、旧原话、转述或工具记忆，也不推进搭档关系。撤回后这些内容从后续实际模型请求消失，旧回复返回 409，重复撤回保留新对话。 |
| `forgetting` 遗忘 | 第一次请求可以看到待遗忘的记忆。工具提交后，本轮重建请求和下一轮均没有旧记忆、转述或旧工具 ID。SQLite 只保留一条带遗忘标记的原始记忆。 |
| `lifecycle` 换搭档、到期和换任 | 同实例换搭档及到期换代后，模型请求都排除了旧称呼、旧原话和旧长期记忆。新实例拒绝旧请求。另构造实例号已推进但待回收标志还在的磁盘状态，重启后补完重置并撤销旧搭档的工具资格。 |
| `recovery` 失败、重试和重启 | 模拟模型在记忆工具执行后返回 503，SQLite 没有留下消息、记忆、关系推进或提交账本。重启后已完成请求可直接重放。失败轮用相同请求 ID 重试时重建授权历史，账本只有一行、消息和记忆各写一次。 |

首次集中验收发现旧历史先入会话会使 B 系统提示在 Messages 请求中落到历史末尾，清空称呼误用了拒绝空值的命名接口，关系测试也没有按原有规则推进到次日。修复后复测相应对话、撤回、生命周期和恢复场景，补测受逐步授权检查影响的工具和遗忘场景。最近一次针对撤回与生命周期的复测另覆盖重复撤回和换代中断。旧 `Giftia.handle()` 改动在集中验收中通过 `tools/selftest.py` 320 项、`tools/quality_selftest.py` 21 项和 `tools/isla_eval_selftest.py` 11 项。

模型实际请求与 SQLite 样本在 `harness/.stage1/stage2-validation/d1079328ab/`，撤回、换代中断补测在 `harness/.stage1/stage2-validation/b301331227/`。取每个用例的最后一次通过结果，共 29 次模拟请求、20 个已提交回合，六个 SQLite 样本均通过 `quick_check`。随后把 `0.2.0` 装入原有 `harness/.stage1/lab`，保留原合成库并通过一条普通聊天检查。各用例目录内有 `model-requests.jsonl`、`acceptance/` 日志及 `fixtures/giftia-test.db`。全程只调用本地模拟模型，真实 API 请求为 0。

## 从现有 profile 复现

从仓库根目录运行。`prepare` 复制当前 profile 补丁并保留已有合成库。`verify_stage2.py` 给各用例新建独立状态根，复测时可用一个或多个 `--case`。

```powershell
$env:Path = "$PWD\harness\.stage1\toolchain\node-v24.11.1-win-x64;$env:Path"
$env:PYTHONUTF8 = '1'
python harness/stage1.py prepare
python harness/stage1.py pack
python harness/stage1.py install
python harness/verify_stage2.py
python harness/stage1.py serve-role
```

运行 `serve-role` 后，从第二个 PowerShell 窗口发送一条普通聊天。`identity-tokens.json` 只位于隔离状态根。

```powershell
$tokens = Get-Content -Raw 'harness/.stage1/lab/identity-tokens.json' | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($tokens.'synthetic-owner')" }
$body = @{ requestId = [guid]::NewGuid().ToString(); text = '你好，艾拉。' } | ConvertTo-Json -Compress
Invoke-RestMethod 'http://127.0.0.1:18111/api/chat' -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body $body
```

阶段 2 只验证搭档和访客私聊。测试桥接暂不启用后台自动整理、查询改写、梦或主动推送，回收文案使用离线 Mock。合成令牌、管理动作和模拟请求记录供隔离验收使用。正式入口、数据库迁移和日志保留策略留给后续阶段。当前撤回覆盖有证据关联的记忆及近期转述，历史正式库中缺乏证据关联的内容仍需随阶段 5 的迁移一起核对。

阶段 3 可从当前 bundle 和 `isla-test` 继续。新增独立只读示例 bundle，用模型实际请求和工具结果验证安装、调用、停用、卸载，以及搭档与访客两种权限。卸载示例工具后复用阶段 2 的普通对话检查。
