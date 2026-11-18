# 艾拉 Harness 阶段 5 云端试用与切换准备记录

2026-11-16（Asia/Shanghai）。腾讯云隔离候选的模拟验收、旧热备兼容性和三类资料的恢复演练已完成。Android TWA 真机、真实模型引用及 R1 正常试用待验收，R2 尚未进入切流步骤。Git 起点为 `main` 的 `b3325f1` 加已有工作区，沿用前三阶段的角色与工具证据以及阶段 4 的网页链路验收。 [实施计划](../HARNESS_PLAN.md) · [阶段 4](2026-11-16-harness-stage4.md) · [路线图 R1/R2](../ROADMAP.md#8-m3试用发布与回退)

## 云端基线与隔离候选

| 项目 | 本次核对 |
|---|---|
| 腾讯云主机 | Ubuntu 24.04.4 LTS、2 vCPU、Python 3.12.3、内存 1,935 MiB。启动前 `MemAvailable` 1,426 MiB，根盘可用 30,691 MiB。演练结束可用内存 1,319 MiB。 |
| 同机正式服务 | `giftia-qq`、`cloudflared`、`giftia-backup.timer` 保持 active。主服务与 Tunnel 前后 PID 一致，重启计数均为 0。回环 `8111` 返回 200，实际电脑公网健康和 `assetlinks.json` 均返回 200。 |
| 已有阶段 1 | `giftia-harness-test` 和 `18111/18112` 继续运行。回环健康 200，PID 与核对前一致。 |
| 阶段 5 候选 | `/opt/giftia-harness-test/stage5-20261116`，新建瞬态单元 `giftia-harness-web-stage5.service`，仅监听 `127.0.0.1:18121/18122`。复用低权限用户、只读 Node 依赖，使用独立 `state` 和 `DSH_HOME`，内存上限 512 MiB。验证单元挂载命名空间内无法读生产库，出站网络限制为回环。 |
| 版本 | Node `24.11.1`、pnpm `10.30.0`、Harness `0.2.2-rc.3`、`isla-test`、艾拉 `0.2.0`、示例 `0.1.0`、`pypdf 6.17.0`。锁文件和两个 bundle 的 SHA-256 与前序环境一致。网页继续使用应用网关 PDF 路径。 |

阶段 5 候选演练后恢复为模拟模式运行，临时恢复单元已停止。正式域名与 Tunnel 仍通往旧网页。 [运行维护](../OPERATIONS.md) · [候选启动脚本](../../harness/stage5_cloud.sh)

## 资源与浏览器结果

空闲采样 2 秒共 99 点，中位数 84.9 MiB。PDF 上传和三轮问答期间每 20 ms 采样，共 66 点，最高 128.9 MiB。该次单元的 `memory.peak` 为 140.8 MiB，包含进程启动。运行状态表观字节数从 252,009 增至 286,225。演练完成时磁盘实际占用为候选源码约 1.3 MiB、Python 解析库约 1.8 MiB、候选状态约 2.7 MiB、三项快照合计 272 KiB。Node 依赖复用阶段 1 的目录。

| 模拟用例 | Harness 首个正文 | Harness 整轮 | 网关读到完成 | 模型请求 |
|---|---:|---:|---:|---:|
| 跨页标签，引用第 1、2 页 | 111 ms | 120 ms | 258.2 ms | 1 |
| 追问归还截止时间，引用第 2 页 | 73 ms | 76 ms | 133.5 ms | 1 |
| 资料无联系电话 | 56 ms | 58 ms | 133.5 ms | 1 |
| 新增普通聊天 | 55 ms | 57 ms | 131.5 ms | 1 |

PDF 上传至状态可用为 385.6 ms。首字采用 Harness 会话中 `turn/start` 到首个原生正文字块的时间差，网页轮询显示完整回答。完成时间包含回环 API 和状态轮询。以上是本机模拟模型、单用户、两页夹具的数据，真实提供商延迟及长时间试用另行测量。 [采样脚本](../../harness/stage5_probe.py)

实际 Edge 经 SSH 回环转发打开云端候选。在 360 CSS 像素视口验收了搭档登录、普通消息和工具状态、PDF 上传中断后重试、跨页引用、打开第 2 页、追问中断网后恢复、刷新后恢复、无效引用重试，以及切换访客后的历史和附件隔离。原始模拟请求中三轮 PDF 均无工具声明，来源页读取经授权，异身份按 ID 猜测返回 404。正式 TWA 对应的公网关联文件可读。本机未找到可用 Android 工具链、连接设备或仓库内测试 APK，TWA 的登录与断线恢复无本次真机证据。

## 迁移、去重与回退

1. 在腾讯云上以 SQLite 在线备份 API 从 `giftia-2026-11-16.db` 制作管理员专用副本，运行实际部署的旧 `Store` 读取。候选 `Store` 在副本初始化并新增 `harness_turns`。旧列逐表比较一致，共 890 条消息、242 条记忆、4 条日记、55 项元数据和 32 项状态。加表后旧代码再次打开通过 `quick_check`。生产热备及其副本内容留在服务器，候选网页始终使用合成库。
2. 在云端候选写入合成普通聊天 `stage5-cutover`。相同 ID 和文本重交只返回既有结果，同 ID 换文本返回 409，Giftia 账本只有一次提交，重提新增模型调用为 0。
3. 停止阶段 5 写入后，分别备份 Giftia SQLite、网页附件和问答 SQLite、Harness 会话文件。恢复到新根目录时逐组件核对 SHA-256，两个 SQLite 通过 `quick_check`，18 个会话目录文件与清单一致。合成凭据单独以权限 0600 复制到恢复环境。
4. 用正式部署旧版 `Store` 打开已做过合成新聊天的快照副本，搭档历史视角读到新增的两条消息，访客视角看不到。回退时可保留当前健康数据库并切回旧代码。旧网页运行时有自己的请求账本和浏览器本地历史，跨新旧网关的待处理请求和网页历史交接留作 R2 发布检查。
5. 恢复后的候选成功重放原普通聊天回执，新增模型调用为 0。附件来源第 2 页仍可访问，其他访客仍收到 404，原生会话事件可解析。针对恢复后的相同附件继续追问，实际只调用一次模拟模型并引用第 2 页。 [备份、恢复与旧版读回脚本](../../harness/stage5_recovery.py)

首次部署发现独立端口启动分支有一处缩进错误，修正后复测候选启动。首次兼容性比较失败是验收脚本把 `sqlite3.Row` 与元组直接比较。首次计时也曾在 JSONL 写入完成前查找回合结束事件。修正两处验收工具后分别复测旧热备和云端采样。通过证据有 11 次模拟请求，首次计时留档另有 1 次模拟请求。真实模型调用为 0。

## 正式网关与 R2 缺口

| 检查项 | 当前证据与后续门槛 |
|---|---|
| 正式认证 | 当前 `/api/session` 未登录为 401，搭档与访客均为 200。搭档推送状态 200，访客为 403。已部署代码使用分角色口令、`HttpOnly`、`SameSite=Strict`，旧聊天有按 IP 与角色的限流。线上合并后为 `deepseek-flash`、思考关闭、温度 1.15、900 tokens，两份本地 TOML 覆盖为空。 |
| 候选认证 | 阶段 5 当前仅提供回环合成凭据。公网接入前需明确 HTTPS 同源、Secure Cookie、身份迁移与会话失效、限流以及新旧请求归属。现有候选 Host 校验只接受回环。正式 `/api/web/state` 实测 404。 |
| 附件与日志 | 候选附件行有效期 24 小时，成功解析后清除原始 PDF 字节。文档回答、来源内容及副本有独立的保留问题，Harness JSONL 当前保留页文本，没有自动联动删除。生产旧版请求账本为 24 小时、最多 256 条，正式 SQLite 热备保留 14 份。R2 要明确后台、日志、WAL 和快照删除的统一约束，以及独立备份和恢复范围。 |
| 真机与真实模型 | 测试需要可访问隔离 HTTPS 入口的已签名 Android TWA。真实引用先验证跨页、同附件追问与资料无答案三个样本，分别检查陈述是否由对应页支持。尚无阶段 5 已明确的实际请求额度，历史评测额度不能用于本次。 |
| R1/R2 | 先按冻结候选完成独立身份的 3～7 天真实试用并记录实际使用。正式入口切换须满足 R1、F2/F3 与 R2 发布验收，处理新旧网关交接及维护/推送边界。 |

## 复现与证据

从仓库根目录用阶段 4 已核对的两页夹具生成待上传源码包，复现时将输出设为一个新文件。包内为明确列出的工作区源码、默认配置、已安装的锁定纯 Python `pypdf` 文件和合成夹具。

```powershell
$pdf = 'harness/.stage1/stage4-validation/34d5cc6d68/synthetic-reference.pdf'
$archive = 'harness/.stage1/stage5-validation/20261116-cloud/candidate-final.tgz'
python harness/stage5_package.py $pdf $archive
```

通过 SSH 将归档传入腾讯云。在独立、首次创建的阶段 5 目标目录解包，链接既有阶段 1 依赖，创建专用账户拥有的状态目录。以下命令在腾讯云执行，`$archive` 为服务器上的归档路径，`$base` 是本次已存在的隔离根。首次部署使用新的 `stage5-*` 目录。

```bash
base=/opt/giftia-harness-test/stage5-20261116
archive=/home/ubuntu/giftia-stage5-input.tgz
nodepath=/opt/giftia-harness-test/toolchain/node-v24.11.1-linux-x64/bin:/usr/bin:/bin
pypath="$base/python:$base/source"
source_dir="$base/source/harness"

# 在新隔离根准备候选
sudo install -d -m 0750 -o root -g giftia-harness-test "$base"
sudo tar --no-same-owner -xzf "$archive" -C "$base"
sudo ln -s /opt/giftia-harness-test/source/harness/node_modules "$source_dir/node_modules"
sudo install -d -m 0700 -o giftia-harness-test -g giftia-harness-test "$base/state"
sudo bash "$source_dir/stage5_cloud.sh" "$base" state prepare
sudo bash "$source_dir/stage5_cloud.sh" "$base" state start

# 只在新目的目录核对旧热备
sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$pypath" python3 "$source_dir/stage5_recovery.py" \
  migrate /opt/giftia/data/backups/giftia-2026-11-16.db "$base/legacy-verified" /opt/giftia

# 新候选合成性能及重复请求验收
cgroup=/sys/fs/cgroup$(systemctl show giftia-harness-web-stage5 -p ControlGroup --value)
sudo -u giftia-harness-test env PATH="$nodepath" PYTHONPATH="$pypath" \
  python3 "$source_dir/stage5_probe.py" --root "$base/state" \
  --pdf "$base/synthetic-reference.pdf" --cgroup "$cgroup" measure
```

浏览器通过 `ssh -N -L 127.0.0.1:18121:127.0.0.1:18121` 打开云端隔离网页。可用 `stage5_probe.py --root "$base/state" plan-browser` 准备模拟响应，按阶段 4 的 Playwright 命令复现手机流程。使用候选的合成凭据，入口端口改为 `18121`。要复现三项恢复，在停止候选写入后执行 `stage5_recovery.py snapshot`，再分别执行 `restore SNAPSHOT NEW_STATE giftia|sessions|attachments`。在隔离恢复目录使用相同的合成凭据后，依次执行 `stage5_cloud.sh ... restored prepare`、`restored start` 和 `stage5_probe.py --root NEW_STATE restored`。旧代码读回使用 `stage5_recovery.py rollback-read COPIED_DB /opt/giftia stage5-cutover`。

- Windows 证据位于 `harness/.stage1/stage5-validation/20261116-cloud/`。其中有 `cloud-metrics.json`、`cloud-recovery.txt`、`legacy-migration-verified.txt`、`production-auth.txt`、`policy.txt`、`post-host.txt`、`public-health.txt`、`browser-result.txt` 和三张手机截图。
- 云端原始合成证据位于 `$base/state` 与 `$base/restored`。各自有 `model-requests.jsonl`、`api-trace.jsonl`、`dsh-home/sessions` 和两套 SQLite。快照及校验清单位于 `$base/recovery/snapshot`，管理员专用旧数据副本位于 `$base/legacy-verified`。

