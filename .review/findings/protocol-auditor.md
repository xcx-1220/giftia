# Stream C 审查报告：I/O 与协议层（qq_official / ws / config / deploy）

审查对象：`adapters/qq_official.py`(1106 行)、`giftia/ws.py`(229 行)、`giftia/config.py`(455 行)、
`giftia/clock.py`(66 行)、`deploy/setup.sh`、`deploy/giftia-qq.service`
方法：静态通读 + 4 个自写探针（本地假服务端/假 OpenAPI/假 LLM 探针），全部离线、无凭据、无真实平台请求。
未改动任何产品代码。

## 探针清单（复现命令）

```powershell
$env:PYTHONIOENCODING="utf-8"
python .review\run_tests.py .review\probes\pa_ws_probe.py       # ws.py 畸形帧 12 项检查
python .review\run_tests.py .review\probes\pa_qq_probe.py       # QQ 适配器协议/限流 11 项检查
python .review\run_tests.py .review\probes\pa_config_probe.py   # .env/toml/路径/落盘 23 项检查
python .review\run_tests.py .review\probes\pa_privacy_probe.py  # 隐私红队 5 项检查
```

探针里的 `[FAIL]` = 找出来的问题（探针写成"打脸式"断言），`[PASS]` = 验证过没问题的部分。
计数：ws 9 通过 / 4 失败；QQ 0 通过 / 11 失败；config 14 通过 / 9 失败；privacy 2 通过 / 3 失败。

---

## P0

### [P0] `/unbind` 之后，私聊里任何陌生人都被当成搭档，模型提示词里直接带出前任搭档的私事

- 位置：`adapters/qq_official.py:642-659`（`_respond` 的认主与 `is_handler` 判定）、
  `adapters/qq_official.py:747-765`（`/bind`、`/unbind`）；
  配合 `giftia/brain.py:531-534`（`is_handler=True` 时 `memory_view = [("private",""), ("person",uid), ("group","*")]`
  —— **private 作用域不带 openid**，谁当上搭档就看到同一份）
- 严重度：P0 阻断（隐私越界：别人的私聊记忆泄漏到别人面前）；置信度：高（探针实测，三条独立链路）
- 现象与影响：
  `owner` 为空时 `is_handler = (kind == "c2c")`，于是"私聊里谁说话谁就是搭档"。
  `/unbind` 只清 `handler_id`（`qq_official.py:763`），**不封存也不隔离已有的 private 记忆**。
  结果：搭档发过 `/unbind` 之后，下一个私聊她的人（陌生人、群里加进来的任何人）会拿到
  `("private","")` 这一整份记忆的召回权——保险箱密码、约定、情绪全在系统提示词里；
  而且这一轮产生的新记忆也写进同一份 `("private","")` 空间（陌生人可以反过来污染搭档的记忆）。
  三条链路都跑通了：
  - A（**出厂默认 `auto_bind = true`**）：`/unbind` 后陌生人私聊 → 被"自动认主"→ 提示词里出现秘密；
  - B（`auto_bind = false`，正式配置项）：`/unbind` 后陌生人私聊 → 直接被当成搭档（连认主都不需要）；
  - E：`/unbind` 后群里**任意成员**（`member_role = "member"`）发一条 `/bind` 就能认主，再私聊问出秘密。
- 触发条件（最小复现，探针已做）：
  搭档 `/bind` → 说一句话 → 产生一条私聊记忆 → 搭档 `/unbind` → 陌生人私聊一句
- 证据（`pa_privacy_probe.py`，链路 A，出厂默认配置）：

  ```
  A. 默认配置（auto_bind = true，出厂值）
  [自动认主·私聊] 搭档 = INTRUDER（想换人：/unbind）
    [INFO] 陌生人私聊后 memory_view = [('private', ''), ('person', 'INTRUDER'), ('group', '*')]，模型调用次数 1
    [INFO] 泄漏到提示词：…【你对他们的印象】…- 小林：他保险箱的密码是 SECRET-ALPHA-7731，别跟别人说…
    [FAIL] 默认配置下，/unbind 之后陌生人私聊拿不到前任搭档的私事  -> 提示词里含密钥字样：True
  ```

  链路 B（`auto_bind=false`，`handler_id=''`，同样命中）：

  ```
  B. auto_bind = false（正式配置项）
    [INFO] strict: auto_bind=False，/unbind=True → handler_id=''
    [INFO] 陌生人私聊后 memory_view = [('private', ''), ('person', 'INTRUDER'), ('group', '*')]，模型调用次数 1
    [FAIL] auto_bind=false 时陌生人私聊拿不到前任搭档的私事  -> 提示词里含密钥字样：True
  ```

  链路 E（抢注）：

  ```
  E. 抢注：搭档 /unbind 之后，群里任何成员一条 /bind 就能认主
  [收到·群] ATTACKER…：/bind
  [绑定] 搭档 = ATTACKER
    [INFO] 群里 member 抢注之后 handler_id = 'ATTACKER'，她的回复 = '……嗯。我记住了。\n以后我只认你一个人说话。'
    [FAIL] 抢注者也拿不到前任搭档的私事  -> 提示词里含密钥字样：True
  ```

  两个对照组通过（说明不是探针误报）：搭档还在绑时陌生人私聊被挡回且**不调模型**；
  群里客人身份 `memory_view = [('group','GROUP-1')]`，看不到私事。

- 建议修法（最小改动，二选一或都做）：
  1. `qq_official.py:659`：没有绑定人时不要把私聊当成搭档——
     `is_handler = bool(owner) and user_id == owner`。
     正常流程不受影响（`auto_bind` 的分支已经在 642-653 行先设好了 owner），
     只有"没有搭档"的状态会变成走客人/挡回路径。
  2. `/unbind`（763 行）改为同时把当前实例的 private 记忆归档/封存（或把 `handler_id` 记成
     "已解绑"哨兵，强制重新 `/bind` 才能恢复搭档身份），别让"解绑"只清一个字符串。

---

## P1

### [P1] 帧读到一半超时被判成 `WSTimeout`（"连接仍可用"），实际字节流已经错位：两条独立消息被拼成一条

- 位置：`giftia/ws.py:117-138`（`_read` 的 `WSTimeout` 分支）、`giftia/ws.py:140-156`
  （`_read_frame` 只有第一个 `_read(2, timeout)` 带调用方超时，第 147/149/152/153 行的
  `_read(2)/_read(8)/_read(4)/_read(length)` 全部退回 `self.timeout`）、
  `giftia/ws.py:183-209`（`recv_text` 的 `chunks` 是局部变量，异常时整段丢弃）
- 严重度：P1 高（消息内容损坏：上下文里两条无关消息被拼成一条）；置信度：高（探针实测）
- 现象与影响：
  - 调用方给的 `timeout=0.3` 只在读第 1、2 个字节时生效；只要帧头到了、后续字节卡住，
    就按 `self.timeout`（适配器里是 15 秒）等，**语义与文档相反**；
  - 卡住时 `_read` 里 `out` 是空的 → 抛 `WSTimeout("等待数据超时")`，而文档说
    "超时抛 WSTimeout（连接仍可用），常用于心跳间隙"。此刻帧头已经被吃掉 2 字节，
    剩下的字节会被当成**下一帧的帧头**解析；
  - 适配器 `session_loop` 里正是 `except WSTimeout: continue`（`qq_official.py:944-945`），
    于是它带着一条错位的连接继续收消息 → 消息被静默拼接/截断，而不是重连。
  - 触发门槛是"帧内两次 TCP 段之间卡 > `self.timeout`"（适配器 15 秒），
    在弱网/网关卡顿时会发生，代价是会话语义被污染而不是干净地重连。
- 触发条件：假服务端先发 2 字节帧头，卡 2.5 秒（`self.timeout=2.0`），再补齐这一帧，
  紧接着发一条**完整独立**的 TEXT 帧。
- 证据（`pa_ws_probe.py` §4，原样输出）：

  ```
  4. 半包超时：连接到底还可不可用
    [INFO] recv_text(timeout=0.3) 实际耗时 2.00s，抛出 WSTimeout(等待数据超时)
    [FAIL] 帧内超时按调用方给的 timeout 生效（不是 self.timeout）  -> 2.00s
    [FAIL] 帧读到一半超时时不抛 WSTimeout（连接已不可信）  -> WSTimeout(等待数据超时)
    [INFO] 恢复后第一次 recv_text → 返回 'REALDATA!AFTER'
    [FAIL] 超时后收到的两条独立消息不会被拼接错位  -> 返回 'REALDATA!AFTER'
  ```

  （服务端先发的是一条内容为 `REALDATA!` 的消息，`AFTER` 是之后另一条独立消息；
  客户端把它们当成"分片 + 续帧"合并成了一条。）
- 建议修法：在 `_read` 里区分"还没开始读这一帧"与"读到一半"两种状态——把 `_read_frame` 拆成
  "先读 2 字节头（可超时，失败 = WSTimeout，连接仍可用）→ 之后所有字节用带 deadline 的读，
  任何超时/EOF 一律抛 `WSClosed`（连接不可信）"，并把调用方 timeout 作为整帧的 deadline 传递；
  或者最保守的做法：`_read` 在 `out` 非空时一律抛 `WSClosed`（现有第 132 行已经是这么做的），
  只需让第 147/149/152/153 行的读也走同一个"帧内"语义，不要回退到 `self.timeout`。

### [P1] 8 秒同文去重会丢掉合法的第二条回复，而且把它当成"已送达"上报

- 位置：`adapters/qq_official.py:342-347`（`send` 里的同文去重），记账在 373 行
- 严重度：P1 高（消息丢失 + 记忆与实际不一致；`SendResult` 是"发送是否送达"的唯一事实来源）；
  置信度：高（探针实测）
- 现象与影响：
  `pieces == [last_text] and time.time()-last_ts < 8` 就 `result.sent.extend(pieces); return`。
  两个**不同的** `msg_id`（= 平台侧两条独立的被动回复）只要文本相同且在 8 秒内，
  第二条不会发出，却进了 `SendResult.sent` → `SendResult.ok == True`、`delivered` 非空 →
  `hub.note_reply_delivery(delivered)` 认为"用户看到了" → 她把这句话记进自己的记忆，
  而用户屏幕上什么也没有。短回复（"嗯。""在的。"）重复概率很高；
  平台重推那种"真重复"其实已经由 `is_duplicate`（485-501 行）按 `msg_id` 挡住了，
  这一层去重只会误伤合法回复。
- 触发条件：同一会话 8 秒内两次模型产出同一句话（两个不同 msg_id）
- 证据（`pa_qq_probe.py` §2）：

  ```
  [发送] 8 秒内发过同样的话，跳过（防重复回复）
    [INFO] 第一次 send：1 个 HTTP 请求，delivered='嗯，我在。' ok=True
    [INFO] 第二次 send（msg_id=MSG-B）：1 个 HTTP 请求，delivered='嗯，我在。' ok=True failed=[]
    [FAIL] 两次合法回复都会真的发出去  -> 只发出 1 条
    [FAIL] 没发出去时 SendResult 不会谎报 delivered  -> delivered='嗯，我在。'
  ```

- 建议修法：把"跳过"降级成"没发也算没送"：命中同文去重时不要 `extend(sent)`，
  而是 `result.failed.append((piece, "8 秒内同文去重"))`（调用方自然会回滚记忆）；
  更好的做法是把去重键加上 `msg_id`：同一个 `msg_id` 重复才跳过，不同 `msg_id` 一律放行。

### [P1] HTTP 200 + 非 JSON 响应会抛 `JSONDecodeError` 穿透 `send()` 和 `handle_event`，拆掉整个 WS 会话

- 位置：`adapters/qq_official.py:256-278`（`_request`，273 行的 `json.loads(raw)` 在
  `try/except` 之外）、`adapters/qq_official.py:374-376`（`send` 只 `except QQError`）、
  `adapters/qq_official.py:970-976`（`session_loop` 只捕获 `WSClosed`）
- 严重度：P1 高（"发送失败如实上报 + 回滚记忆"的机制被绕过；一条坏响应拆掉整条连接）；
  置信度：高（探针实测）
- 现象与影响：
  `_request` 的调用方约定"失败一律 `QQError`"（374、864 行都按这个写）。
  代理页/网关页/CDN 错误页常常是 **HTTP 200 + HTML**，此时 `json.loads` 抛
  `json.JSONDecodeError`：`send()` 不接 → `result` 根本不存在 → `_respond` 里
  `hub.note_reply_delivery(...)`（712 行）不会执行（记忆不回滚）；
  异常继续穿过 `handle_event` → `session_loop` 只 `except WSClosed` → 冒到 `run()` → 断线重连。
  即：一个坏的 HTTP 响应体 = 一次会话拆掉 + 一轮记忆不一致。
- 触发条件：`api_base` 指向一个对 `/v2/...` 返回 `200 text/html` 的端点（探针用本地假 OpenAPI 模拟）
- 证据（`pa_qq_probe.py` §1）：

  ```
  1. 业务返回 HTTP 200 + 非 JSON（代理页/网关页）
    [INFO] send() 抛出：JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    [FAIL] _request 的契约是「失败一律 QQError」  -> JSONDecodeError: ...
    [INFO] 一条 c2c 事件走完 handle_event：JSONDecodeError: Expecting value: line 1 column 1 (char 0)
    [FAIL] 非 JSON 响应不会把异常抛穿事件处理（会拆掉整个 WS 会话）  -> JSONDecodeError: ...
  ```

  同一类问题：鉴权接口返回 JSON 数组时 `body.get("access_token")` 抛 `AttributeError`，
  同样不在 `QQError` 里（`pa_qq_probe.py` 同一节，第二次运行）：

  ```
    [INFO] 鉴权接口返回 JSON 数组时 access_token()：AttributeError: 'list' object has no attribute 'get'
    [FAIL] 鉴权接口返回非对象 JSON 时也是 QQError  -> AttributeError: 'list' object has no attribute 'get'
  ```
- 建议修法：`_request` 里把 `json.loads(raw)` 包进 `try/except ValueError` →
  `raise QQError(f"响应不是 JSON：{raw[:200]!r}")`；并校验 `isinstance(body, dict)`。
  这样 374/864 行的 `except QQError` 才真的覆盖全部失败形态。

### [P1] 同一个 `msg_id` 的回复条数不受平台上限约束（单聊 4 / 群 5），长回复必然被平台拒掉

- 位置：`adapters/qq_official.py:144-158`（`chunk_text`，`MAX_CHARS=350`）、
  `adapters/qq_official.py:349-363`（每段一次 HTTP、`msg_seq` 递增）、
  `adapters/qq_official.py:714-719`（系统通知复用同一个 `msg_id`）
- 严重度：P1 高（长回复后半段永远发不出去 = 消息丢失；且文件头 21-22 行、README:433-438
  自己写明了"群 5 次 / 单聊 4 次"的限制，代码里没有任何一处计数）；置信度：高（探针实测）
- 现象与影响：
  `max_tokens=900` 的回答切到 350 字一段可以有 5 段；再叠加
  `[终端服务]/[记忆整理]/[记忆强化]` 通知（716-719 行，同一个 msg_id、seq 继续加），
  单条事件就能发出 6+ 条回复。平台按 `msg_id` 计数，第 5（单聊第 5）/第 6 条会被拒 →
  用户看到半截，`SendResult.failed` 里出现业务错误（这次至少如实上报了）。
- 触发条件：一段约 1700 字的回复（`max_tokens=900` 的正常上限内）
- 证据（`pa_qq_probe.py` §3）：

  ```
    [INFO] 1690 字 → chunk_text 切成 5 段
    [INFO] 单聊：同一 msg_id 发出 5 条，msg_seq=[1, 2, 3, 4, 5]
    [INFO] 再加一条系统通知 → 同一 msg_id 共 6 条，msg_seq=[1, 2, 3, 4, 5, 6]
    [FAIL] 单聊里同一个 msg_id 的回复条数不超过 4（平台硬限）  -> 6 条
  ```

- 建议修法：给 `send` 加一个"本次事件已发条数"的预算（单聊 4 / 群 5），
  超出时把剩余段落合并/截断并按平台限制决定是否放弃；通知优先占用额度或改走主动消息；
  最省事的版本：`chunk_text` 的段数上限按 `max(1, 限制-通知数)` 收口，超长时以"省略号+一句收尾"结束。

---

## P2

### [P2] 分片消息只受"单帧 `max_frame`"约束，整条消息的字节数没有上限

- 位置：`giftia/ws.py:150-151`（只比较单帧长度）、`giftia/ws.py:188`+`204-209`（`chunks` 无上限累加）
- 严重度：P2 中（无界内存增长，对端可控）；置信度：高（探针实测）
- 现象与影响：`max_frame` 形同"每帧"上限。对端只要连发续帧，消息可以无限长，
  全在内存里（`chunks` + `b"".join`），期间没有任何字节数检查；
  适配器用默认 `max_frame=8MB`，一个 1000 片的恶意/异常流就是数 GB。
- 证据（`pa_ws_probe.py` §3，故意把 `max_frame` 设成 4096）：

  ```
    [INFO] max_frame=4096 单帧上限；实际收到一条 400010 字节的消息
    [PASS] 100 个 4000 字节续帧（总 400KB）被完整重组
  ```

- 建议修法：加一个 `max_message`（默认比如 4×`max_frame`），在 `recv_text` 累加时检查，
  超了抛 `WSError` 并关连接。

### [P2] 对端主动 close 之后，本地 `close()` 是空操作：socket 不会真的关闭

- 位置：`giftia/ws.py:196-203`（收到 CLOSE 时先 `self.closed = True` 再抛异常）、
  `giftia/ws.py:211-222`（`close()` 第一行 `if ... or self.closed: return`）
- 严重度：P2 中（资源未及时释放；`session_loop` 的 `finally: ws.close()` 恰好在最需要它的路径上失效）；
  置信度：高（探针实测）
- 现象与影响：平台用 close 帧断开（QQ 的 4009 就是这条路径）时，`recv_text` 抛 `WSClosed`
  之前就把 `closed` 置 True；`session_loop` 的 `finally` 调 `ws.close()` → 直接 return →
  TCP 连接保持打开，服务端也观察不到 FIN。只能靠 CPython 引用计数回收 `socket.__del__`
  兜底（调用方只要多持有一个引用/形成循环引用，就真的漏 fd）。
- 证据（`pa_ws_probe.py` §5）：

  ```
    [INFO] ws.closed = True
    [INFO] close() 之后 socket.fileno() = 424（-1 才是真的关了）
    [INFO] 服务端看到客户端 TCP 关闭（EOF）= None
    [FAIL] 对端 close 之后调用 close() 会真的关闭 socket  -> fileno=424 eof=None
  ```

- 建议修法：`close()` 不要用 `self.closed` 当"已经关过"的标志——用一个单独的 `self._sock_closed`；
  收到对端 CLOSE 时只标记协议层已关闭，`close()` 仍然执行 `sock.close()`。

### [P2] 控制帧规则完全不校验：FIN=0 的 ping、payload 200 字节的 ping 都会被接受并回显

- 位置：`giftia/ws.py:191-193`（`if opcode == OP_PING: self._send_frame(OP_PONG, payload)`）
- 严重度：P2 中（RFC6455 §5.5 要求协议失败并关闭连接；这里静默容忍，且把违规 payload 原样回显）；
  置信度：高（探针实测）
- 证据（`pa_ws_probe.py` §2）：

  ```
    [INFO] 客户端把 200 字节的 FIN=0 ping 回显成：[(10, 200)]
    [PASS] 违规控制帧（FIN=0 / 200 字节）被接受并回显 pong
  ```

- 建议修法：收到 `OP_PING/OP_PONG/OP_CLOSE` 时检查 `fin` 必须为 1、长度 ≤ 125，
  否则抛 `WSError` 并关闭连接。

### [P2] 主动消息（`_maybe_outreach`）绕过了 `send()` 的分钟/小时限频

- 位置：`adapters/qq_official.py:862-867`（直接 `self._request("POST", path, body)`）
- 严重度：P2 中（"防平台风控"的三道闸只保护被动回复；主动消息是更容易触发风控的那一类）；
  置信度：高（探针实测）
- 证据（`pa_qq_probe.py` §5，先把限频表塞到 65 条 > 每小时上限 60）：

  ```
    [INFO] 发送限频表：每小时上限 60，已塞 65 条
    [主动·私聊·测试] 主动来找你了
    [INFO] 主动消息实际 HTTP 请求数=1，body=[{...'path': '/v2/users/OWNER/messages'...}]
    [FAIL] 主动消息也受 send_max_per_minute/hour 约束  -> 绕过了限频，仍发出 1 条
  ```

- 建议修法：`_maybe_outreach` 发送前调一次 `_rate_limit_wait(key)`，并在成功后 `_note_send(key)`；
  注意 `tools/audit.py:701` 把它列成了"故意直连"的已知例外，改的时候要一起更新那条说明。

### [P2] 限频命中是"硬丢"，与代码注释"超了就等（不硬丢，但要留痕）"相反

- 位置：`adapters/qq_official.py:326`（注释）、`adapters/qq_official.py:355-359`（实现）
- 严重度：P2 中（分段回复会缺一段，用户看到半截）；置信度：高（探针实测）
- 证据（`pa_qq_probe.py` §4）：

  ```
    [INFO] 限频表已满 → ok=False failed=[('这句能不能发出去', '每分钟最多 6 条')] 实际 HTTP 请求数=0
    [FAIL] 限频命中时不丢消息（或至少如实上报为失败）  -> 被硬丢：ok=False
  ```

  （如实上报这一点是好的——`_respond` 会回滚记忆；坏的是"丢"，且和注释矛盾。）
- 建议修法：要么按注释改成"等到窗口过了再发"（带一个上限，别无限等），
  要么把注释改成"超了就放弃这一段"并让调用方把丢弃写进 `notices`。

### [P2] `extract_content` 的"拍一拍"识别会把正常文本吃掉（"Pokemon" → "[拍了拍]"）

- 位置：`adapters/qq_official.py:183-188`（`blob` 变量算出来没用，真正判定是
  `any(w in str(el).lower() for w in ("poke","拍了拍"))`）
- 严重度：P2 中（引用/转发消息的正文被整段替换成占位符，且模型看不到原文）；
  置信度：高（探针实测，纯函数）
- 证据（`pa_qq_probe.py` §6）：

  ```
    [INFO] 元素文本含 'Pokemon'  → '[拍了拍]'
    [INFO] 元素文本含 'poker'    → '[拍了拍]'
    [INFO] 普通引用文本          → '被引用的原话'
    [FAIL] poke 识别不会吃掉正常文本  -> [拍了拍]
  ```

- 建议修法：只看结构化字段（`el.get("content_type")`/`ark_type`/`type` 等，也就是已经算好的
  `blob`），不要对 `str(el)` 全文做子串匹配；判不出来就当普通文本处理。

### [P2] `.env` 解析与常见写法不兼容：`export` 前缀 / BOM 静默失效，行内 `#` 注释变成值的一部分

- 位置：`giftia/config.py:349-371`（`load_env_file`，365 行的 `value.strip().strip('"').strip("'")`，
  361 行的注释判断只认整行 `#`）
- 严重度：P2 中（凭据静默失效，报错信息还指错方向："没有找到 API key"）；
  置信度：高（探针逐条构造真实文件实测）
- 现象与影响：
  - `export KEY=value`（很常见的写法）→ 变量名被写成 `EXPORT KEY`（Windows 上还会大写），
    `KEY` 根本没设；同理解析出的脏变量名带 `\ufeff` 前缀（UTF-8 BOM 文件的第一行）；
  - `KEY=value # 注释` → 值变成 `'value # 这是注释'`；`KEY="v" # 注释` → `'v" # 注释'`
    （`.strip('"')` 只剥两端，引号在中间就留下了）。
  - 影响面：`DEEPSEEK_API_KEY` / `QQ_BOT_SECRET` 静默为空 → 适配器打印"缺少凭据"，
    用户明明写在 `.env` 里。
- 证据（`pa_config_probe.py` §1）：

  ```
    [FAIL] 行内 # 注释：PA_INLINE == 'value'  -> 实际 'value # 这是注释'
    [FAIL] 引号后带注释：PA_QC == 'value'  -> 实际 'value" # 注释'
    [FAIL] export 前缀：PA_EXPORT == 'value'  -> 实际 None；脏变量 ['EXPORT PA_EXPORT']
    [FAIL] BOM 开头的第一行：PA_BOM == 'value'  -> 实际 None；脏变量 ['\ufeffPA_BOM']
  ```

  （`CRLF`、两侧空格、值里带 `=`、单双引号、整行注释都正确，这几项 PASS。）
- 建议修法：读文件用 `encoding="utf-8-sig"`；去掉行首 `export `；
  值解析时若以引号开头，只剥离匹配的成对引号，并在引号闭合后截断（不闭合时按原样或用 `shlex`）。

### [P2] `settings.toml` 只做"取字段"，不做存在性/类型/范围校验：缺字段直接 `TypeError` 崩，越界值照单全收

- 位置：`giftia/config.py:381-384`（`_sub`）、`408-418`（stages）、
  `LifespanConfig.effective_hours`（54-60）；触发点 `giftia/lifespan.py:123-128`
- 严重度：P2 中（配置错误 = 启动即崩且错误信息不可读；或静默行为异常）；置信度：高（探针实测）
- 现象与影响（四个独立问题，探针一次跑出）：
  1. `[[degradation.stages]]` 缺字段 → `TypeError: Stage.__init__() missing 5 required positional
     arguments`，用户看不出是哪个文件哪一行，也不知道缺什么；
  2. `total_hours = 0` → `effective_hours()` 返回 0 → 寿命一出生就到期
     （`expired=True, ratio=0.0`），她会立刻走"终端服务/回收"路径；
  3. `response_tier = -5`、`speak_max_per_day = -1` 被原样接受（档位判定、日上限都会变得不可预期）；
  4. 拼错的键（`response_tierr`、`wake_delay_second`）被 `_sub` 静默忽略 → 用户以为改了配置，
     实际没生效；`keywords = "晚安"`（字符串）也不会报错，适配器 `tuple(kw)` →
     `('晚','安')`（`qq_official.py:587-588`），变成**逐字匹配**："今天晚饭吃什么"也会触发她。
- 证据（`pa_config_probe.py` §2）：

  ```
    [INFO] 缺字段的 stage 抛出：TypeError: Stage.__init__() missing 5 required positional arguments: ...
    [INFO] response_tier = -5 被接受，settings 里就是 -5；speak_max_per_day = -1
    [INFO] total_hours=0 → effective_hours=0.0，出生第一秒 status：expired=True ratio=0.0 stage=clear
    [INFO] 拼错的键被静默忽略：response_tier=0，wake_delay_seconds=2.0
    [INFO] keywords = "晚安" → settings 里是 '晚安'（str），适配器 tuple(它) = ('晚', '安')
  ```

  （乱序的 `stages` 会被按 `min_ratio` 降序重排，这一项 PASS。）
- 建议修法：`_sub` 改成先校验必填字段缺失（收集成一条人话错误：文件、段落、字段名）；
  对 `total_hours`/`response_tier`/`speak_max_per_day`/`keywords` 加类型与范围断言并给默认值；
  可选：加载完后对比 `dataclass_fields`，把未知键打成 warning（`_sub` 已经拿到了这个信息）。

### [P2] 收到 SIGTERM 时，还在等窗口里的批次消息会被直接丢掉

- 位置：`adapters/qq_official.py:450-457`（`_flush_batches`）、`:932`（**唯一**调用点，在 WS 循环里）、
  `:975-976` 与 `:998-1000`（退出路径只 `ws.close()` + `shutdown_default()`）
- 严重度：P2 中（systemd 停止/重启时命中 2 秒窗口的那条消息永久消失：不回复、不落聊天记录）；
  置信度：中（**静态分析**：调用点只有一处，退出路径没有 flush；未跑集成探针）
- 现象与影响：`wake_delay_seconds` 默认 2 秒；这 2 秒内 `_stopping` 被置位的话，
  `while not self._stopping` 直接退出，批次里的消息既没被 `log_only` 落库、也没被回应。
  `systemctl restart`（改 persona.toml 的常规操作）正好会走这条路。
- 触发条件：群里 @ 她之后 2 秒内收到 SIGTERM
- 证据：调用点唯一（`grep -n "_flush_batches" adapters/qq_official.py` → 450 定义、932 调用），
  退出路径 `session_loop` 的 `finally`（975-976）与 `run()` 的 `finally`（998-1000）都没有它。
- 建议修法：`run()` 的 `finally` 里、`shutdown_default()` **之前**调一次 `self._flush_batches()`
  （或至少把 pending 的 items 用 `log_only=True` 落库），让"停止服务"不丢消息。

---

## P3

### [P3] 握手只校验 `Sec-WebSocket-Accept` 与首行子串 `"101"`，不校验 `Upgrade`/`Connection`

- 位置：`giftia/ws.py:98-114`
- 严重度：P3 低；置信度：高（探针实测）
- 说明：`accept` 的比对是真的做了（含正确的 key+GUID），这一点 README:419-420 的宣称成立；
  但状态行判断是 `"101" not in lines[0]` 这种子串匹配，且响应缺少 `Upgrade: websocket` /
  `Connection: Upgrade` 时照收。证据（`pa_ws_probe.py` §1）：
  `[PASS] 缺 Upgrade/Connection 响应头时仍接受（只查 accept）`、`[PASS] HTTP 200（即使 accept 正确）被拒绝`。
- 建议：状态行按 `HTTP/1.1 101` 前缀严格判断；补 `upgrade: websocket` 检查（几行）。

### [P3] `Host` 头不带端口

- 位置：`giftia/ws.py:79-87`（`Host: {self.host}`）
- 严重度：P3 低；置信度：高（探针实测）
- 说明：`ws://127.0.0.1:14608/` 实际发出的是 `Host: 127.0.0.1`。QQ 网关是 443 所以无感，
  但 `QQ_API_BASE`/自建反代/网关返回非默认端口时会踩到严格的虚拟主机校验。
- 建议：`Host: {host}:{port}`，仅当 `port` 不是该 scheme 的默认端口时带端口。

### [P3] 非法 UTF-8 静默替换，没有告警或错误

- 位置：`giftia/ws.py:209`（`decode("utf-8", "replace")`）
- 严重度：P3 低；置信度：高（探针实测）
- 证据：`pa_ws_probe.py` §6 → `[INFO] 非法 UTF-8 载荷 → '���abc'`。
  对 QQ 的 JSON 文本帧而言，坏字节会在 `json.loads` 才炸（且是 P1 里那条穿透路径）。
- 建议：`errors="replace"` 至少 `print` 一条告警（或改成"抛 WSError"由调用方决定）。

### [P3] deploy：测试项数三处不一致（187 / 218 / 270），`Restart=always` 没有 StartLimit，且自检与实际运行可能用不同解释器

- 位置：`deploy/setup.sh:76`、`README.md:23`、`README.md:649`、`deploy/giftia-qq.service:15,18-19`
- 严重度：P3 低（运维摩擦，不会损坏数据）；置信度：中（**静态分析**，本机没有 Linux/systemd 可实测）
- 事实：
  1. 同一套自测，`setup.sh:76` 说"187 项"、`README.md:23` 说"218 项"、`README.md:649` 说"270 项"，
     三个数字互斥（实际项数应由 Stream E 的复跑来定，我这里只报不一致）。
  2. `Restart=always` + `RestartSec=5`，没有 `StartLimitIntervalSec/StartLimitBurst`：
     `.env` 里凭据非空但**无效**（比如 AppSecret 过期）时会走 `main()` 的早退/`run()` 的退避，
     5 秒一轮无限重启；setup.sh 的检查只判断 `QQ_BOT_APPID/SECRET` 非空（70-72 行），
     拦不住"填了但错"。
  3. `setup.sh:77` 用 PATH 里的 `python3` 跑自检（可能是用户自己装的 3.12），
     而 `giftia-qq.service:15` 写死 `/usr/bin/python3`（Ubuntu 22.04 上是 3.10，没有 tomllib）
     → 自检通过、服务起不来。脚本的报错文案里提过要改 `ExecStart`（26-30 行），
     但第 4 步和第 6 步没有对齐校验。
- 建议：把测试项数改成从一个来源生成（或写"约 N 项"）；`Restart=on-failure` +
  `StartLimitIntervalSec=60` + `StartLimitBurst=3`；第 4 步用 `$(command -v python3)` 的绝对路径，
  并与 `ExecStart` 的解释器比对。

### [P3] 文档与代码不一致：`is_wakeup` 已实现，但 README 与模块文档说"尚未实现"

- 位置：`adapters/qq_official.py:27`（"本文件暂未实现，见 README「下一步」"）、
  `adapters/qq_official.py:855-856`（实际实现了 `body["is_wakeup"] = True`）、
  `README.md:442`、`README.md:749`（"还没接"）对比 `README.md:182` 与 `config/settings.toml:198-200`
  （文档化了 `use_wakeup` 开关）
- 严重度：P3 低；置信度：高（代码/文档互证）
- 说明：功能是有的（`[outreach] use_wakeup = true` + 单聊时加字段），但 README 的"未实现/下一步"
  会让用户以为打开也没用。建议统一到"已实现、真机未验证"。

---

## 未验证的怀疑（没有跑出证据，只给线索）

1. **长回复/长时间模型调用期间发不出心跳**：`session_loop` 是单线程（`qq_official.py:923-945`），
   心跳只在两次消息之间发。README:426 说真机心跳周期是 41 秒，而 `request_timeout=60` +
   工具轮（最多 3 轮）可能超过一个周期 → 平台按"没心跳"断连（4009/resume 兜）。结构上成立，
   没跑集成探针验证。
2. **`_seen` 去重表的清理顺序**：`is_duplicate`（488-501 行）刷新已有 key 的**值**但不改变
   `OrderedDict` 的插入顺序，而清理只看"最前面那个"。被反复重推的 msg_id 会卡在队首，
   让后面的过期条目清不掉（内存与去重窗口都不准）。影响量级小，未实测。
3. **`_seqs` 淘汰导致 `msg_seq` 回退**：`_next_seq`（312-317 行）超过 500 个 msg_id 就丢最早的；
   5 分钟被动窗口内如果同一个 msg_id 又被用到，seq 会从 1 重新开始 → 被平台判重。
   需要 500 条不同 msg_id 才会发生，未实测。
4. **`gateway_url()` 不校验返回的 URL**（301-309 行）：`/gateway` 返回 `http://` 或空都会走到
   `WebSocket` 的 `WSError`，兜底逻辑只在 `QQError` 时生效；真机上没遇到，未实测。
5. **`chunk_text` 的 350 是字符不是字节**（144-158 行）：平台限制是字节还是字符没有官方依据可查，
   emoji/中文混排时实际长度约 3 倍，未验证。
6. **群里"接收所有消息"开/关事件的名字**（`GROUP_MSG_RECEIVE`/`GROUP_MSG_REJECT`）与
   撤回事件名都是作者按文档+猜测写的（代码注释自己也标了"没找到正式名字"）；
   真机是否推这两个事件、字段名是否是 `group_openid`，未验证。
7. **群 5 分钟被动窗口没有被显式计时**：代码完全依赖 `msg_id` 交给平台裁决；
   如果 `_run_batch` 因网络卡顿延后超过 5 分钟才发送，平台会拒（表现为业务错误码），
   代码无法提前发现。未在真机验证窗口边界。

## 顺带验证过的正向结论（免得只报坏消息）

- `Sec-WebSocket-Accept` 是**真的在比对**（key+GUID 的 SHA1+base64），不是只看 101（探针 §1 PASS）。
- 16/64 位长度编码、最高位为 1 的畸形 64 位长度（会被 `max_frame` 拦下）、
  UTF-8 字符跨分片 + 跨 TCP 包都正确（探针 §3/§6 PASS）；客户端发出的帧全都带掩码（沿用项目自测）。
- `_buf` 的手工偏移计算是对的：握手响应捎带的第一帧、以及多个帧同处一个 TCP 段都能正确切分
  （`ws_selftest` §1 与我的探针共同覆盖）。
- `Authorization: QQBot {token}`、`GET /gateway`、op10/op2/op1/op11 序列、`intents=1<<25`、
  `shard=[0,1]`、HTTP 200 + `body.code != 0` 判失败，这些都对（项目自测 69 项覆盖，我复核了代码路径）。
- 事件去重按 `msg_id` 且区分"旁听过/回过"（485-501 行），比"一律当重复"更准；
  自己插嘴的日上限（402-414 行）按群、按天记账，只在**决定要说**时扣额度，符合注释。
- `config.py` 的路径全部基于 `__file__` 解析，跟 cwd 无关（探针 §3：cwd 切到 `C:\` 后
  `db_path` 仍是 `D:\ai chatbot\data\giftia.db`）。
- 跑一轮真实（mock 模型）对话后扫描 `data/` 下全部文件：**没有任何 .env 凭据落盘**；
  `runs.jsonl` 只有长度/条数类数字，不含对话内容与密钥（探针 §4：
  `{"ts": ..., "in_chars": 17, "out_chars": 30, "prompt_chars": 2240, "recalled": 0, "said": 0}`）。
- `deploy/giftia-qq.service` 没有用 `Environment=`/`EnvironmentFile=` 承载密钥
  （`.env` 由应用自己读，权限 600），所以 `systemctl show` 不会暴露凭据；
  `ProtectSystem=strict` + 只读加固 + `ReadWritePaths=/opt/giftia/data` 与代码实际写盘位置一致。
