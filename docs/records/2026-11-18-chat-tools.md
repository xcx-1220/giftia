# 艾拉聊天工具与反馈

2026-11-18（Asia/Shanghai）。新增独立 Cordis bundle `giftia-chat-tool-bundle@0.1.0`，使用生产锁定的 DSH `0.2.2-rc.3`。`isla_calculate` 支持两数的加减乘除、百分比和乘方，`isla_convert_units` 支持同类长度、质量、体积、时间和温度单位。结果保留 12 位有效数字。工具自身在服务器本地计算。腾讯云访问百科和天气的上游探测出现超时，首批选择独立可用的本地工具。

生产配置只给搭档开放。工具在进入模型可见列表前核对当前回合和角色，执行时复查。普通聊天的实际人格上下文在工具调用前后继续生效，追加指引要求艾拉用自然语言说明结果、错误和能力边界，保留准确数值和单位。网页将工具状态显示为「我算一下…」「算好了」等简短提示，实际聊天回复由模型生成。

本机与腾讯云隔离环境通过以下验收。

- `276` 的 `18.5%` 返回 `51.06`，`32°F` 返回 `0°C`。
- 除零和跨量纲换算产生工具错误。工具结果、错误和原有人格指引出现在后续模型请求，网页保存模型给出的聊天答复与独立状态事件。
- 访客模型请求看不到这两项工具，模拟模型强行调用时也被拒绝。停用和卸载后，普通聊天继续成功。
- Edge 同域缓存复测通过旧 worker 更新、离线壳与恢复会话。旧壳首次出现时刷新一次即可进入新版。

```powershell
$env:PYTHONPATH = $PWD.Path
python harness/verify_chat_tools.py --gateway-port 18187 --model-port 18188
python harness/verify_r2_cache.py --openssl OPENSSL_PATH
```

```text
sudo bash BASE/source/harness/r2_cloud.sh BASE chat-tools-test
```

记录位于 `harness/.stage1/r2-validation/` 下的 `chat-tools-20261118.txt`、`chat-tools-51e2c44d18/`、`chat-tools-cache-20261118.txt` 和 `cloud-20261117/chat-tools-acceptance.txt`。腾讯云隔离目录为 `/opt/giftia-harness-test/stage5-20261118-chat-tools-r2`，验收状态为 `state/chat-tools-checks/chat-tools-853a17348b`。模型请求均为合成验收，涵盖人格指令到达模型和结果回传。真实回复的自然程度仍由正常使用观察评价。

正式站 `https://giftia.icu` 已发布，安装归档 SHA-256 为 `920d639ba26d0956c06f2aecb8344b0932886961f77477eeb197d995dfa4c8eb`，隔离候选归档为 `ae27ff5f813c276084c629295dba0a857b6a407a868ec9d6a12dda7b46af180f`。两次操作前均无活动网页任务。第一次因受控 PATH 中缺少 `runuser` 而自动回退，确认旧 profile 和公开健康恢复后改用 `/usr/sbin/runuser`，重试发布成功。正式回退点为 `/opt/giftia/data/releases/chat-tools-hi3JgcQq`，保存了原 profile、角色服务文件和网页。公网已提供新版静态文件，生产验证没有发送付费聊天请求。

```text
sudo bash /opt/giftia-harness/source/harness/chat_tools_deploy.sh rollback /opt/giftia/data/releases/chat-tools-hi3JgcQq
```

发布、自动回退及再发布见证据目录的 `cloud-20261117/chat-tools-production.txt`、`chat-tools-production-retry.txt`、`chat-tools-final.txt` 和 `chat-tools-public-auth.txt`。
