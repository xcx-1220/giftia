# 艾拉 Harness 阶段 3 验收记录

2026-11-16（Asia/Shanghai）。阶段 3 在 Windows 隔离环境通过。工作区从 `main` 的 `b3325f1` 和已有阶段 1、2 改动接续。沿用 `isla-test`、Harness `0.2.2-rc.3`、Node `24.11.1`、pnpm `10.30.0`、阶段 1 的合成 SQLite 夹具及阶段 2 的普通聊天链路。重新打包的艾拉 `0.2.0` SHA-256 与阶段 2 一致。 [实施计划](../HARNESS_PLAN.md) · [阶段 1 环境和装卸证据](2026-11-16-harness-stage1.md) · [阶段 2 普通聊天和角色隔离证据](2026-11-16-harness-stage2.md)

## 示例 bundle 和权限

`giftia-sample-tool-bundle@0.1.0` 声明兼容 Harness `0.2.2-rc.3`，归档 SHA-256 为 `37f76800b7f504f0bb0848027e8db3b0c5741af582fb0b421809d429221c9c4c`。包内含 npm manifest、Cordis 补丁和插件模块，无额外运行时依赖。原生工具 `sample_reference` 接受空对象，固定返回「合成资料 SYNTH-REF-001：星砂图书室每周三 14:00 开放。」工具无需读取文件或查询数据库，也不写记忆。 [示例 bundle](../../harness/sample-tool-bundle/index.js)

示例 bundle 的权限默认都为 `false`。服务端可在 `$DSH_HOME/profiles/isla-test/cordis.patch.yml` 追加以下覆盖，示例给搭档开放：

```yaml
- id: giftia-sample-tool
  disabled: false
  config:
    permissions:
      partner: true
      guest: false
```

用 `guest: true` 授权访客。改为 `disabled: true` 后包仍装在 profile 中，Cordis 停用该插件。恢复为 `false` 可重新启用。改完配置后重启隔离聊天进程。每次工具解析和执行均检查当前会话的权限和生成轮次。身份来自阶段 2 的服务端认证及当前搭档绑定，通过私有回环回调查询。回调只提供当前会话的角色类别和轮次预算。示例插件持有自己的配置、工具定义和资料，艾拉 bundle 及 `POST /api/chat` 的请求和响应协议沿用阶段 2。 [角色网关](../../harness/role_gateway.py) · [示例 Cordis 补丁](../../harness/sample-tool-bundle/cordis.patch.yml)

## 复现

从仓库根目录运行，沿用阶段 1 已装的 Harness 依赖。脚本为每次验收创建独立状态根，执行 npm 打包和原生 CLI 装卸，再通过回环 `/api/chat` 发送合成请求。需要能连接本机回环端口 `18111`、`18112` 和角色回调的临时端口。

```powershell
$env:Path = "$PWD\harness\.stage1\toolchain\node-v24.11.1-win-x64;$env:Path"
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $PWD.Path
python harness/verify_stage3.py
# 只复测卸载
python harness/verify_stage3.py --case uninstalled
```

单独打包和装卸时，将 `$Stage3Lab` 设为已准备好且聊天进程已停止的隔离状态根。首次在缺少目录符号链接权限的 Windows 环境安装时，在该 profile 的 `pnpm-workspace.yaml` 中设置 `storeDir: .pnpm-store`。停用或重新启用可用上面的 Cordis 补丁。卸载时移除示例的专用覆盖，并执行 `remove`。

```powershell
$env:DSH_HOME = Join-Path $Stage3Lab 'dsh-home'
npm.cmd pack --ignore-scripts --pack-destination "$Stage3Lab\packages" ./harness/sample-tool-bundle
node harness/node_modules/@deepseek-ai/dsh/lib/bin.js plugin --profile isla-test add "$Stage3Lab\packages\giftia-sample-tool-bundle-0.1.0.tgz"
python harness/stage1.py --root $Stage3Lab serve-role
# 停掉隔离服务并移除示例的 profile 覆盖后运行
node harness/node_modules/@deepseek-ai/dsh/lib/bin.js plugin --profile isla-test remove giftia-sample-tool-bundle
```

## 验收结果

| 用例 | 实际结果 |
|---|---|
| `installed` 安装和默认权限 | 独立归档通过检查，原生 CLI 把示例加入 profile。默认两种身份均看不到示例工具。仅授权搭档后，搭档模型清单含示例与原有三个记忆工具，工具调用及结果均进入下一次请求。未经授权的访客清单为空，模拟模型强行调用得到 `UNKNOWN_TOOL`，无资料返回，对话仍成功。 |
| `permissions` 访客权限及换绑 | 仅授予访客后，访客只看到示例工具，搭档只看到原有记忆工具。交换搭档绑定后，相同令牌按新角色改变可见性，现搭档失去示例权限，原搭档以访客身份成功调用。 |
| `disabled` 停用 | 两种角色授权均为 `true`，Cordis 停用后模型清单均无示例。包仍在 profile，两个角色的普通聊天各成功提交一次。 |
| `reenabled` 重新启用 | 恢复 Cordis 启用后，两种角色各成功调用一次，模拟模型在后续请求收到固定资料。 |
| `uninstalled` 卸载 | 原生 CLI 移除包依赖和 bundle 声明，艾拉 bundle 仍在 profile。搭档只看到原有记忆工具，访客清单为空。两种角色分别只调用一次模拟模型，各自成功提交一次普通聊天，无工具事件。 |

主证据目录为 `harness/.stage1/stage3-validation/0ca4c85fda/`。集中验收中，重启的模拟服务把上一场景保留的脚本与访客恢复的历史匹配，卸载状态出现额外的一次 `UNKNOWN_TOOL`，两次实际请求的工具清单均为空。修正验收脚本，让每个探针覆盖当前模拟脚本，仅复测卸载。卸载通过的证据在 `harness/.stage1/stage3-validation/9d80c29a76/`。开始运行时另遇到 Windows 符号链接权限和 Codex 沙箱回环限制，分别以 profile 内 pnpm store 和获准的本机回环执行解决。

取各用例最后一次通过的结果，共 14 个提交回合、20 次模拟模型请求、5 次成功调用及 1 次被拒调用。两个证据目录的 SQLite 均通过 `quick_check`。各目录的 `model-requests.jsonl` 保存模型实际请求，`dsh-home/sessions/**/session.v4.jsonl` 保存原生 `tool/call`、`tool/result` 和 `turn/end` 事件，`acceptance/*.log` 保存回环服务日志，`*.patch.yml` 与 `*.profile.json` 保存各状态配置，`fixtures/giftia-test.db` 可查提交账本，`packages/` 保存两个归档。付费模型请求为 0。

## 阶段 4 交接提示词

```text
请在 D:\ai chatbot 继续《艾拉原生接入 DeepSeek Harness 实施计划》的阶段 4「跑通附件与自有 UI」。

先核对 Git，阅读 docs/HARNESS_PLAN.md 和 docs/records/2026-11-16-harness-stage3.md，并从 Harness 0.2.2-rc.3、isla-test、艾拉 0.2.0 和示例 bundle 0.1.0 接续。保留工作区改动，复用前三阶段的验收证据。先确定附件走 Harness Host 还是应用网关，用跨页放置合成内容的文本 PDF 验证上传、取消、失败重试、按页提取、页码引用和继续追问。让附件 ID 和历史由服务端绑定到授权身份及会话，验证无跨身份读取，文档内容不自动写为共同经历。

用手机优先的自有网页接通普通消息、附件卡片、工具状态、历史和断线恢复。先完成实施，再集中验收上传、提问、引用、追问以及刷新或重启后恢复。检查模型实际输入和事件，修复后只复测受影响的用例。在 docs/records 记录方案、命令和证据，更新实施计划并给出阶段 5 交接提示词。沿用隔离环境和模拟模型。正式入口与数据迁移留到阶段 5。不要在 ~/.codex/skills 下安装 node_modules。
```
