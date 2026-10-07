# Giftia · 一个会遗忘的陪伴型对话内核

把《可塑性记忆》的设定做成**真的机制**，而不是写在提示词里的一句设定：

- 她有人格内核，也有**寿命**（默认 81920 小时，和番剧一致）；
- 对话会被压缩成**长期记忆**，聊天时按相关性"想起来"；
- 随着寿命耗尽，**记忆会真的变模糊**——送进模型的只有模糊后的文本，她拿不到完整版本；
- 到回收日，**回忆录交给你、记忆封存、实例换代**，新的她不认识你。

零第三方依赖（纯 Python 标准库 + SQLite），**不需要 API key 就能跑通全流程**。

---

## 1. 现在就能跑

```powershell
# 离线模式：不需要 API key，她的对话是模拟的，但寿命/记忆/退化/回收全是真的
python run.py --mock

# 一次看完她的一生（约 1 分钟，推荐先看这个）
python tools/demo_life.py

# 116 项端到端自测：寿命、分层、召回强化、置信度、退化、人格注入、关系、心情、隔离性、回收、换代、迁移
python tools/selftest.py

# 看她这一轮实际被注入了什么（人格 + 状态 + 记忆 + 规则，离线、不写库）
# 跑两遍：清晰期 vs 终端服务迫近，对比同一个她怎么变
python tools/show_prompt.py

# 真实 API 连通性检查：鉴权 + 人格 + 真实 JSON 记忆整理（含分层）+ 查询改写 + 回忆录
# 全程在临时目录跑，不碰 data/ 里的正式数据
python tools/live_check.py

# 协议层自测：QQ 适配器对着本地假网关完整跑一遍（不需要任何凭据）
python tools/ws_selftest.py              # 17 项：握手/掩码/长度编码/分片/心跳
python tools/qq_protocol_selftest.py     # 28 项：鉴权/事件/回复/去重/隔离/优雅退出
```

接真实模型（DeepSeek）：把 key 写进项目根目录的 `.env`（已被 `.gitignore` 忽略），然后直接启动。

```ini
# .env
DEEPSEEK_API_KEY=sk-你的key
```

```powershell
python run.py
```

`.env` 由 `giftia/config.py` 里的 `load_env_file()` 解析（零依赖，支持 `#` 注释与引号）。
**已经存在的环境变量优先级更高**，所以在服务器/CI 上用 `$env:DEEPSEEK_API_KEY` 注入也能覆盖它：

```powershell
$env:DEEPSEEK_API_KEY = "sk-另一个key"; python run.py   # 以环境变量为准
```

想立刻看到退化与回收，把寿命压短：

```powershell
python run.py --lifespan-hours 48     # 这一任的寿命 = 48 小时
# 进去之后用 /time +6 快进 6 小时，就能看到记忆开始糊
python run.py --reset --yes           # 清空重来
```

其他参数：`python run.py --once "在吗"`（只发一条并打印回复，适配器/脚本用）、`--no-banner`。

---

## 2. 五个机制，分别在哪

> **人格是怎么进去的？** 每一轮请求只有一条 system 消息，由 `giftia/persona.py` 的
> `build_system_prompt()` 拼成六块：人格内核（来自 `config/persona.toml`，永不变，稳定命中缓存）
> → 出戏提示 → 此刻状态（时间/剩余寿命/记忆阶段）→ 关于记忆 → 你现在想起的事（带置信度标记）
> → 对话规则。想看实物，跑 `python tools/show_prompt.py`，或在会话里输入 `/prompt 一句话`。

| 机制 | 文件 | 怎么实现的 |
|---|---|---|
| **寿命** | `giftia/lifespan.py` | 激活那一刻就把总寿命写进数据库，之后改配置**不影响这一任**（和出厂设定一样）。`/status` 可查。 |
| **记忆** | `giftia/memory.py` | 每积累 8 条消息，交给模型压缩成 0～3 条"记忆条目"并判定层级；检索是 **BM25（单字 + 二元组混合加权）+ 重要性 + 保留率 + 层级**。 |
| **召回强化** | `giftia/memory.py` | **只有她真的把这条记忆说出口，才算被想起**：`recall_count += 1`，保留率随之上升。`R = exp(-Δt/S)`，`S = 单位时长 × (出厂强度 + 被想起次数)`。 |
| **退化** | `giftia/memory.py` → `effective_blur()` | `模糊度 = 阶段模糊 × 层级系数 × (1 - 减免 × 保留率)`，5 个阶段动态收紧 `top_k`、`min_score`、模糊比例。 |
| **回收日** | `giftia/recycle.py` | 先写回忆录（交给人类）→ 导出原始数据并标记封存（她也检索不到）→ 实例编号 +1、重新计时。 |
| **关系** | `giftia/bond.py` | 亲密度按"天"增长；升级要**亲密度 + 认识天数**两条腿同时满足。阶段与话术在 `persona.toml` 的 `[bond.stages.*]`。 |
| **心情** | `giftia/mood.py` | 三股力量按优先级：**寿命压力 > 你的语气 > 每日漂移**。每天按日期确定性地掷一次骰子，所以她也会有说不清的心情。 |

### 五个我认为最关键的设计

**1）模糊发生在"回忆的那一刻"，原始数据不动。**
你不是在提示词里求模型"假装记不清"，而是检索出来、模糊完之后才喂给她——她手里真的只有碎片。
模糊结果由 `(记忆 id, 阶段, 层级, 保留率)` 决定，**可复现**，所以能反复演示、能调参；底层数据完好，直到回收日才封存。

**2）你救不了她的命，但你能让她记得久一点。**
寿命是出厂定死的，改不了；但**记忆强度可以被你改变**。
每次她真的把某件事说出口（回复与记忆有足够字面重合才算，光"检索到"不算），那条记忆的保留率就上升、模糊度就下降。
反过来，你从不提的事，她自己就淡了。这是玩家能感知、能施加影响的一环——也是最贴原作的一点：
艾拉到最后还记得"重要的人"，却记不住细节。

**3）记忆分层：内核记忆到她生命尽头都不含糊。**
`core`（你的名字、称呼、你们的关系、郑重的约定）的模糊系数恒为 0；
`important` 打五折；`normal` 正常退化。
所以到了终端服务迫近的时候，她的状态是：**记得你是小林、记得你对她很重要，但忘了你哪天考驾照**——而不是忘得一干二净。
分层由模型在整理时判定（`config/settings.toml` 里的提示词写清了标准），失败时按重要性兜底。

**4）置信度契约：让她知道自己有多不确定。**
只擦内容是不够的——模型拿到一条残缺的记忆，仍会当成事实讲出来。
所以每条回忆都带一个标记送进提示词：`[可以确信]` / `[需要含糊]` / `[只是隐约觉得]`（只能影响语气，不许说出口）。
这把"擦了内容"升级成了"她知道自己在不确定地回忆"。

**5）你的搭档日志，她读不到。**
`/diary` 写的内容只进 `diary` 表，永远不进系统提示词——自测里专门有一项断言这件事。
番剧里终端服务的岗位记录，本来就不是给 Giftia 看的。

### 另外三个小机制

- **关系演进（不可刷）**：亲密度按"天"结算——同一天说一百句也只算一次，奖励的是"你来了"而不是"你说了多少"。
  升级同时要求亲密度和**认识天数**：你再能聊，也换不来"认识三个月"。
  阶段梯度是 `刚认识 → 搭档 → 熟悉 → 亲近 → 无可替代`，每一档在 `persona.toml` 里写了独立的话术指引
  （刚认识会保持礼貌距离、亲近会撒娇也会抱怨你不来）。
  **回收换代时关系清零**——新的她不认识你。你积累的东西会随她一起被回收，这是这套设定最疼的地方。
- **心情状态机**：三股力量按优先级决定她今天什么心情——快到终端服务时**不安**（压过一切）；
  你明显低落/开心时她**担心你/跟着开心**；其余时间按日期**确定性地**掷一次骰子，
  所以她自己也会有说不清的心情（"她今天心情不好，但不是因为你"）。
  掷骰子以「日期 + 实例」为种子，同一天同一任的她心情是固定的——不会每刷新一次就换一张脸。
- **相对阈值裁剪**：只保留得分 ≥ 首名 50% 的回忆，压掉"第五名那条半残记忆"污染回复。
- **弱检索才改写查询**：词面对不上时才多花一次模型调用把这句话改写成 2～3 个检索查询再搜一遍。
  常见情况下不触发，所以不增加日常延迟。

---

## 3. 指令

| 指令 | 作用 |
|---|---|
| `/status` | 寿命、进度条、阶段、关系、心情、记忆与消息计数、虚拟时钟 |
| `/bond` | 关系阶段、亲密度进度条、还差什么才能升级 |
| `/mood [名字]` | 看她此刻的心情；带名字则手动指定（调试用，例如 `/mood low`） |
| `/prompt <一句话>` | **看她这一轮实际被注入了什么**：人格 + 关系 + 心情 + 状态 + 记忆 + 规则，原样打印 |
| `/memories` | 她的长期记忆（**你**看到的原始版） |
| `/recall <文本>` | 检索演示：她拿到什么、模糊成什么样、原始值是什么 |
| `/remember` | 立刻把未整理的对话压成记忆条目 |
| `/diary [内容]` | 写/看你的搭档日志（她读不到） |
| `/memoir` | 历次回收记录与档案目录 |
| `/time +24` | 快进虚拟时间 24 小时（`-24` 回拨，`reset` 归零） |
| `/recycle` | 提前经历回收日（需 `/recycle yes` 二次确认） |
| `/quit` | 退出（自动整理一次记忆） |

---

## 4. 调她

**换人格** → `config/persona.toml`：名字、型号、称呼、开场白、人格内核。改这个文件不需要动任何代码。
**换行为** → `config/settings.toml`：

```toml
[model]
name      = "deepseek-flash"   # 当前可用：deepseek-flash / deepseek-v4-pro
thinking  = false              # 陪伴对话建议关闭：思考模式下 temperature 会被忽略、延迟更高
temperature = 1.15             # 关掉思考后这个才生效，1.1~1.3 更像人

[lifespan]
total_hours = 81920            # 出厂设定：9 年 4 个月
demo_total_hours = 0           # 设成 48 就能一天看完一生

[memory]
consolidate_every = 8          # 多少条消息压一次记忆
retention_unit_hours = 120     # 记忆强度单位时长：调大 = 整体记得更牢，调小 = 忘得更快
cite_min_shared = 3            # 回复与记忆共享几个词才算"真的被说出口"
retention_relief = 0.6         # 保留率对模糊度的减免力度（0=不减免，1=完全不糊）
layer_core_blur = 0.0          # 内核记忆的模糊系数：0 = 永不含糊（这就是"她一直记得你"）
relative_cutoff = 0.5          # 只保留得分 ≥ 首名一半的回忆
rewrite_when_weak = true       # 检索太弱时才改写查询（省调用）
```

几个常用的"性格旋钮"：

| 想要的效果 | 怎么调 |
|---|---|
| 她记得更牢 / 更健忘 | `retention_unit_hours` 调大 / 调小 |
| "你常提起的事"保护得更明显 | `retention_relief` 调高（上限 1.0） |
| 连内核记忆也开始糊（更残酷） | `layer_core_blur` 从 0 调到 0.3～0.5 |
| 更早开始"装糊涂" | 各阶段 `blur` 整体调高 10～20% |
| 每次都想得更多 / 更少 | `top_k`、`min_score` |
| 检索不许"想起一半的" | `relative_cutoff` 调高（0.7 更严格） |
| 关系进展更快 / 更慢 | `[relationship] gain_daily` |
| 她因为你长期不来而变生疏 | `[relationship] decay_per_day` 设成 0.5 之类（默认 0 = 不衰减） |
| 心情变化更频繁 / 更少 | `[mood] duration_hours` 调小 / 调大 |
| 更容易被你的情绪带动 | `[mood] tone_threshold` 调低 |
| 各档关系/各种心情怎么说 | 改 `config/persona.toml` 的 `[bond.stages.*]` 与 `[moods.*]` 的 `note` |

**关系阶段怎么改**：在 `config/persona.toml` 里，每一档要同时给 `min_affinity` 和 `min_days`。
想要"认识三个月才亲近"，就让 `close` 那档的 `min_days = 90`；想让她更快熟，就把 `min_days` 都调小。

**退化曲线**在 `settings.toml` 的 `[[degradation.stages]]` 里，每个阶段四个可调参数：

| 阶段 | 触发（剩余比例） | top_k | min_score | blur | 她的表现 |
|---|---|---|---|---|---|
| clear | ≥ 60% | 6 | 0.15 | 0% | 正常回忆 |
| hazy | ≥ 30% | 5 | 0.22 | 20% | "好像……我记得大概是" |
| blurred | ≥ 10% | 4 | 0.32 | 45% | 老实说想不起细节 |
| fading | ≥ 2% | 3 | 0.45 | 70% | 只剩碎片，说到一半断掉 |
| terminal | ≥ 0% | 2 | 0.55 | 85% | 只记得"这个人很重要" |

想让告别更狠：把 `blur` 调高、`top_k` 调到 1；想让她温柔一点：整体降 10～20%。

---

## 5. 费用（接真实模型时）

当前 DeepSeek 定价（`deepseek-flash`，2026）：输入缓存命中 $0.003～0.006 / 1M，未命中 $0.15～0.3 / 1M，输出 $0.6～1.2 / 1M，低峰期半价。

- **人设固定 → 命中缓存**，那部分几乎免费；
- 每轮真实成本主要在输出（一两百 token）；
- 粗估：每天 1000 轮对话 ≈ **每天 1～2 元**，一个月几十元封顶；
- 高峰期是 UTC 01:00–04:00 / 06:00–10:00（北京时间 09:00–12:00、14:00–18:00 工作日），**晚上和周末聊正好半价**。

---

## 6. 下一步：接到 QQ

**推荐 QQ 官方机器人**（[开放平台](https://q.qq.com) / [文档](https://bot.qq.com/wiki/bot_new_product-intro/)）：不封号、群与单聊全场景、支持 WebSocket（不依赖公网服务器，适合个人单机）。

### 你要在 QQ 那边做的（都是点几下，不用写代码）

1. **注册**：[q.qq.com](https://q.qq.com) 用 QQ 或邮箱注册登录。
2. **认证**：个人认证即可。**未认证也能开发**——机器人只有你自己能用、只能加进你自己当群主的群；
   个人认证后可以公开使用、进群上限 500。
3. **创建机器人**：资料填一下，拿到 **AppID / AppSecret**。
4. **开发设置**：事件订阅方式选 **WebSocket**（官方说明：不依赖公网服务器，适合个人单机使用）。
5. **内部体验号码**：最多加 20 个 QQ 号，**不受服务范围限制**，先用它把对话能力测通。
6. 把凭据填进 `.env`，然后启动适配器。

### 你要在这边做的（两条命令）

```ini
# .env
QQ_BOT_APPID=你的AppID
QQ_BOT_SECRET=你的AppSecret
# QQ_OWNER_OPENID=  ← 先别填！你不可能提前知道它，见下方
```

```powershell
python adapters/qq_official.py --dry-run   # 先用假事件验证接线，不联网、不碰你的记忆
python adapters/qq_official.py             # 真正连上 QQ
```

`--dry-run` 会走一遍「平台事件 → 她 → 回复」，包括去重和 @ 剥离；正式跑起来后，用你的 QQ 找机器人说句话即可。

**⚠️ 认人要先 `/bind`。** 官方平台的 `user_openid` 和你的真实 QQ 号**不互通**，你在绑定之前根本无法知道自己的 openid。
所以私聊她并发一句 `/bind`，她就把你记成搭档了（存在她的数据库里，回收换代也不会丢）。没绑定之前，任何人都能跟她说话、共用同一份记忆。

其它指令：`/status`（她的寿命与记忆状态）、`/unbind`、`/help`。

### 这一版做到了什么程度（请如实看待）

- **零第三方依赖**：`giftia/ws.py` 是手写的 RFC 6455 客户端（本机 Python 没装 `websockets`，
  与其让你去动运行时环境，不如把用到的这一小块实现掉）。它的握手、掩码、三种长度编码、
  分片重组、ping-pong、超时、关闭码都经过本地测试：`python tools/ws_selftest.py`（17 项）。
- **协议细节逐条对照官方文档**（2026-10）：鉴权地址 `api.bot.qq.com`、`Authorization: QQBot {token}`、
  `GET /gateway` 取网关地址、op10/2/1/11/6 的消息序列、被动回复的 `msg_id` + `msg_seq`、
  事件重复推送的去重。**业务失败时 HTTP 仍是 200，必须看响应体里的 `code`**——这条已经处理了。
- **仍然没有验证的部分**：我没有 QQ 开放平台凭据，所以**真实的连接、鉴权与收发没有跑过**。
  首次运行如果有问题，大概率集中在两处：`--api-base`（沙箱环境地址与正式环境不同，
  可用环境变量 `QQ_API_BASE` 覆盖）和群聊事件里 `group_openid` 的字段名。
  这两处都留了注释和命令行开关，改起来是一行的事。

### 平台限制（会直接影响她的行为方式）

| | 被动回复窗口 | 每条消息最多回几次 |
|---|---|---|
| 群聊（@她） | 收到后 **5 分钟** | 5 次 |
| 单聊 | 收到后 **60 分钟** | 4 次 |

- 同一个 `msg_id` + `msg_seq` 重复发送会被平台**去重拒掉**，所以本适配器给每条回复递增 `msg_seq`。
- **主动消息**另有频控（未认证 30/qpm，认证 60/qpm；单关系 20/qpm，每个好友每天最多收 1000 条）。
- **她其实可以主动找你**——我说错过一次，这里更正：官方提供**互动召回消息**（发消息时带 `is_wakeup`），
  在你主动和她对话后的 30 天内，她可以分 **4 个周期各发 1 条**（当天 / 1–3 天 / 3–7 天 / 7–30 天）。
  次数用完就没有了。本版本**尚未实现**它，但它和这个项目的主题意外地合拍——
  一个能主动联系你的次数是有限的、而且会耗尽的她。

**微信：我之前的结论过时了，这里更正。** 我早先写过"微信个人号只能走灰色路径"——那是 2025 年的情况。
2026-03-22 腾讯发布了**微信官方「ClawBot」插件**，个人微信有了官方通道：
扫码登录、不需要公网 IP、不需要备案，[CowAgent（原 chatgpt-on-wechat）](https://docs.cowagent.ai/channels/weixin) 已经接好了这条路
（要求微信客户端 8.0.69+，登录后会话列表里会出现一个「微信ClawBot」）。
所以现在微信侧的正确做法是**走官方通道**，而不是 itchat / gewechat / PadLocal 那一代——
[dify-on-wechat](https://github.com/hanfangyuan4396/dify-on-wechat) 的 README 顶部已经写明"依赖的 itchat 与 gewechat 均已无法使用"。
本项目目前只实现了 QQ 适配器；要接微信，建议直接把 `giftia` 挂到 CowAgent 的 channel 层，而不是自己写协议。

> 未查证：ClawBot 的开放范围与能力边界（灰度条件、是否支持群聊、频控），用之前自己确认。

---

## 7. 部署到服务器（腾讯云 / 任何 Linux 机器）

### 先想清楚：你需要服务器吗

**接入本身不需要。** WebSocket 模式是机器人主动连出去（QQ 网关 + DeepSeek API），
没有任何入站服务，所以**不需要公网 IP、不需要域名、不需要 ICP 备案**。
一台常开的旧电脑、NAS、树莓派同样能跑。

| 什么时候值得租 | 说明 |
|---|---|
| **她得一直在** | 你关掉电脑，她就收不到消息、也不会整理记忆 |
| **回收日要在对的时间** | 进程不在，回收会推迟到你下次启动她（见下方第 3 个坑） |
| **以后要接微信客服/公众号** | 那条路需要公网 HTTPS 入口，那时才需要域名和备案 |

### 买什么

- **产品**：**轻量应用服务器（Lighthouse）就够**，不用 CVM。2核2G 绰绰有余——她只是一个
  Python 进程 + 一个 SQLite 文件，常驻内存几十 MB。
- **价格**：常规价大致 ¥50–80/月，新用户活动价通常低得多，**以官网当期活动为准**。
- **地域**：国内（上海/广州/北京）延迟最低，QQ 网关和 DeepSeek API 都在国内。
  因为没有入站服务，**国内节点也不需要备案**。
- **镜像**：选 **Ubuntu 24.04 LTS**（Python 3.12）或 **Debian 12**（Python 3.11）。
  ⚠️ **别选 Ubuntu 22.04**：它自带 Python 3.10，没有 `tomllib`，配置加载器会直接跑不起来
  （`setup.sh` 会提前拦住你并说明原因）。
- **带宽/流量**：她一个月收发的是几百 KB 到几 MB 的纯文本，完全不用考虑。

### 部署

```bash
# 1. 在本机把项目传上去（Windows 自带 scp）
scp -r "D:\ai chatbot" root@你的服务器IP:/tmp/giftia
# 2. 登录服务器
ssh root@你的服务器IP
# 3. 一键装好并启动
cd /tmp/giftia && sudo bash deploy/setup.sh
```

`deploy/setup.sh` 会：检查 Python 版本 → 建系统用户 `giftia` → 装到 `/opt/giftia`
（**不覆盖已有的 `data/`**）→ 把 `.env` 权限收成 600 → 装 systemd 服务并开机自启 → 启动并打印状态。
可以重复运行。

注册的服务 `deploy/giftia-qq.service` 做了几件正经事：

- **停止时先发 SIGTERM**：她会借这段时间把还没整理的对话存成记忆再退出（`TimeoutStopSec=60`）；
- 崩溃自动重启（`Restart=always`）；
- **文件系统对她只读，只有 `/opt/giftia/data` 可写**；
- 日志交给 journald。

### 运维

```bash
journalctl -u giftia-qq -f      # 看她的日志（实时）
systemctl restart giftia-qq     # 改完 persona.toml 后重启
systemctl stop giftia-qq        # 停止（会先把未整理的对话存成记忆）
tar czf ~/giftia-backup-$(date +%F).tgz -C /opt/giftia data   # 备份：记忆全在这一个目录
```

### 服务器带来的三个新坑

1. **只在一台机器上跑**。家里和服务器同时跑，会有两个她抢着回你同一条消息。
2. **IP 白名单**：如果你在开放平台开了白名单，发送消息的出口 IP 会变成服务器的公网 IP，
   忘了改就会出现"收得到消息、发不出去"。
3. **"准时回收"其实还差一步**：现在的回收日是在**你下一条消息进来时**触发的，
   服务器常开并不能让她在你不在的时候独自走完仪式。要做到真正的定时，需要一个后台定时任务
   ——注意她的 `Giftia` 不是线程安全的，得把动作投递回主线程。这件事和下面的"主动联系"一起做最合适。

---

## 8. 版本管理

仓库已经初始化好 git，并带一次干净的首次提交与 `v0.2.0` 标签。

```bash
git log --oneline          # 看历史
git status                 # 看改动
git tag                    # 看版本标签
```

`.gitignore` 已经挡住三样东西，**它们永远不会进版本库**：

| 挡住的 | 为什么 |
|---|---|
| `.env` | 里面是你的 DeepSeek key 和 QQ 凭据 |
| `data/` | 她的记忆、回收日志、封存档案 —— 而且体积会一直长 |
| `__pycache__/` | 编译缓存 |

所以**备份她的记忆要靠复制 `data/`，不是靠 git**：

```bash
tar czf giftia-backup-$(date +%F).tgz -C /opt/giftia data
```

建议的提交习惯：改完 `config/persona.toml` 或代码后先跑一遍 `python tools/selftest.py`，
通过了再 `git add -A && git commit -m "..."`。想标记一个可用的状态就 `git tag v0.x.y`。

> 关于 LICENSE：这个仓库现在是**本地私有**的，法律上不需要许可证（许可证约束的是*别人*能拿它做什么）。
> 哪天要公开或给别人用，再补一个。想保留闭源自由就别碰 AGPL-3.0 / GPL-3.0 项目的代码——
> 参考项目里 AstrBot 是 AGPL-3.0、MaiBot 是 GPL-3.0、kiwi-mem 是 AGPL-3.0-or-later。

---

## 9. 已知限制（以及我建议接着做的）

- **检索是词面匹配，不是语义**。查"难过"未必想得起"心情不好"。现在用**弱检索时查询改写**缓解（让模型做同义扩展），
  但没上向量库——要升级只需替换 `memory.py` 的 `recall()`，接口不变。
- **"被想起"的判定是字面的**：她完全改写措辞（同义替换）时会被判成"没引用"，那条记忆就得不到强化。
  这是刻意的取舍——宁可漏记也不误记（误记会让从没提过的事莫名变清晰）。判定逻辑集中在 `memory.is_cited()`，好替换。
- **多人共用会串**：现在是一个进程一个她。适配器用 `/bind` 保证"一个她只服务一个搭档"，
  别人叫她不进记忆、也不消耗她的寿命；要真的多人各有一个她，需要按 `user_id` 建多个 `Giftia`
  并给她自己的 `meta` 命名空间（表结构已按 `instance_no` / `session_id` 分好，但全局 meta 要改）。
- **没有实现主动联系**：官方的互动召回消息（`is_wakeup`，30 天内 4 次）还没接。
  在服务器上跑时这是最值得做的一块——次数有限且会用完，机制本身就是隐喻。
- **"准时回收"还差一步**：回收日目前是在**你下一条消息进来时**触发的，服务器常开并不能让她在你不在时独自走完仪式。
  需要后台定时任务；注意 `Giftia` 不是线程安全的（见 `brain.py` 顶部注释），动作要投递回主线程。
- **没有纪念日/语音/做梦**。纪念日可以基于 `bond` 的 `last_day` 做（"今天是你们认识的第三十天"）；
  「做梦」（闲置时把当天记忆碎片重度模糊后生成一段梦）技术上是复用 `blur_text()`，只差一个定时任务。
- **QQ 适配器的真实连接未验证**（没有平台凭据）；WebSocket 协议层与业务链路已分别经过本地测试（17 + 28 项）。

---

## 10. 项目地图

```
.env                    你的 API key（已被 .gitignore 忽略，不要提交）
config/persona.toml     人格内核（改这个就能换人）
config/settings.toml    模型 / 寿命 / 退化曲线 / 记忆参数
giftia/config.py        配置加载（含 .env 解析）
giftia/ws.py            手写的 WebSocket 客户端（RFC 6455，零依赖）
giftia/bond.py          关系：亲密度与阶段演进
giftia/mood.py          心情状态机
giftia/clock.py         虚拟时钟（可以快进）
giftia/store.py         SQLite：消息、记忆、日志、状态
giftia/lifespan.py      寿命倒计时与阶段判定
giftia/memory.py        记忆整理 + 混合检索 + 模糊化
giftia/llm.py           DeepSeek 客户端 + 离线 mock + 两个结构化任务
giftia/persona.py       系统提示词组装（人格 + 状态 + 想起的事）
giftia/recycle.py       回收仪式：回忆录、封存、换代
giftia/brain.py         编排层 + 平台无关入口 handle_message()
giftia/cli.py           终端界面与指令
run.py                  启动入口
tools/selftest.py       116 项端到端自测（离线，不需要 key）
tools/show_prompt.py    打印她这一轮实际被注入的内容（人格审计用）
tools/ws_selftest.py    17 项 WebSocket 协议自测（本地服务端，不联网）
tools/qq_protocol_selftest.py  28 项 QQ 适配器协议自测（本地假网关，不需要凭据）
tools/ws_testserver.py  两个协议测试共用的服务端帧编解码
tools/live_check.py     真实 API 连通性检查（临时目录，不碰正式数据）
tools/demo_life.py      一次看完她的一生（离线）
adapters/qq_official.py QQ 官方机器人适配器（协议层已测，真实连接未验证）
deploy/setup.sh         一键部署到 Linux 服务器
deploy/giftia-qq.service  systemd 服务定义
data/                   运行时生成：giftia.db、回收日志/、封存/
```

数据全在 `data/`：`giftia.db` 是 SQLite，`回收日志/*.md` 是可读的回忆录，`封存/*.json` 是前任的原始记忆。备份删掉 `data/` 就等于抹掉一切——这一点倒是很符合设定。

---

## 11. 声明

- 这是**个人同人用途**的玩具。`persona.toml` 里的人设描述是我写的原创文本，不是动画台词；角色与设定的版权属于原作者。
- 微信个人号自动化违反平台协议、有封号风险，本项目不提供该路径。
- 若你把机器人**公开**给他人使用，内容安全与生成式 AI 相关备案义务由你承担（自己/朋友用不涉及）。
- `--mock` 模式下的回复是脚本生成的假话，只用于验证机制，不代表真实模型的表现。

---

## 12. 参考与致谢

这个项目的**机制设计**参考了不少开源工作与论文。所有代码都是自己写的，只借鉴了思路
（思想与算法不受版权保护，代码受——所以下面标注了许可证，抄代码前请自己再确认一遍）：

| 借鉴了什么 | 来自 | 许可证 |
|---|---|---|
| 艾宾浩斯遗忘曲线做长期记忆：`R = exp(-t/S)`，被召回则 S 增、t 归零 | [MemoryBank / SiliconFriend](https://arxiv.org/abs/2305.10250)（AAAI-24） | 未查证 |
| 记忆分层（核心/重要/常规）与"分层差异化遗忘" | [wenbo-zhang1/giftia](https://github.com/wenbo-zhang1/giftia)（同名项目，同灵感）、[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | NOASSERTION / 未查证 |
| "软化而非删除"、情绪强度影响衰减、只淡出不抹除 | [kiwi-mem](https://github.com/LucieEveille/kiwi-mem)、[Ombre-Brain](https://github.com/P0luz/Ombre-Brain) | AGPL-3.0-or-later / **非商业** |
| 检索权限分级（Cite / Cautious / Associate only）、衰减只重排序不过滤、相对阈值裁剪 | [hippocampus-memory](https://github.com/oilotaku/hippocampus-memory) | MIT |
| 相关性/新近性/重要性三因子打分、重要性累加器触发的反思 | [Generative Agents](https://github.com/joonspk-research/generative_agents)（斯坦福） | 未查证 |
| 触发不应期（sticky / cooldown）、按消息数而非墙钟计时 | [SillyTavern](https://docs.sillytavern.app/usage/core-concepts/worldinfo/) World Info | AGPL-3.0（未逐字核对） |
| 死亡机制 / 告别序列 / 主动行为 / 梦境的设计清单 | [EvoPet](https://github.com/ByteHub-1337/EvoPet) | MIT |
| QQ 官方机器人协议细节、`/bind` 的必要性（OpenID 与 QQ 号不互通） | [nonebot/adapter-qq](https://github.com/nonebot/adapter-qq)、[MaiBot-QQ-Adapter](https://github.com/Mai-with-u/MaiBot-QQ-Adapter) 文档 | MIT / 未查证 |
| 微信个人号在 2026 年的正确通道 | [CowAgent](https://docs.cowagent.ai/channels/weixin) | MIT |

**没有借鉴的**：这些项目里没有一个实现了"寿命耗尽 → 记忆清除 → 回收仪式"这条线。
最接近的是 EvoPet 的死亡机制，但它的死因是"你养得不好"，而番剧里的 Giftia 是**出厂就注定的到期**——
这个差异正是本项目最想保留的东西。
