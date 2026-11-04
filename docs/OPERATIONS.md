# 腾讯云运行维护

本页说明现有操作流程；最近实机核对日期是 **2026-11-04**，详见[基线记录](records/2026-11-04-baseline.md)。以下发布、重启、修复和推送命令是操作手册，不表示本次整理已执行。

## 当前拓扑

| 项目 | 已核对配置 |
|---|---|
| 主机 | 腾讯云 Ubuntu 24.04，Python 3.12.3，约 2GB 内存 / 40GB 系统盘 |
| 应用目录 / 用户 | `/opt/giftia` / `giftia`；生产目录不是 Git 工作区 |
| 主服务 | `giftia-qq.service`，当前为网页模式 |
| 启动命令 | `/usr/bin/python3 /opt/giftia/adapters/qq_official.py --web-port 8111 --web-only` |
| 外部入口 | `https://giftia.icu` → Cloudflare Tunnel → `http://localhost:8111` |
| 源站监听 | Web 仅 `127.0.0.1:8111`；外部 SSH 使用 22；无需源站开放 80/443 |
| Tunnel | `cloudflared.service`，令牌从 `/etc/cloudflared/token` 文件加载，不把值复制到文档 |
| 权限 | `ProtectSystem=strict`、`ProtectHome=yes`，应用可写目录 `/opt/giftia/data` |
| 热备 | `giftia-backup.timer`，服务器本地时间 04:00 起随机延迟最多 300 秒，保留 14 份 |

QQ 自 10 月 20 日暂停，服务名保留。当前 model/persona 生效值必须合并个人覆盖后检查；不能仅凭本地文件推断线上配置。

## 日常只读核对

在服务器运行：

```bash
systemctl is-active giftia-qq cloudflared giftia-backup.timer
systemctl status giftia-qq --no-pager
systemctl list-timers giftia-backup.timer --all
journalctl -u giftia-qq --no-pager -n 80
journalctl -u cloudflared --no-pager -n 40
journalctl -u giftia-backup --no-pager -n 20
curl http://127.0.0.1:8111/api/health
sudo -u giftia /usr/bin/python3 /opt/giftia/deploy/backup_db.py --check
```

公网健康也从实际客户端网络检查。健康 200 只证明 HTTP 入口存活，不证明模型生成、主队列或通知送达。日志、session 输出和数据库可能含个人信息，报告只保留必要摘要。

11 月 4 日出现过“腾讯云直接请求公网域名返回 403，而实际电脑客户端返回 200”的差异，不能只据某个客户端结果宣布全站不可用；应区分源站、Tunnel、边缘规则和客户端。

本机 `tools/check_sync.ps1` 可比文件哈希，但它读取 Git 已跟踪路径，原始换行也影响结果；新未跟踪文件不包含在内，归档移动与文档差异也不代表业务代码未部署。精确核对需明确基线、文件清单、LF 规范化与个人覆盖排除范围。生产无 Git 时保存发布清单，不假装存在云端 commit。

## 现有实例升级

1. 准备与 `/opt/giftia` 分开的候选源码，运行适用离线检查与隔离迁移验证。核对候选包含的模型、人物、前端和运维文件范围。
2. 比较线上有效配置、实例元数据与候选；保护 `.env`、`config/*.local.toml` 和 `data/`。准备一致性备份及必要的运行文件快照。
3. 使用发布工具先预览，审阅代码范围与记忆策略。`--repair-memories` 是历史质量发布的可选修复，不是每次发布都要执行。

```bash
sudo python3 /path/to/candidate/deploy/quality_release.py --source /path/to/candidate
```

4. 在该次部署范围确定且验证通过后执行应用：

```bash
sudo python3 /path/to/candidate/deploy/quality_release.py --source /path/to/candidate --apply
```

工具保存 `data/releases/quality-时间戳/` 下的代码、数据库一致性备份和运行文件快照，保留凭据与个人覆盖，拒绝模型配置变化。健康失败会回退代码及本批附加策略，不替换实时数据库。

**文件范围限制：** 现有工具按后缀选择文件，不覆盖 `.sh`、`.service`、`.timer`；涉及安装脚本或 systemd 的发布不能仅靠这个命令完成。源文件移走后也要核对旧目标文件是否仍留在服务器，不能从本地归档推断云端已清理。相关改进登记在路线图 O1。

5. 验证源站和公网、身份、有效配置、数据库完整性、任务恢复、网页/App，并记录版本和观察范围。不要在正式实例灌入合成测试聊天。

回退示例：

```bash
sudo python3 /path/to/candidate/deploy/quality_release.py --rollback /opt/giftia/data/releases/quality-时间戳
```

实际路径取本次发布记录。旧的 `quality-20261025-184702` 是历史恢复点，不意味着适合所有未来版本。回退代码前检查维护任务和 schema 兼容，保留新聊天；只有数据损坏才讨论全量数据库恢复。

## 新安装与服务配置

`deploy/setup.sh` 检查 Python、创建 `giftia` 用户、安装源码、运行自测并注册主服务和备份 timer。它保留 `data/`、`.env`、个人覆盖，更新仓库默认配置与服务模板。

```bash
sudo bash deploy/setup.sh
```

当前脚本仍按 QQ 凭据判断自动重启，而 service 已是 `--web-only`。网页新装先配好模型与网页口令，再明确启动并核验：

```bash
sudo systemctl restart giftia-qq
curl http://127.0.0.1:8111/api/health
```

Tunnel、DNS、VAPID 和 Worker 不由 `setup.sh` 自动建立。外部路径应指向本机 8111，Worker 使用 `deploy/cf-push-relay/worker.js`，中转密钥与服务端一致。初次设备授权和通知实测见[网页与推送](WEB.md)。历史安装细节在[旧网页记录](archive/2026-10-project/WEB_LEGACY.md)，平台界面与命令版本需在实际安装时复核。

传送源码优先使用审阅后的 Git 提交归档：`git archive` 只包含指定提交的已跟踪文件，应先确认追踪列表没有秘密/数据，并注意它不包含未提交改动。不要把整个工作目录含凭据直接打包，也不要将本地旧 `data/` 当部署内容。

## 凭据与配置维护

网页与推送键名见[网页文档](WEB.md)。`.env` 由服务端保存为权限 600；个人改动放 `.local.toml`。修改凭据使用仓库的 `put_env.sh` 合并，避免 shell 字符串拼接破坏内容：准备受保护的 LF `KEY=VALUE` 文件，传到服务器上的受控位置，再执行：

```bash
sudo bash /opt/giftia/deploy/put_env.sh /path/to/protected/keys.env --restart
```

仅出现的键会被替换；核对重启和功能后按凭据管理方式处理临时文件。密钥、口令、Tunnel token 和 APK 私钥不进入 Git、日志或 Markdown。

## 备份、恢复和迁移

每日任务使用 SQLite 在线备份 API，输出 `data/backups/giftia-YYYY-MM-DD.db`，同日重跑替换当天副本，保留 14 份，并执行完整性校验。它是**同机数据库备份**，不包含全部资产，也不能防止整机或账户丢失。

```bash
sudo systemctl start giftia-backup.service
sudo -u giftia /usr/bin/python3 /opt/giftia/deploy/backup_db.py --list
```

完整恢复清单：一致性数据库、回忆录、封存、订阅/最近推送、请求账本及必要状态文件、有效配置与代码版本；凭据、VAPID、Tunnel 和 APK 签名材料单独受控保存。运行中直接复制主 `.db` 会遗漏 WAL，不可作为一致性备份；整目录快照需停服务或设计协调快照流程。

迁移前在独立目录/机器恢复并核对实例、寿命、关系、数据计数、任务与权限；关闭真实推送，防止恢复副本主动联系用户。确认后才切换唯一正式实例。完整异地自动备份和恢复演练尚未实现，见路线图 O2。

## 排障顺序

| 现象 | 先检查 |
|---|---|
| 本机 8111 不通 | 主服务、启动参数、网页口令是否配置、静态目录 |
| 本机正常但公网异常 | cloudflared、Tunnel ingress、DNS/边缘规则与客户端差异 |
| 登录 401 | 口令是否变更、旧 cookie、用户角色；不要输出实际口令 |
| 队列繁忙 / 长时间等待 | 状态码、request_id、主模型/工具耗时、维护队列；避免直接重复生成 |
| 没收到通知 | 当前设备权限/订阅、VAPID、Worker、服务接受结果及设备状态 |
| 更新后人物没变 | 部署文件、个人覆盖、服务重启、生效提示；不要只看默认配置 |
| App 界面旧 | service worker 缓存与部署静态资源；签名问题另见安卓文档 |
