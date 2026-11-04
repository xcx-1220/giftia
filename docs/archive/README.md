# 历史归档索引

归档日期：2026-11-04。这里保存历次设计、讨论、实测和审查的证据。**当前执行只看 [统一未来计划](../ROADMAP.md)**；当前架构、人物、运维从[文档导航](../README.md)进入。

正文保留当时的说法和状态，仅加归档提示并修正本地 Markdown 链接。历史文章中的“下一步”“当前”“总方案”、行号、价格、平台限制和测试数量，不自动适用于今天。归档本身不说明其中所有问题已完成；未完成主题的去向见路线图第 11 节。

## 按时间与用途阅读

| 目录 | 内容 |
|---|---|
| [项目沿革](2026-10-project/README_LEGACY.md) | 旧完整 README、网页首装/事故背景、QQ 调研清单和旧 C 角色卡；正文末次整理可能晚于 10 月，目录表示该轮项目阶段 |
| [聊天质量](2026-10-quality/CHAT_QUALITY_RESULTS.md) | 10 月 25 日 A～E 的设计、实现、实测、部署与恢复点 |
| [人物候选](2026-10-character/ISLA_CHARACTER_IMPLEMENTATION.md) | 10 月 26 日 B 方向的讨论、方案、基线、样本与实际结果；没有发布 |
| [审查与修复](reviews/FIX_REPORT.md) | 早期审查问题与后续修复；源头探针和登记册保留在仓库 `.review/` |

人工样本是设计讨论，不能当模型输出；真实样本记录失败与不理想片段，不作为承诺效果。临时原始数据在 `.review/tmp/`，不随 Git 分发；长期结论已保留在相应记录中。

## 旧路径迁移表

旧路径不再留空壳占位文档，以免重新形成两套入口。下表用于定位旧引用；README 和 WEB 原路径已经换成精简后的当前文档。

| 原路径 | 保存位置 | 性质 |
|---|---|---|
| `README.md` | [README.md](2026-10-project/README_LEGACY.md) | 历史设计或说明 |
| `docs/WEB.md` | [WEB.md](2026-10-project/WEB_LEGACY.md) | 历史设计或说明 |
| `docs/BACKLOG.md` | [BACKLOG.md](2026-10-project/BACKLOG.md) | 历史设计或说明 |
| `docs/人设卡-v2-艾拉.md` | [人设卡-v2-艾拉.md](2026-10-project/人设卡-v2-艾拉.md) | 历史设计或说明 |
| `docs/CHAT_QUALITY_PLAN.md` | [CHAT_QUALITY_PLAN.md](2026-10-quality/CHAT_QUALITY_PLAN.md) | 历史设计或说明 |
| `docs/CHAT_QUALITY_RESULTS.md` | [CHAT_QUALITY_RESULTS.md](2026-10-quality/CHAT_QUALITY_RESULTS.md) | 已发生的实现 / 实验 / 修复 |
| `docs/AI_COMPANION_REFERENCES.md` | [AI_COMPANION_REFERENCES.md](../reference/AI_COMPANION_REFERENCES.md) | 外部调研参考；未来建议归入统一计划 |
| `CODE_REVIEW_REPORT.md` | [CODE_REVIEW_REPORT.md](reviews/CODE_REVIEW_REPORT.md) | 历史设计或说明 |
| `FIX_REPORT.md` | [FIX_REPORT.md](reviews/FIX_REPORT.md) | 已发生的实现 / 实验 / 修复 |
| `docs/ISLA_CHARACTER_BASELINE.md` | [ISLA_CHARACTER_BASELINE.md](2026-10-character/ISLA_CHARACTER_BASELINE.md) | 历史设计或说明 |
| `docs/ISLA_CHARACTER_BRIEF.md` | [ISLA_CHARACTER_BRIEF.md](2026-10-character/ISLA_CHARACTER_BRIEF.md) | 历史设计或说明 |
| `docs/ISLA_CHARACTER_CANDIDATES.md` | [ISLA_CHARACTER_CANDIDATES.md](2026-10-character/ISLA_CHARACTER_CANDIDATES.md) | 人工设计与对照 |
| `docs/ISLA_CHARACTER_EXPRESSION_PLAN.md` | [ISLA_CHARACTER_EXPRESSION_PLAN.md](2026-10-character/ISLA_CHARACTER_EXPRESSION_PLAN.md) | 历史设计或说明 |
| `docs/ISLA_CHARACTER_IMPLEMENTATION.md` | [ISLA_CHARACTER_IMPLEMENTATION.md](2026-10-character/ISLA_CHARACTER_IMPLEMENTATION.md) | 已发生的实现 / 实验 / 修复 |
| `docs/ISLA_CHARACTER_LIVE_SAMPLES.md` | [ISLA_CHARACTER_LIVE_SAMPLES.md](2026-10-character/ISLA_CHARACTER_LIVE_SAMPLES.md) | 真实模型输出 |
| `docs/ISLA_CHARACTER_RELATIONSHIP_GROWTH.md` | [ISLA_CHARACTER_RELATIONSHIP_GROWTH.md](2026-10-character/ISLA_CHARACTER_RELATIONSHIP_GROWTH.md) | 人工设计与对照 |
| `docs/ISLA_CHARACTER_SAMPLES.md` | [ISLA_CHARACTER_SAMPLES.md](2026-10-character/ISLA_CHARACTER_SAMPLES.md) | 人工设计与对照 |

## 本轮整理记录

- 根 README 缩为项目入口；机制、运行使用和部署分别放入当前指南。
- 原 BACKLOG、聊天质量计划、人物总方案与阶段草案统一退出执行入口；未完成项按任务 ID 合并到路线图。
- 保留历史原文及失败样本，修正移动后的可解析 Markdown 链接；反引号中的历史路径与命令按原语境保留。
- `.review/` 不移动探针、登记册或证据，只修正必要的文档反向链接；旧版本的文档修补脚本不作为当前整理工具。
- 同日只读运行基线转为可跟踪的 [记录](../records/2026-11-04-baseline.md)。
