# 审查协议（所有 reviewer 必读）

项目：`D:\ai chatbot`（Giftia，纯标准库 Python + SQLite 的陪伴型对话内核）
Python：3.14.3（`python` 命令）。零第三方依赖。
**不要修改 `giftia/`、`adapters/`、`tools/`、`config/`、`run.py` 里的任何文件**——本次只审查，
不改产品代码。发现的每个问题都要有证据。

## 沙箱约束（很重要，第一次跑测试就会踩）

- 本会话文件策略为 workspace-write：只有 `D:\ai chatbot` 下可写。
  **系统临时目录不可写**（`PermissionError: [WinError 5]`）。
- **更坑的一点（已实测）**：连工作区内的目录，只要是**以 mode 0o700 新建**的，也会被沙箱拒绝访问
  （`os.mkdir(p, 0o700)` / `tempfile.mkdtemp()` 都是 0o700 → `WinError 5`）。
  实测：`os.mkdir(p, 0o700)` FAIL、`os.mkdir(p, 0o777)` PASS、`Path.mkdir()`（默认 0o777）PASS。
- **跑项目自带脚本的正确姿势**（不改产品代码）：用现成的启动垫片

  ```powershell
  $env:PYTHONIOENCODING="utf-8"
  python .review\run_tests.py tools/selftest.py
  python .review\run_tests.py tools/ws_selftest.py
  python .review\run_tests.py tools/qq_protocol_selftest.py
  ```

  它把 `tempfile.tempdir` 指到 `.review\tmp\runtime` 并把 `mkdtemp` 换成 0o777 版本。
  你自己的探针脚本里如需临时目录，请 `os.makedirs(path, exist_ok=True)`（默认 0o777 可访问），
  **不要**用 `tempfile.mkdtemp()` 的默认行为；或直接 `import tempfile; tempfile.mkdtemp = ...`。
- 任何需要临时数据的脚本，也应把临时根指到工作区内，例如：

  ```powershell
  $tmp = "D:\ai chatbot\.review\tmp\<你的名字>-<随机>"
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $env:TEMP = $tmp; $env:TMP = $tmp
  python tools/selftest.py
  ```

  纯 Python 探针用 `data_dir=r"D:\ai chatbot\.review\tmp\..."` 之类显式路径更稳。
- 绝不要碰 `data/giftia.db`（那是作者的正式数据，可能是唯一一份）。用临时库。
- 不要把 `.env` 里的 key 打印出来或写进报告。API key 存在于 `.env`，**不要**为了测试而调用真实
  API（花钱且非必要）；除非某条结论只能靠真机验证，那就标为"未验证"。

## 每条发现必须包含

```
### [P?] 一句话标题
- 位置：giftia/memory.py:123-140（函数名）
- 严重度：P0 阻断 / P1 高 / P2 中 / P3 低；置信度：高/中/低（没跑出证据一律标"低/疑似"）
- 现象与影响：会发生什么、对用户/数据/线上有什么后果
- 触发条件：最小复现步骤（命令级）
- 证据：真实输出片段（不要凭印象写）。运行过就给命令 + 关键输出
- 建议修法：最小改动方向，不要直接改
```

**没有证据的怀疑，明确写"未验证"**。禁止把"代码看起来像有问题"写成确定结论。
反过来，**已跑出反例的必须贴输出**。

## 严重度定义

- **P0**：数据损坏/丢失、线上崩溃、隐私越界（别人的私聊记忆泄漏到群里/别人面前）、
  凭证泄漏、明确的资金/额度失控。
- **P1**：功能与文档承诺相反、核心机制静默失效、消息丢失或重复发送、
  真实场景下必然触发的异常路径（未捕获异常导致进程退出、事务未回滚、
  并发写冲突、无限循环/无界增长）。
- **P2**：边界条件错误、错误处理不当、资源未释放、可读性/可维护性问题、
  配置项声明了但没生效。
- **P3**：风格、注释错别字、轻微冗余。

## 工作方式

1. **先静态读通你负责的模块**，画出数据流和状态机；不要只看被 diff 的地方。
2. **再写探针脚本验证你的怀疑**（脚本放 `.review/probes/`，只有你自己写；文件名带你的名字前缀）。
   探针要断言式：能打印 PASS/FAIL 和实际值。
3. 重点找这几类问题（本项目自己宣称的机制，都要试着打脸）：
   - 记忆分区/隐私：群聊、他人私聊能否查到搭档的私事（`scope`+`subject_id` 过滤是否在**所有**读取路径上）
   - 记忆退化：`effective_blur()` 是否真的随阶段/层级/保留率变化；`top_k`/`min_score`/`relative_cutoff` 是否都生效
   - 召回强化：`recall_count` 是否真的只在"说出口"时 +1；会不会被刷
   - 时间与整数运算：小时/天换算、时区、DST、`round()` 银行家舍入、负数剩余寿命
   - SQLite：事务边界、并发、WAL、连接泄漏、SQL 注入（字符串拼接的表名/列名）
   - 适配器（C 负责）：协议字段、去重、幂等、重连退避、消息 id 复用、被动回复窗口、异常吞掉
   - 工具循环：`tools_max_rounds` 是否真的封顶、失败路径是否会死循环
4. **报告文件**：写到 `.review/findings/<你的名字>.md`（先建目录），按严重度排序。
   一发现一标题。末尾加一节"未验证的怀疑"。**只写你自己的文件**，不要动别人的。

## 已有的测试资产（可复用，不要改）

- `tools/selftest.py`（218 项端到端）、`tools/ws_selftest.py`（17 项）、
  `tools/qq_protocol_selftest.py`（69 项）、`tools/audit.py`（11 项静态审计）、
  `tools/demo_life.py`、`tools/live_check.py`（需要真 key，跳过）。
- 这些测试**能过不代表没 bug**：它们多半只验证"设计者想到的路径"。
  你的价值在于找它们没覆盖的路径。可以借鉴它们搭环境的方式（看 `tools/selftest.py` 开头）。
