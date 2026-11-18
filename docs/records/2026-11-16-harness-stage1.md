# 艾拉 Harness 阶段 1 验收记录

2026-11-16（Asia/Shanghai）。阶段 1 已通过，阶段 2 待开始。开工基线为本地 `main` 的 `b3325f1`。保留了工作区原有的文档改动。生产最后一次代码比对仍以 11 月 4 日记录的 `01dd7e9` 为准。本次重新实测了主机资源、服务和入口健康。 [实施计划](../HARNESS_PLAN.md) · [此前项目与腾讯云基线](2026-11-04-baseline.md)

## 版本和结构

| 项目 | 锁定及核对结果 |
|---|---|
| DeepSeek Harness | 官方预发行版 `@deepseek-ai/dsh@0.2.2-rc.3`，标签 `dsh-v0.2.2-rc.3`，发布页提交 `38a6a5c`。核对时 npm `next` 为 `rc.3`，`latest` 为 `rc.1`。Harness 要求 Node `>=24.9.0`。 |
| 运行与安装 | Node `24.11.1`，随发行包提供的 npm `11.6.2`，本项目依赖 pnpm `10.30.0`。Windows x64 和 Linux x64 的 Node 档均通过官方 SHA-256 校验。Python 仅用标准库，实测 Windows `3.14.3` 和 Ubuntu `3.12.3`。 |
| 依赖锁 | [`harness/package-lock.json`](../../harness/package-lock.json) SHA-256 `6abf63ec634c8c9682e7b9188aade03db853d59d70acde486aaf30148ab49102`，Windows 和腾讯云相同。 |
| 艾拉 bundle | `giftia-isla-harness-bundle@0.1.0`，`engines.dsh` 精确锁定 `0.2.2-rc.3`。包内是 `package.json`、Cordis 补丁和插件模块，SHA-256 `5c8e6c8cda66785c064c747351de7cb603146247572c412f2f74276f10908f35`。 |

[官方发行说明](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.3) · [锁定版本 CLI 参考](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.3/apps/cli/reference/README.md) · [Node 发行校验清单](https://nodejs.org/dist/v24.11.1/SHASUMS256.txt)

`isla-test` 从官方 `sdk-minimal` 模板派生。测试入口 `127.0.0.1:18111` 提供 `/api/health` 和 `/api/test-turn`，通过 stdio JSON-RPC 让 Harness agent loop 生成回复。官方 DeepSeek adapter 使用 `127.0.0.1:18112/v1/messages` 的模拟模型。模拟模型以 SSE 返回固定回复，并记录实际模型请求。bundle 通过 `ctx.systemPrompt.section` 注册最小的合成角色提示。配置关闭自动压缩、会话日志上传、插件清单上报和可见的 Shell、通用文件等工具。请求实测 `deepseek-flash`、`max_tokens=900`、`thinking.type=disabled`、无工具。温度和正式 B 角色上下文留给阶段 2 核对。

- Windows 使用 `harness/.stage1/lab` 作为状态根，包含 `workspace/`、`dsh-home/`、`fixtures/giftia-test.db`、模拟请求日志和打包产物。进程使用当前 Windows 用户，隔离范围为工作目录、环境变量、profile 和数据路径。
- 腾讯云使用专用系统用户 `giftia-harness-test` 和 `/opt/giftia-harness-test`。源码在 `source/harness`，状态在权限 `0700` 的 `state`，布局与本地相同。`giftia-harness-test.service` 将 `/opt/giftia` 和用户主目录设为不可访问，写入范围限定在测试状态目录及服务私有临时目录，配置回环网络和 `512M` 内存限额。回环健康端口供同机测试使用。
- 合成 SQLite 使用现有 `Store` 生成两条消息和两条记忆，覆盖虚构搭档私有与访客 `person` 视角。阶段 1 的测试对话使用全新 Harness 会话，SQLite 在阶段 2 接入角色服务后才参与按轮读取和提交。

## 复现

Windows 从仓库根目录运行以下命令。将校验过的 Node x64 ZIP 解压到示例目录，或把已安装的相同版本放到 `PATH`。

```powershell
$env:Path = "$PWD\harness\.stage1\toolchain\node-v24.11.1-win-x64;$env:Path"
$env:PYTHONUTF8 = '1'
Set-Location harness
npm.cmd ci --include=optional --no-audit --no-fund
python stage1.py prepare
python verify.py
python stage1.py install
python stage1.py serve
```

`prepare` 在缺少 profile 时创建模板，每次复制阶段 1 补丁，已有合成库会保留。`verify.py` 完成后会卸载 bundle，随后用 `install` 重新装入刚验收的包。复测时传 `--case`，可重复传入。`restart` 用例需要已安装的 bundle。`stage1.py dump` 打印生效的 Cordis 配置。已运行的环境可以用 `verify.py --probe installed` 验证，保持 `--root` 与运行中的服务一致。

腾讯云已有部署可按以下命令复验。首次重建时，创建同名无登录系统用户和独立目录，校验并解压 Node Linux x64 至 `toolchain/node-v24.11.1-linux-x64`。向 `source` 复制 `harness/` 源文件和 `giftia/__init__.py`、`giftia/store.py`。运行 `npm ci`，再用测试用户依次执行 `stage1.py --root "$root/state" prepare`、`pack`、`install`。将仓库测试 unit 安装到 `/etc/systemd/system/giftia-harness-test.service` 并执行 `systemctl daemon-reload`。

```bash
root=/opt/giftia-harness-test
export PATH="$root/toolchain/node-v24.11.1-linux-x64/bin:/usr/bin:/bin"
cd "$root/source/harness"
sudo systemctl start giftia-harness-test
sudo -u giftia-harness-test env PATH="$PATH" python3 verify.py --root "$root/state" --probe installed
sudo systemctl restart giftia-harness-test
sudo -u giftia-harness-test env PATH="$PATH" python3 verify.py --root "$root/state" --probe installed
```

同机完整验收应先停本测试服务以释放两个端口，再运行 `verify.py --root "$root/state"`。之后重新安装刚验收的包并启动本测试服务。实验命令、npm 安装和 bundle 安装的文件都位于项目或云端测试目录。

## 验收结果

| 用例 | Windows 与腾讯云结果 |
|---|---|
| 合成库 | SQLite `quick_check=ok`，合成标记存在，消息和记忆各两条。 |
| 启动 | 回环健康接口返回 `isla-test` 和锁定版本。普通测试输入得到 Harness 输出，每次精确命中一次回环模拟请求。 |
| 开发加载 | `--patch development.patch.yml` 生效，模拟请求的系统提示含艾拉插件文本。 |
| 打包 | `npm pack` 生成 601 B 归档，两平台哈希一致，归档仅含声明的三个文件。 |
| 安装 | `dsh plugin --profile isla-test add` 增加 profile 的 bundle 声明。普通测试轮的系统提示出现艾拉文本。 |
| 重启 | 新进程继续命中模拟模型，艾拉文本仍生效。隔离 home 中存在持久会话日志。已保存会话的恢复语义属于阶段 2。 |
| 卸载 | `dsh plugin --profile isla-test remove` 移除 bundle 声明。艾拉文本从实际请求消失，测试轮仍可生成。 |

腾讯云初检为 Ubuntu 24.04.4、2 vCPU、1935 MiB RAM，`MemAvailable` 1387 MiB，根盘剩余约 32 GiB。测试服务在真实 systemd 启动与重启后各通过一次安装态探针，两个端口仅监听 IPv4 回环。在运行中的服务挂载命名空间，以测试用户验证合成库可读、生产库不可读。单轮后所测 cgroup 峰值约 80～83 MiB。当前已装回 bundle，云端测试 unit 为 `active`，开机自启为 `disabled`。中途连接中断后复核，依赖和 unit 已成功安装，随后完成验收。

正式 `giftia-qq`、Cloudflare Tunnel 和备份定时器保持 `active`。正式服务和 Tunnel 的 PID 与核对前一致，重启计数均为 0。验收后本机源站 `8111` 与公网健康接口均为 HTTP 200。阶段 1 使用合成数据和回环模型，真实 API 请求为 0。正式入口和迁移按阶段 5 处理。

## 阶段 2 交接提示词

```text
请在 D:\ai chatbot 继续《艾拉原生接入 DeepSeek Harness 实施计划》的阶段 2。先核对 Git 状态，读 docs/HARNESS_PLAN.md、docs/records/2026-11-16-harness-stage1.md、docs/PROJECT.md、docs/CHARACTER.md 和 harness/ 源码，从已锁定的 Harness 0.2.2-rc.3、isla-test profile 和合成库接续。Windows 和腾讯云阶段 1 的启动、重启、开发加载、打包及装卸证据可以复用。正式入口与迁移留到阶段 5，不调用付费模型。

从 giftia/brain.py 的 Giftia.handle() 及现有身份、关系、记忆和请求账本入手，抽出准备本轮、受限记忆工具、提交本轮三类角色服务接口。沿用主线程写入、B 人物配置和现有可见性规则。让原生 bundle 注册角色上下文和记忆能力，让普通对话的模型调用由 Harness agent loop 完成。服务端解析身份、实例号与请求 ID，绑定到用途明确的会话，并实现幂等提交。检查 Harness 历史和工具结果在撤回、遗忘、重启与换任后的可见性。保持自动压缩关闭，必要时废弃旧模型会话并从当前授权内容重建。

先完成实现，再集中使用合成数据和模拟模型验收普通聊天、关系推进、最近上下文、长期召回、工具写入、撤回、遗忘、访客隔离、换任、重启和失败重试。用模拟模型的实际请求证明可见内容和模型参数，用角色库或请求账本证明每个有效请求只提交一次。修复后仅复测受影响的用例，写入新的 docs/records 记录。不要在 ~/.codex/skills 下安装 node_modules。
```
