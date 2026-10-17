# 网页入口（giftia.icu）

除了 QQ，她还有一个网页入口：<https://giftia.icu>。

不是另起一个服务，也不是复制一个她。`giftia/webgate.py` 在**同一个进程**里开了一个
HTTP 线程：请求排进队列，由主线程交给她执行。所以网站上的你和 QQ 上的你，
面对的是**同一份记忆、同一个会话上下文、同一条寿命**。

> 本文写的是当前代码和已经上线的部署。端口、口令名、项数都以仓库里的实现为准；
> 代码改了，先改代码，再回来改这里。

---

## 1. 架构：一个进程，两个入口

```
QQ WebSocket 循环（主线程）                    HTTP 线程（辅助）
────────────────────────────                  ─────────────────────────────
收事件 → 处理 → 回 QQ                          POST /api/login  → 验口令、发 cookie
   │                                           POST /api/chat   → 验 cookie、限流、入队
   │  每个 tick：                              GET  /api/health → 活着
   ├── web_gate.drain(settings) ◄──── 队列 ◄──┤ GET  /*         → 静态文件（PWA 壳）
   │        │                                  └─ 等结果（最多 60 秒）
   │        └── 最多 2 个/次 → handle_message(scene="c2c" / "guest")
   └── 继续收 QQ
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

真正的执行在主循环里：`self.web_gate.drain(self.settings)`，每 tick 最多处理
`DRAIN_PER_TICK = 2` 个请求。网站在等回话时，WebSocket 那边的 `recv` 最多睡 0.2 秒
就回来排空一次——让排队的人不用干等，同时每 tick 只做 2 个，别让一次排空把 QQ 收消息卡太久。

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
| 和 QQ 一起开 | `python adapters/qq_official.py --web-port 8111` | 服务器上的 systemd 就是这么配的 |
| 只跑网站 | `python adapters/qq_official.py --web-port 8111 --web-only` | 不连 QQ，适合本机调试 |
| 看入口活着没 | `curl http://127.0.0.1:8111/api/health` | 返回 `{"ok": true}` |

`--web-port 8111` **只监听 `127.0.0.1`**，不对外开端口。公网访问走 Cloudflare Tunnel
（见第 6 节），服务器本身不需要暴露 8111。

没配 `AILA_WEB_SECRET` 时，`--web-port` 不会开入口：日志会提示去 `.env` 里加口令，
QQ 那条线照常跑。`--web-only` 则必须同时给 `--web-port` 和口令，否则直接退出。

> **代码现状的一个小别扭**：`--web-only` 确实不连 QQ，但 `main()` 在进入 `run()` 之前
> 仍会检查 `QQ_BOT_APPID` / `QQ_BOT_SECRET` 是否非空。空着会以「缺少凭据」退出
> （帮助文本写的是“本机调试不用凭据”，两处不一致）。本机调试时保留已有凭据，
> 或先填两个非空占位值；占位值从代码看能跑，但没在空凭据的机器上实跑过。

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

### 访客：看得见自己，看不见你

用 `AILA_WEB_FRIEND_PASS` 登录后：

- `scene="guest"`，身份是 `aila_visitor` cookie 里的独立 id；
- 视角只有 `person(他自己)`——**检索不到搭档的私事**；
- 写进他自己的记忆分区，**不进搭档的会话**；
- `is_handler=False` → 心情、亲密度、寿命**一点都不动**；
- 限流额度只有搭档的一半（他花的是搭档的 token）。

访客第一次登录时，服务端会发一个随机的 `aila_visitor`（16 个十六进制字符）。
换浏览器或清掉 cookie，就是另一个访客了——旧分区还在，但新身份看不到它。

### 为什么必须两套口令

如果只有一个口令，朋友进来就是“以搭档的身份”：

| 会发生什么 | 为什么 |
|---|---|
| 她当着他的面提起你的私事 | 他拿到的是搭档的视角 |
| 他的话写进你的会话 | 同一个 c2c 会话上下文 |
| 你的心情、关系、寿命被陌生人推进 | `is_handler=True`，一切照常结算 |

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

## 4. 接口

| 方法 | 路径 | 请求 | 成功 | 失败 |
|---|---|---|---|---|
| `POST` | `/api/login` | `{"pass": "..."}` | `200 {"ok":true,"role":"owner"\|"friend"}` + `Set-Cookie` | 口令错 `401`（强制延迟 0.4 秒） |
| `POST` | `/api/chat` | `{"text": "..."}` | `200 {"reply":"...","parts":[...]}` | 未登录 `401`；超频 `429`；空消息 `400`；60 秒没处理完 `504`；执行出错 `502` |
| `GET` | `/api/health` | — | `200 {"ok":true}` | — |
| `GET` | `/*` | — | 静态文件 | 目录穿越/不支持的后缀 `404` |

几个实现细节：

- 聊天文本最多 2000 字，超出截断。
- 口令错误也慢 0.4 秒再回，别让它成为一个高速爆破口。
- `parts` 是后端把回复按换行和句末标点切出来的 1～3 段；前端一段一段播。
- 静态文件按后缀白名单发（html/js/css/json/png/jpg/svg/ico/webmanifest/txt/woff2），
  解析后的路径必须还在 `web/` 里，否则 404。
- HTML/JS/CSS 用 `no-cache`，改完刷新就能看到；图标之类缓存一天。
- API 响应统一 `Cache-Control: no-store`。

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
| `icons/icon-192.png`、`icons/icon-512.png` | 图标 |

### 分段出现

后端把回复切成 `parts`，前端**一段一段**冒出来：每段之前先亮“正在输入”，
随机停 500～900ms，再让气泡落下来。

节奏靠停顿，不靠动画。几段同时出现就又变回“一次性刷出来的机器人”了，
所以前端是串行 `Promise` 链，不是 `Promise.all`。

### 历史与缓存

- 聊天历史存 localStorage（`ella.history`），只留最近 100 条；
- 右上角菜单可以「清空这段对话」，也可以「忘掉口令」；
- service worker 只缓存静态壳（HTML/CSS/JS/图标/清单），
  **绝不缓存 `/api/`**——聊天是实时的，缓存住就会“她永远说上一句”；
- 页面导航用“先给本地壳、后台再取新的”的策略，第二次打开瞬间出画面。

### 手机端

- 高度用 `100vh → 100dvh → var(--vvh, 100dvh)` 三层兜底；
- JS 用 `visualViewport` 把键盘弹起后的真实高度写进 `--vvh`，
  解决 iOS Safari 上键盘遮住输入框的问题；
- `viewport-fit=cover` + `env(safe-area-inset-*)` 处理刘海和 home indicator。

---

## 6. 部署：Cloudflare Tunnel（已上线）

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
- **不需要 ICP 备案**——境内 IP 上没有 80/443 在对外服务，公网入口在 Cloudflare 边缘。

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
| `QQ_BOT_APPID` | QQ 机器人 AppID | 适配器不启动 |
| `QQ_BOT_SECRET` | QQ 机器人 AppSecret | 适配器不启动 |
| `AILA_WEB_SECRET` | 搭档口令 | 网站入口不启动（QQ 照常） |
| `AILA_WEB_FRIEND_PASS` | 访客口令 | 只有搭档能登录，朋友进不来 |

`.env` 在项目根目录，已被 `.gitignore` 挡住；服务器上是 `/opt/giftia/.env`，权限 600。
`.env.example` 目前只列了 DeepSeek 和 QQ 两组，网页这两组要自己加。

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

---

## 8. 已知限制

| 限制 | 具体表现 | 为什么 |
|---|---|---|
| **网站请求是串行的** | 多个访客同时发，会排队一条一条处理（每条大约 1～5 秒）。10 个人同时说话，最后一个可能等 40 秒甚至 504 | 她不是线程安全的；并行只有“给每个访客一个独立的她”，那就不是同一个艾拉了 |
| **QQ 会被阻塞** | 网站请求处理期间，QQ 那条线同样停下来等 | 两边在同一个主线程里执行 |
| **还没有 Web Push** | 她不会主动弹通知找你，只能你点开网页 | 现在只有请求-响应，没有推送通道 |
| **还没有自动备份** | 她的全部数据是服务器上 `/opt/giftia/data/giftia.db` 一个文件，零副本；README 里有手动 `tar` 命令，但要你自己跑 | 备份要单独做；删掉 `data/` 就等于抹掉一切 |

另外几条来自代码的现实：

- 访客身份靠 cookie。清 cookie / 换浏览器就是新访客，旧分区还在但新身份看不到。
- 只有一套访客口令，朋友之间没有独立账号；拿到口令的人共享 `friend` 角色。
- 访客 id（`aila_visitor`）是客户端 cookie，服务端不签名；分区的隔离假设“cookie 里的 id 就是本人”。
  别把搭档的 openid 或别人的 visitor id 当公开信息。

---

## 9. 自测与排障

### 自测

```powershell
python tools/web_selftest.py     # 36 项：口令门禁/全链路/静态文件/访客隔离
```

它是真的起一个 HTTP 入口（`port=0` 让系统分配端口）、真的走队列，覆盖：

- 口令错误进不来、正确能进、cookie 不是明文；
- HTTP → 队列 → 主线程 → 她的全链路；
- 网站说的话进搭档会话（和 QQ 共享）；
- 静态文件（`/`、`app.js`、`style.css`、`manifest.json`、`sw.js`、图标）；
- 目录穿越、空消息、限流；
- 访客：独立身份、检索不到搭档私事、不进搭档会话、不动亲密度/心情。

五个固定项数脚本的合计（2026-10-17 实测）：

| 脚本 | 项数 |
|---|---|
| `tools/selftest.py` | 320 |
| `tools/ws_selftest.py` | 17 |
| `tools/qq_protocol_selftest.py` | 77 |
| `tools/web_selftest.py` | 36 |
| `tools/audit.py` | 11 |
| **合计** | **461** |

### 排障

| 现象 | 先看哪里 |
|---|---|
| 日志里没有 `[web] 网站入口已启动` | `journalctl -u giftia-qq -f`；`--web-port` 给了没？`AILA_WEB_SECRET` 配了没？ |
| 本机 `curl 127.0.0.1:8111/api/health` 不通 | 服务起来没、端口是不是 8111、`web/` 目录在不在 |
| 公网打不开、本机却正常 | cloudflared 的 systemd 服务、Cloudflare 的 DNS/隧道配置 |
| 登录一直 401 | 口令是不是改过；改口令会让旧 cookie 失效，重新登录 |
| 聊天 429 | 说太快，限流了；等一分钟 |
| 聊天 504 | 她那边排队或正在处理别的请求；这是串行的代价，不是网络超时 |
| 页面 404 | `web/` 没部署全；用 `.\tools\check_sync.ps1` 逐文件对一遍 |

她自己的日志里，网站相关的前缀是 `[web]`；处理请求出错会打印异常栈，
不会拖垮 QQ 那条线（`drain()` 里每个 job 单独兜底）。

---

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `giftia/webgate.py` | HTTP 入口、口令、限流、队列、静态文件 |
| `adapters/qq_official.py` | `--web-port` / `--web-only` 的接线，主循环里 `drain()` |
| `web/` | PWA 前端 |
| `tools/web_selftest.py` | 网站入口自测（36 项） |
| `deploy/giftia-qq.service` | systemd：`ExecStart` 带 `--web-port 8111` |
| `deploy/put_env.sh` | 安全改服务器 `.env` |
| `README.md` | [项目总览](../README.md)；网页入口那一节链到这里 |
