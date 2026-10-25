# 网页入口（giftia.icu）

当前使用网页 <https://giftia.icu> 和加载同一网站的安卓 App；QQ 自 2026-10-20 起暂停。

不是另起一个服务，也不是复制一个她。`giftia/webgate.py` 在**同一个进程**里开了一个
HTTP 线程：请求排进队列，由主线程交给她执行。所以网站上的你和 QQ 上的你，
面对的是**同一份记忆、同一个会话上下文、同一条寿命**。

> 2026-10-25 已部署聊天质量更新，内部/公开健康、身份认证、新接口和数据保留检查通过。
> 旧安装记录作为历史背景保留；本次发布与回滚见 [CHAT_QUALITY_RESULTS.md](CHAT_QUALITY_RESULTS.md)。

---

## 1. 架构：一个进程，网页 / App 共用内核

当前 `deploy/giftia-qq.service` 的启动命令是：

```bash
/usr/bin/python3 /opt/giftia/adapters/qq_official.py --web-port 8111 --web-only
```

服务名仍是 `giftia-qq`，但不会连接 QQ。主线程排空网页队列，每 600 秒检查一次搭档主动联系；
恢复 QQ 后，网页队列改由 QQ WebSocket 主循环一起处理。

```
主循环（主线程；当前不连 QQ）                   HTTP 线程（辅助）
────────────────────────────                  ─────────────────────────────
处理网页任务、检查主动联系                      POST /api/login  → 验口令、发 cookie
   │                                           POST /api/chat   → 验 cookie、限流、入队
   │  每个 tick：                              GET  /api/health → 活着
   ├── web_gate.drain(settings) ◄──── 队列 ◄──┤ GET  /*         → 静态文件（PWA 壳）
   │        │                                  └─ 等结果（最多 60 秒）
   │        └── 最多 2 个/次 → handle_message(scene="c2c" / "guest")
   └── 继续循环（恢复 QQ 时还会收发 QQ 事件）
```

### HTTP 线程只做四件事

对聊天请求，HTTP 线程只做：**验 cookie、限流、入队、等结果**。
它不解释消息、不拼提示词、不读写数据库、不碰 `Giftia`。
健康检查和静态文件不碰她，可以当场返回。

### 为什么 HTTP 线程绝不碰她

她不是线程安全的：

- 单线程；
- 一个 SQLite 连接（在主线程里创建）；
- 虚拟时钟、心情、亲密度、寿命都在这一个对象的内存里。

两个线程同时进去，轻则状态错乱，重则 SQLite 直接报
`recursive use of cursors`。**队列是唯一安全的形状。**

真正的执行在主循环里：`self.web_gate.drain(self.settings)`，每次最多处理
`DRAIN_PER_TICK = 2` 个请求。`--web-only` 循环每次排空后睡 0.05 秒；
恢复 QQ 时，WebSocket 循环也会定期调用 `drain()`。模型调用仍是同步串行执行。

### 踩过的坑：在 HTTP 线程里读一下 `handler_id` 也会炸

一开始觉得“只是读个 id，不算碰她吧”。不是。`handler_id` 是从 SQLite 读的，
而那个连接是主线程建的：

```
ProgrammingError: SQLite objects created in a thread can only be used in that same thread
```

所以搭档的 `Job.user_id` 一开始是空的：**搭档的 `user_id` 必须由主线程在 `drain()` 里解析**
（`hub.handler_id`），再交给 `handle_message()`。访客的 id 直接来自 cookie 里的字符串，
不读库，所以可以在 HTTP 线程里填。

一句话：**凡是要查库、要碰她状态的，都只能发生在主线程。**

---

## 2. 怎么开

这两个开关在 `adapters/qq_official.py` 上，不在 `run.py` 上——网站入口和 QQ
共用同一个进程，所以从适配器启动。

| 目的 | 命令 | 说明 |
|---|---|---|
| 只跑网站 / App | `python adapters/qq_official.py --web-port 8111 --web-only` | 当前部署模板，不需要 QQ 凭据 |
| 和 QQ 一起开 | `python adapters/qq_official.py --web-port 8111` | 恢复 QQ 时使用，需要 QQ 凭据 |
| 本机离线调试 | `python adapters/qq_official.py --web-port 8111 --web-only --mock-llm` | 仍需网页口令；使用本机配置的数据目录 |
| 看入口活着没 | `curl http://127.0.0.1:8111/api/health` | 返回 `{"ok": true}` |

`--web-port 8111` **只监听 `127.0.0.1`**，不对外开端口。公网访问走 Cloudflare Tunnel
（见第 6 节），服务器本身不需要暴露 8111。

没配 `AILA_WEB_SECRET` 时，`--web-port` 不会开入口：日志会提示去 `.env` 里加口令，
QQ 那条线照常跑。`--web-only` 则必须同时给 `--web-port` 和口令，否则直接退出。

`--web-only` 已跳过 QQ 凭据检查，不需要填占位 AppID / Secret。真实对话需要 DeepSeek key。
要从当前 systemd 恢复 QQ，确认 QQ 凭据后，移除实际 unit 中的 `--web-only`，
执行 `sudo systemctl daemon-reload`、`sudo systemctl restart giftia-qq`。
仓库 service 模板仍是网页模式，下次运行 `setup.sh` 会重新安装模板。

---

## 3. 身份：口令就是身份

网站没有“注册账号”这回事，只有两套口令，分别对应两种身份。

| `.env` 里的键 | 角色 | 场景 | 她眼里的你 | 能碰到的记忆 |
|---|---|---|---|---|
| `AILA_WEB_SECRET` | `owner`（搭档） | `scene="c2c"` | 搭档本人 | 和 QQ **共享同一份** |
| `AILA_WEB_FRIEND_PASS` | `friend`（访客） | `scene="guest"` | 一个独立的访客 | 只有他自己的那一份 |

### 搭档：一个她

用 `AILA_WEB_SECRET` 登录后，走的是 `scene="c2c"` + 搭档身份，
`user_id` 由主线程解析成 `hub.handler_id`。QQ 那边也是同一个 `handler_id`，所以：

- 长期记忆是同一份；
- c2c 会话上下文是同一个（工作记忆不另起一份）；
- 心情、亲密度、寿命是同一套。

**这就是“一个她”**：不是两个前端连到两个她，而是两个入口连到同一个她。

2026-10-20 的修正还让网页搭档路径传入 `speaker_name`，取已知搭档称呼，空值时回退到
代码中的“椎名”。这补齐了 QQ 原本就传入的昵称信息，但不保证不同场景每次回复都相同。
如果感觉她更礼貌、有距离，先确认是否用访客口令登录；两种角色的提示词与记忆视角本来就不同。

### 访客：看得见自己，看不见你

用 `AILA_WEB_FRIEND_PASS` 登录后：

- `scene="guest"`，身份是 `aila_visitor` cookie 里的独立 id；
- 视角只有 `person(他自己)`——**检索不到搭档的私事**；
- 写进他自己的记忆分区，**不进搭档的会话**；
- `is_handler=False` → 不推进搭档亲密度或心情，也不触发到期回收；寿命仍按时间自然流逝；
- 限流额度只有搭档的一半（他花的是搭档的 token）。

访客第一次登录时，服务端会发一个随机的 `aila_visitor`（16 个十六进制字符）。
换浏览器或清掉 cookie，就是另一个访客了——旧分区还在，但新身份看不到它。

### 为什么必须两套口令

如果只有一个口令，朋友进来就是“以搭档的身份”：

| 会发生什么 | 为什么 |
|---|---|
| 她当着他的面提起你的私事 | 他拿到的是搭档的视角 |
| 他的话写进你的会话 | 同一个 c2c 会话上下文 |
| 搭档关系和心情被陌生人推进，可能触发到期回收 | `is_handler=True`，进入搭档路径 |

这不是“权限没配细”，是**身份模型错了**。所以两套口令不是可选项。

### cookie 不是明文口令

登录成功后，cookie 长这样：

```
aila_auth=owner.<hmac>          # 搭档
aila_auth=friend.<hmac>         # 访客
aila_visitor=<16 位十六进制>    # 访客首次登录时另发一个
```

`<hmac>` 是 `HMAC-SHA256(口令, "aila-web")`，带角色前缀，方便一眼看出是哪套。
cookie 是 `HttpOnly; SameSite=Strict; Max-Age=31536000`。
**改口令 = 该角色的旧 cookie 立刻失效**，需要重新登录。

> 前端为了下次自动登录，会把口令本身存在 localStorage 的 `ella.pass` 里。
> 菜单里的「忘掉口令」**只清这一份本地口令**，不会让服务端那个 HMAC cookie 立刻失效；
> 共用设备上更稳的做法是清掉本站的站点数据（cookie + localStorage）。

---

## 聊天质量更新说明（2026-10-25，已部署）

以下请求编号、结果查询、分视角历史与后台维护已部署并验收。
旧部署仍支持原来的 `POST /api/chat {text}`；新版也兼容无 request_id 的旧客户端。
验证与发布状态见 [CHAT_QUALITY_RESULTS.md](CHAT_QUALITY_RESULTS.md)。

## 4. 接口

| 方法 | 路径 | 请求 | 成功 | 失败 |
|---|---|---|---|---|
| `POST` | `/api/login` | `{"pass": "..."}` | `200 {"ok":true,"role":"owner"\|"friend"}` + `Set-Cookie` | 口令错 `401`（强制延迟 0.4 秒） |
| `POST` | `/api/chat` | `{text,request_id?}` | `200 {request_id,status,reply,parts}`；等待中 `202` | 未登录 `401`；超频 `429`；空文本/无效编号 `400`；同编号不同内容 `409`；繁忙 `503`；执行失败 `502` |
| `GET` | `/api/chat/result?request_id=...` | 本人 cookie | 完成 `200`，等待 `202` | 无此身份下的请求 `404`；未登录 `401`；失败 `502` |
| `GET` | `/api/session` | 本人 cookie | `{role,view_id,known,greeting}` | 未登录 `401` |
| `GET` | `/api/health` | — | `200 {"ok":true}` | — |
| `GET` | `/api/push/status` | 搭档 cookie | `200 {owner,subscribed,count,public_key}` | 非搭档 `403` |
| `POST` | `/api/push/subscribe` | 浏览器订阅 JSON、搭档 cookie | `200 {ok,count}` | 非搭档 `403`；缺 endpoint `400` |
| `GET` | `/api/push/last` | 搭档 cookie | 最近消息 `{title,text,ts}`，没有时 `{}` | 非搭档 `403` |
| `POST` | `/api/push/test` | 搭档 cookie | `200 {sent,total,error}` | 非搭档 `403`；发送结果要看 `sent`，不能只看 HTTP 200 |
| `GET` | `/*` | — | 静态文件 | 目录穿越/不支持的后缀 `404` |

几个实现细节：

- 聊天文本最多 2000 字，超出截断。
- 新版使用容量为 5 的队列，登记请求与入队受同一把锁保护；满时返回 `503 {busy:true,...}`。
- 服务端等待最多 60 秒、前端单次请求 45 秒；任务继续执行，新版按相同 request_id 查询/恢复。页面连续等待三分钟仍无结果时保留请求，允许继续编辑草稿。
- `/api/health` 只说明 HTTP 入口活着，不会检测模型、主线程任务或推送服务。
- `/api/chat` 直接走 `handle_message()`，没有 CLI / QQ 的斜杠指令分发。
- 口令错误也慢 0.4 秒再回，别让它成为一个高速爆破口。
- `parts` 是后端把回复按换行和句末标点切出来的 1～3 段；前端一段一段播。
- 静态文件按后缀白名单发（html/js/css/json/png/jpg/svg/ico/webmanifest/txt/woff2），
  解析后的路径必须还在 `web/` 里，否则 404。
- HTTP 层的 HTML/JS/CSS 使用 `no-cache`，图标缓存一天；service worker 仍可能先显示旧壳再后台更新。
- 经 `_json()` 返回的 API 响应带 `Cache-Control: no-store`；登录响应单独构造，当前未显式设置此头。

---

## 5. 前端（PWA）

`web/` 下就是整个前端，零构建、零第三方依赖：

| 文件 | 作用 |
|---|---|
| `index.html` | 口令页 + 聊天页；PWA meta、iOS 主屏、安全区 |
| `app.js` | 登录、发消息、分段播、localStorage 历史、键盘适配 |
| `style.css` | 手机优先的布局与配色 |
| `manifest.json` | PWA 清单 |
| `sw.js` | service worker：只缓存静态壳 |
| `push.js` | 搭档通知授权、订阅、最近主动消息合并 |
| `.well-known/assetlinks.json` | 网站与安卓签名的关联声明 |
| `icons/icon-192.png`、`icons/icon-512.png` | 图标 |

### 分段出现

后端把回复切成 `parts`，前端**一段一段**冒出来：每段之前先亮“正在输入”，
新版只在段间停 60～160ms，减少完整回复到达后的额外等待，等待期间可继续编辑草稿。

节奏靠停顿，不靠动画。几段同时出现就又变回“一次性刷出来的机器人”了，
所以前端是串行 `Promise` 链，不是 `Promise.all`。

### 历史与缓存

- 新版聊天历史按服务端视角存为 `ella.history.<view_id>`，只留最近 100 条；旧 `ella.history` 只迁给搭档。待处理请求为 `ella.pending.<view_id>`；
- 右上角菜单可以「清空这段对话」，也可以「忘掉口令」；
- service worker 只缓存静态壳（HTML/CSS/JS/图标/清单），
  **绝不缓存 `/api/`**——聊天是实时的，缓存住就会“她永远说上一句”；
- 页面导航用“先给本地壳、后台再取新的”的策略，第二次打开瞬间出画面。

清空网页显示历史不会删除服务端 SQLite 的消息和长期记忆。换设备不会自动拉取服务端历史；
`push.js` 在页面初始化时尝试补入最近一条主动消息，不是完整聊天同步。
若通知点击只聚焦已经打开的窗口而未重新初始化页面，消息可能需要刷新后才补入。

当前视觉是冷白与青绿，保留深色模式。安卓 APK 加载同一网站，签名维护见 [ANDROID.md](ANDROID.md)。

### 手机端

- 高度用 `100vh → 100dvh → var(--vvh, 100dvh)` 三层兜底；
- JS 用 `visualViewport` 把键盘弹起后的真实高度写进 `--vvh`，
  解决 iOS Safari 上键盘遮住输入框的问题；
- `viewport-fit=cover` + `env(safe-area-inset-*)` 处理刘海和 home indicator。

---

## 6. 部署：Cloudflare Tunnel（历史部署记录）

| 项 | 值 |
|---|---|
| 域名 | `giftia.icu`，托管在 Cloudflare |
| NS | `garret.ns.cloudflare.com`、`luciane.ns.cloudflare.com` |
| 隧道 ID | `b9f5fd3e-f078-445d-afac-7f2219d0bf11` |
| 入口配置 | `giftia.icu → http://localhost:8111` |
| DNS | `CNAME giftia.icu → b9f5fd3e-f078-445d-afac-7f2219d0bf11.cfargotunnel.com`（已代理） |
| 服务器上的 cloudflared | systemd 服务，token 在 `/etc/cloudflared/token`，`enabled`，自动重连 |
| 服务器入站端口 | 只有 `22`（SSH） |

### 为什么服务器只开 22 也能上网站

隧道不是靠服务器开端口做端口映射，而是**服务器主动出站**连到 Cloudflare：

```
访客 ──HTTPS──► Cloudflare 边缘 ──隧道──► 服务器上的 cloudflared ──► 127.0.0.1:8111
```

所以：

- 公网没有暴露任何 Web 端口；
- 服务器安全组不需要开 80/443；
- 公网入口位于 Cloudflare 边缘。这是网络拓扑说明，不能仅由源站是否开放 80/443 推导备案义务。

### 安装 cloudflared 的坑

**不要在服务器上直接下载 cloudflared。** 实测 GitHub 会卡住，挂十几分钟没动静。

正确做法：**本机下载好，再 scp 上去**。二进制约 38MB，本地下载实测 48 秒。
到了服务器上再 `chmod +x`、装成 systemd 服务。

> 改完隧道入口配置后要重启 cloudflared 服务。具体 unit 名以安装时为准
> （默认常见的是 `cloudflared`），本文没有在服务器上核对过 unit 名。

---

## 7. `.env` 与凭据

### 键

| 键 | 用途 | 没有会怎样 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 模型 | 只能用 mock，真实对话起不来 |
| `QQ_BOT_APPID` | QQ 机器人 AppID | 仅恢复 QQ 时需要，`--web-only` 可空 |
| `QQ_BOT_SECRET` | QQ 机器人 AppSecret | 仅恢复 QQ 时需要，`--web-only` 可空 |
| `AILA_WEB_SECRET` | 搭档口令 | 网站入口不启动（QQ 照常） |
| `AILA_WEB_FRIEND_PASS` | 访客口令 | 只有搭档能登录，朋友进不来 |
| `VAPID_PRIVATE_KEY` | VAPID 私钥，服务端据此推导公钥并签名 | 可以聊天，但不能订阅 / 发送通知 |
| `VAPID_PUBLIC_KEY` | 密钥生成命令同时输出的公钥 | 当前服务端不直接读它，以私钥推导结果为准 |
| `VAPID_SUBJECT` | VAPID 联系标识 | 默认 `mailto:aila@giftia.icu` |
| `PUSH_RELAY_URL` | 推送中转完整地址，含 `/push` | 直连浏览器推送服务 |
| `PUSH_RELAY_SECRET` | 中转鉴权，匹配 Worker 的 `RELAY_SECRET` | 已配置中转但鉴权不匹配时，发送失败 |

`.env` 在项目根目录，已被 `.gitignore` 挡住；服务器上是 `/opt/giftia/.env`，权限 600。
`.env.example` 目前只列 DeepSeek 和 QQ，网页与推送键需要自行补充。下面只展示键名和占位值：

```ini
AILA_WEB_SECRET=替换为搭档口令
AILA_WEB_FRIEND_PASS=替换为另一套访客口令
VAPID_PRIVATE_KEY=替换为生成的私钥
VAPID_SUBJECT=mailto:你的联系邮箱
PUSH_RELAY_URL=https://你的Worker域名/push
PUSH_RELAY_SECRET=替换为与Worker一致的密钥
```

### 部署时不要覆盖服务器上的 `.env`

`deploy/setup.sh` 打 tar 时显式排除了：

```
--exclude=./.env
--exclude=./.env.*
```

**这不是洁癖。** 源码目录里本来就有一份 `.env`；不排除的话，部署会把本机这份
覆盖到服务器上，服务器上的凭据（尤其是只存在于服务器上的网页口令）就没了。
这是真发生过的事故。

同理，README 里手动打包的命令也带了 `.env` / `.env.*` 的排除。打包后建议再确认一次：

```powershell
tar tzf giftia-src.tar.gz | Select-String '\.env($|\.)|^\./data/'   # 期望：无输出
```

### 改服务器上的 `.env`：只能走 `put_env.sh`

不要在服务器上直接 `nano`，也不要用 `printf` 或 `sudo sh -c "cat >> ..."` 拼。
正确流程是：

```bash
# 本地写一个 LF 的 KEY=VALUE 文件（一行一个键），scp 上去
scp keys.env ubuntu@你的服务器IP:/tmp/keys.env

# 服务器上：逐键替换，然后重启
sudo bash /opt/giftia/deploy/put_env.sh /tmp/keys.env --restart
```

`put_env.sh` 内部做四件事：

1. 逐键删掉旧行（只删文件里出现的键，别的键一个字都不动）；
2. `sudo tee -a` 追加新行（重定向由 root 身份的 `tee` 做，不受外层 shell 权限影响）；
3. `chown giftia:giftia` + `chmod 600`；
4. 逐键回读确认，打印 `KEY✓` / `KEY✗`。

`--restart` 会 `systemctl restart giftia-qq`，等 6 秒再报 `is-active`。

### 两次真实事故，所以才有这个脚本

| 写法 | 实际发生了什么 |
|---|---|
| `printf "A=x\ny"` | `\n` 在某些 shell 里被吃掉，值变成字面量 `n`，口令直接失效 |
| `sudo sh -c "cat >> /opt/giftia/.env"` | 重定向由**外层非 root shell** 执行 → `Permission denied`；而前一步的删除已经生效 → 键全没了、服务起不来 |

规矩就一句：**本地写 LF 文件 → scp → `put_env.sh`**。全程不碰引号和转义。

### 当前安装脚本的启动条件

`setup.sh` 保留 `.env`、`data/` 和 `config/*.local.toml`，但会更新仓库默认配置及 service 模板。
它会安装并启用 `giftia-backup.timer`。第 6 步仍按 QQ 凭据是否齐全决定自动重启，
与当前 `--web-only` 模板存在不一致：纯网页部署配好模型和网页口令后，应手动
`sudo systemctl restart giftia-qq` 并检查本机 `/api/health`。
脚本不会替你生成 VAPID 密钥或部署 Tunnel / Worker。

### Web Push：只通知搭档

当前无 QQ 的主动联系路径是：每 600 秒检查理由和额度 → 生成一句话 →
记入同一个搭档会话并记额度 → `push_notify()` 保存最近消息、通知已订阅设备。
访客不能访问推送状态、订阅、最近消息和测试接口，均返回 403。

1. 首次配置时在可信终端运行 `python -m giftia.push --gen`，私钥写入服务器 `.env` 后重启。
   已有安装保留原私钥；生成新密钥不会迁移旧设备订阅。
2. 如服务器无法直连浏览器推送服务，部署 [`deploy/cf-push-relay/worker.js`](../deploy/cf-push-relay/worker.js)，
   Worker 设置 Secret `RELAY_SECRET`，服务端设置 `PUSH_RELAY_URL=https://…/push` 和匹配的 `PUSH_RELAY_SECRET`。
   Worker 只转发白名单推送域名；本项目历史上用它绕过源站直连 FCM 超时。
3. 用搭档口令登录支持通知的浏览器 / App，允许系统通知，点击“让她能主动找你”。
   前端缺少 `Notification` / `PushManager` 或权限已拒绝时会直接跳过按钮。
4. 用同一搭档会话调用 `POST /api/push/test` 查看 `sent / total`，再确认设备实际收到通知。

推送是空载荷唤醒，service worker 再用搭档 cookie 从 `/api/push/last` 取正文；取不到时显示兜底文案。
最多保留最近 12 个订阅，404 / 410 的失效订阅会清理。数据存于 `data/push_subs.json` 和 `data/push_last.json`。
`push.js` 只在初始化时合并最近一条消息到本机 `ella.history`，并用 `ella.pushMerged` 记录合并时间。

主动规则仍受 `[outreach]` 约束：仓库配置为每天最多 3 条、30 天最多 60 条、间隔至少 3 小时，
23:30～08:00 安静，临近终端服务的理由除外。`--web-only` 固定按 owner 规划，
不会按 `target="both"` 再给 QQ 群发一份。当前实现会在通知成功前记额度，失败不回滚。

### 自动备份

`giftia-backup.timer` 在服务器本地时间每天 04:00 起执行，随机延迟最多 300 秒，停机错过后补跑。
`deploy/backup_db.py` 用 SQLite 在线备份 API 写入 `data/backups/giftia-YYYY-MM-DD.db`，
保留最近 14 份，同日重跑覆盖当天备份，随后检查完整性并打印消息 / 记忆条数。

```bash
systemctl list-timers giftia-backup.timer --all
sudo systemctl start giftia-backup.service
journalctl -u giftia-backup --no-pager -n 30
sudo -u giftia /usr/bin/python3 /opt/giftia/deploy/backup_db.py --list
sudo -u giftia /usr/bin/python3 /opt/giftia/deploy/backup_db.py --check
```

自动任务只覆盖数据库，同机备份不覆盖磁盘丢失；回忆录、封存、推送 JSON、凭据、个人配置和 APK 签名材料需另行保存。
完整迁移应先停主服务再归档，或使用在线备份生成的一致数据库副本。

---

## 8. 已知限制

| 限制 | 具体表现 | 为什么 |
|---|---|---|
| **网站主回复串行** | 慢主模型/工具调用仍会排队；新版使用严格有界队列 | 当前对象与 SQLite 连接在主线程，维护计算/网络已隔离 |
| **超时继续执行** | 新版通过同一 request_id 查结果；执行中崩溃且无已落库回复时明确报中断 | 结果落盘保留 24 小时/最多 256 条，不宣称模型执行严格恰好一次 |
| **恢复 QQ 后共享阻塞** | 网站处理期间会延迟 QQ 收事件，当前暂停 QQ 时不涉及 | 入口共用主线程 |
| **历史只在本机** | 换设备看不到完整显示历史，推送仅补最近一条 | 尚无服务端历史拉取接口 |
| **订阅状态按所有设备汇总** | 一台设备订阅后，另一台设备可能不再显示订阅按钮 | `status.subscribed` 是总订阅数是否大于 0，没有核对本机订阅 |
| **通知接受不等于设备显示** | 新版仅在至少一个推送服务接受后记历史/额度，全失败恢复上一条通知 | 仍为无载荷推送与“最近一条消息”协议，没有设备展示回执 |
| **没有异地自动备份** | 当前定时器只保留源站同机数据库副本 | 未实现异地复制与自动恢复演练 |

另外几条来自代码的现实：

- 访客身份靠 cookie。清 cookie / 换浏览器就是新访客，旧分区还在但新身份看不到。
- 只有一套访客口令，朋友之间没有独立账号；拿到口令的人共享 `friend` 角色。
- 访客 id（`aila_visitor`）是客户端 cookie，服务端不签名；分区的隔离假设“cookie 里的 id 就是本人”。
  别把搭档的 openid 或别人的 visitor id 当公开信息。

---

## 9. 自测与排障

### 自测

```powershell
python tools/web_selftest.py     # 44 项：口令/全链路/静态文件/访客隔离/推送接口权限
```

它是真的起一个 HTTP 入口（`port=0` 让系统分配端口）、真的走队列，覆盖：

- 口令错误进不来、正确能进、cookie 不是明文；
- HTTP → 队列 → 主线程 → 她的全链路；
- 网站说的话进搭档会话（和 QQ 共享）；
- 静态文件（`/`、`app.js`、`style.css`、`manifest.json`、`sw.js`、图标）；
- 目录穿越、空消息、限流；
- 访客：独立身份、检索不到搭档私事、不进搭档会话、不动亲密度/心情。
- 推送：搭档能读状态、订阅与取最近消息，访客收到 403；不向真实推送服务发通知。

五个脚本的合计（2026-10-21 实测，均 0 失败；审计另有 3 条提示）：

| 脚本 | 项数 |
|---|---|
| `tools/selftest.py` | 320 |
| `tools/ws_selftest.py` | 17 |
| `tools/qq_protocol_selftest.py` | 77 |
| `tools/web_selftest.py` | 44 |
| `tools/audit.py` | 11 |
| **合计** | **469** |

测试不调用真实模型 / QQ。协议与网页套件需要允许本机回环连接；沙箱若报 `WinError 10013`，
应在允许本地套接字的环境复跑。本次这些套件已复跑通过；未重验线上 Tunnel、设备通知和备份定时器。

### 排障

| 现象 | 先看哪里 |
|---|---|
| 日志里没有 `[web] 网站入口已启动` | `journalctl -u giftia-qq -f`；`--web-port` 给了没？`AILA_WEB_SECRET` 配了没？ |
| 本机 `curl 127.0.0.1:8111/api/health` 不通 | 服务起来没、端口是不是 8111、`web/` 目录在不在 |
| 公网打不开、本机却正常 | cloudflared 的 systemd 服务、Cloudflare 的 DNS/隧道配置 |
| 登录一直 401 | 口令是不是改过；改口令会让旧 cookie 失效，重新登录 |
| 聊天 429 | 说太快，限流了；等一分钟 |
| 聊天 503 / `busy` | 等待队列过深，稍后重试 |
| 聊天 504 / 前端超时 | 检查排队深度、模型耗时和日志；任务可能还会执行，不要连续重复提交 |
| 页面 404 | `web/` 没部署全；用 `.\tools\check_sync.ps1` 逐文件对一遍 |
| 网页和原 QQ 感觉不同 | 检查搭档 / 访客身份、称呼和实际加载的人格配置；访客不具备搭档记忆 |
| “让她能主动找你”按钮不出现 | 搭档登录、浏览器能力 / 权限、VAPID 私钥、多设备汇总订阅状态；登录后可刷新页面 |
| 测试接口 200 却没通知 | 看 `sent / total`，再看 Worker `/push` 路径与密钥、上游状态和设备权限 |
| 备份没出现 | `systemctl list-timers giftia-backup.timer --all` 和 `journalctl -u giftia-backup`；核对服务器时区 |

她自己的日志里，网站相关的前缀是 `[web]`；处理请求出错会打印异常栈，
不会拖垮 QQ 那条线（`drain()` 里每个 job 单独兜底）。

---

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `giftia/webgate.py` | HTTP 入口、口令、限流、队列、静态文件 |
| `adapters/qq_official.py` | `--web-port` / `--web-only` 的接线，主循环里 `drain()` |
| `web/` | PWA 前端 |
| `giftia/push.py` | VAPID、推送发送、订阅与最近消息存储 |
| `tools/web_selftest.py` | 网站入口自测（44 项） |
| `deploy/giftia-qq.service` | systemd：`--web-port 8111 --web-only` |
| `deploy/backup_db.py`、`deploy/giftia-backup.*` | 在线热备、轮转、校验与每日定时器 |
| `deploy/cf-push-relay/worker.js` | 浏览器推送服务的 Cloudflare 中转 |
| `docs/ANDROID.md` | [安卓套壳与签名维护](ANDROID.md) |
| `deploy/put_env.sh` | 安全改服务器 `.env` |
| `README.md` | [项目总览](../README.md)；网页入口那一节链到这里 |
