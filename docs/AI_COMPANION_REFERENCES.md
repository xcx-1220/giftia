# 艾拉可参考的开源角色与陪伴项目

调研日期：2026-10-26。范围：GitHub 仓库、关键源码与项目官方文档的只读调研。未安装第三方项目，未迁移现有架构，未追加聊天模型实验。

调研建议已并入[改进总方案](ISLA_CHARACTER_EXPRESSION_PLAN.md)。本页保留来源、设计判断与复查入口，后续批次、调用上限和发布验收以总方案为准。

本次关注已确认的 B 方向：青涩、好奇、期待，初识有温度但小心，随信任增加更愿意袒露心声。判断依据是可以迁移的机制，不以项目宣传、截图或 star 数证明长期聊天质量。

## 1. 优先参考哪些项目

| 项目 | 查阅深度 | 值得参考的部分 | 对艾拉的建议 |
|---|---|---|---|
| [MaiBot / 麦麦](https://github.com/Mai-with-u/MaiBot) | README、表达学习/审核/抽样源码 | 学习“情境 → 表达方式”，保留来源、过滤与审核 | 建立有来源、可纠正的小规模表达习惯，不只保存事实和情绪 |
| [ChatHaruhi](https://github.com/LC1332/Chat-Haruhi-Suzumiya) | README、ChatHaruhi 实现 | 根据当前输入检索角色场景，在预算内加入生成上下文 | 试验独立的少量原创示范对话，让模型看到青涩、温度怎样具体表达 |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern) | 仓库入口、官方角色设计文档 | 人物描述、情境、开场、对话示范各有用途 | 人物事实、当前关系和表达示范分层，避免把全部要求堆进 core |
| [Storyhold](https://github.com/cspiritsong/Storyhold) | README、epistemic.js、longterm.js 等 | 角色知识视角、来源范围、非亲历记忆的 secondhand 注入 | 给“听用户讲过”明确的来源语义，避免变成艾拉读过、做过 |
| [dsh-whale-persona](https://github.com/OMGLogic/dsh-whale-persona) | README、lib/index.mjs | 简洁人物锚点，作为独立提示段加入宿主 | 借鉴风格锚点与注入层次，保留艾拉自己的气质 |
| [whale-girl](https://github.com/vlln/whale-girl) | README | 桌宠对真实运行事件产生动作反馈；可选本地会话里程碑 | 后续增强在场感时，让反馈对应真实事件；文字质量先独立验收 |
| [whale-moe](https://github.com/zhushenghui123/whale-moe) | README，确认默认分支 master | 项目声明记忆审核/锁定、七档关系、特质和心情锚点 | 借鉴可查看、可纠正的成长记录；具体效果和代码路径需再深入验证 |
| [Project AIRI](https://github.com/moeru-ai/airi) | README、包结构、core-character 说明 | 角色契约、语音、Live2D/VRM 与不同表现端的分工 | 作为后续语音和形象交互参考；不把外观表现当作文字人格验证 |

这些项目中，当前最适合深入借鉴的是 MaiBot、ChatHaruhi/SillyTavern、Storyhold。鲸鱼娘相关项目应按人设插件、桌宠、皮肤和成长助手分别理解；尚未确认它们与用户最初截图中的机器人属于同一实现。

## 2. MaiBot：把相处沉淀为表达习惯

已核对的实现：

- [expression_learner.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/learners/expression_learner.py)：候选包含 situation、style 和来源信息，学习真实会话消息，过滤后进入写入流程；并发闸门控制批次，规划器思考不作为聊天表达学习材料。
- [expression_review_store.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/learners/expression_review_store.py)：有审核状态、拒绝标记、AI 审核记录和人工恢复记录。
- [weighted_sampling.py](https://github.com/Mai-with-u/MaiBot/blob/main/src/learners/weighted_sampling.py)：按出现计数形成有界权重并抽样，避免一次注入所有表达。

对艾拉的迁移提案：在现有偏好与互动反馈之外，识别少量“交流情境与表达倾向”的关联。例如，用户分享完成一件麻烦事时，更喜欢她先有自己的高兴和反应，而非连续确认细节。

来源首先采用用户明确认可的回复或维护者选定的示范。保留来源消息、适用情境、用户纠正和失效条件。重复出现本身不等于用户喜欢；不能直接以模型自己反复生成的次数当作正反馈，否则会重现旧的告别桥段强化问题。

初版可复用现有带来源的状态，不先照搬群聊流行语学习、完整插件系统或更多人格分数。艾拉的稳定底色继续约束习惯，避免逐渐完全模仿用户的口头禅。

## 3. ChatHaruhi 与 SillyTavern：让模型看到表达怎样发生

[ChatHaruhi.py](https://github.com/LC1332/Chat-Haruhi-Suzumiya/blob/main/ChatHaruhi2.0/ChatHaruhi/ChatHaruhi.py) 的 `add_story()` 会检索场景，按 token 预算加入提示；生成流程同时使用人物设定与近期对话。这提供了一个与单纯罗列性格形容词不同的参照方式。

[SillyTavern 官方角色设计文档](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)分别说明人物描述、情境、开场与示范对话的作用，并明确示范对话可随上下文预算进入或退出。其文档示例的具体风格不等于艾拉应采用的风格。

这使我建议调整此前“例句只放回归材料”的做法：保留例句不写死进人物 core，同时试验一个独立、很小、可以关闭的表达示范层。

候选实验：

1. 整理 12～20 组原创短对话，覆盖初识、被夸、好奇、不同意见、表达愿望和普通告别。
2. 每轮最多选择 0～2 组，优先符合当前关系和交流情境；没有匹配就不加入。
3. 明确标注为虚构的风格示范，不能作为真实回忆、人物已读作品或与当前用户的历史。
4. 示范中的称呼、书名与情节不得进入长期记忆；检查生成是否照抄、是否把示范往事当事实。
5. 固定模型与已有记忆，比较仅 core 与 core 加少量示范，分别评人物感、自然度、事实错误、复读和延迟。

这仍是假设，尚未证明能改善 DeepSeek 上的艾拉。不把本次人工样本全部塞进每轮，也不把大段原作台词或其他机器人的完整口吻移植过来。

## 4. Storyhold：知道一件事与经历过一件事要分开

已核对：

- [epistemic.js](https://github.com/cspiritsong/Storyhold/blob/main/epistemic.js)：按角色组织知识，抽取记录带来源消息范围。
- [longterm.js](https://github.com/cspiritsong/Storyhold/blob/main/longterm.js)：`shouldInjectMemory()` 根据 witnessed_by 判定 full/secondhand，在注入中标记 secondhand，或按设置省略非亲历内容。
- [README](https://github.com/cspiritsong/Storyhold#readme)：关系历史、角色视角、活动状态及记忆命名空间等设计。其主要场景是叙事角色扮演，不代表整套状态均适合日常陪伴。

对艾拉最直接的用途，是修正本轮评测中反复出现的错误：你介绍一段小说情节后，她说“我读的时候”“我合上书后”。

建议沿用已有来源消息，并进一步区分以下语义：用户讲述、艾拉通过真实工具获得、明确共同虚构、推测或想象。置信度与来源各自有用：即使确信你讲过，也不代表她亲自经历过。

特别注意：角色出现在聊天里，只能证明听到了你的叙述，不能证明读过故事或参与了故事中的事件。因此不能直接把“聊天在场”映射成“事件亲历”，需要按事件本身判断。

先从提示中的来源表述和典型回归用例验证，必要时再补充状态字段。保留当前 scope/subject 隔离、撤回和遗忘边界。

## 5. 鲸鱼娘与 AIRI：辨认在场感来自哪里

[dsh-whale-persona 的源码](https://github.com/OMGLogic/dsh-whale-persona/blob/main/lib/index.mjs)把人设注册为独立的提示段；默认内容是少量风格标签。它主要承担人物语气的注入，不能据此证明长期关系、记忆或推理质量。

[whale-girl 的 README](https://github.com/vlln/whale-girl#readme)描述了任务运行、等待、完成、出错等事件对应的表现，以及默认关闭的可选本地会话里程碑。README 明确这部分会话记忆不进入模型上下文。因此，桌宠显得有反应，不等于语言模型知道那些事件。

[whale-moe](https://github.com/zhushenghui123/whale-moe/tree/master)是另一类成长语音助手，README 声明可审核和锁定记忆、关系等级及心情锚点。可以作为成长可视化的候选参考；本次未运行它，也未核对全部成长实现，不评价其实际聊天质量。

[AIRI](https://github.com/moeru-ai/airi)展示了语音、浏览器与桌面表现、角色模型等不同层次；[core-character 包说明](https://github.com/moeru-ai/airi/blob/main/packages/core-character/README.md)明确共享角色契约与渲染端职责分工。当前 README 仍把部分记忆项目列为进行中/未完成，不能把它当作已经验证成熟的长期关系方案。

对艾拉，后续可让界面的思考、接收、完成、断线反馈对应真实状态；语音和形象作为独立体验层。当前先验收纯文字是否具有人物感，再评估这些表现层的收益。

## 6. 建议如何调整后续工作

下表保留调研得到的相对优先级。执行时已整合为：第一批表达示范/经历来源/聊天节奏，第二批关系成长/表达习惯，第三批正常使用与发布；细节见总方案。

| 优先级 | 工作 | 首轮规模 | 要验证的结果 |
|---|---|---|---|
| P0 | 表达示范层的小实验 | 12～20 组原创示范，每轮 0～2 组 | 比只有人物说明更自然，且没有复读、泄漏虚构往事 |
| P0 | 知识来源与亲历区分 | 书、电影、用户行程、虚构情景等定向用例 | 不再把用户叙述说成自己经历 |
| P1 | 有来源的表达习惯 | 先人工选定少量正反馈，再验证提取 | 产生属于相处的变化，能被纠正，不形成固定桥段 |
| P1 | 持续关注的分寸 | 重逢、未完话题、已经完成的事 | 不在用户刚回来时连续追问旧话题，已完成事项能退出 |
| P2 | 真实事件的表现反馈、语音与形象 | 在文本质量可接受后单独试用 | 增加在场感，同时保持语言和状态一致 |

沿用已有 DeepSeek Off / 1.15 基线。每次仅比较一个主要变化，沿用现有隔离重放和盲评工具；研究过程本身不启动新的付费模型实验或发布。

这项调研最重要的设计修正是：人物说明之外，增加“表达示范”和“从真实相处中形成的表达习惯”两种候选依据，同时让它们与事实记忆保持清楚的语义边界。

## 7. 复查入口

查阅时记录的部分分支头，便于下一轮核对来源：

| 仓库 | 分支 | commit |
|---|---|---|
| Mai-with-u/MaiBot | main | 062d45cf8bd4272ac57d7ec03fcf405e5a458211 |
| LC1332/Chat-Haruhi-Suzumiya | main | 290bf4ad22076156083804013012847a77c0646c |
| OMGLogic/dsh-whale-persona | main | ff77e9d17c945ee5a6a2ddcf2383074cd2a81e58 |
| vlln/whale-girl | main | 4b4350478c82d22d506a93567e52ab0ff90c2585 |
| cspiritsong/Storyhold | main | 4c4bf5b8343a389bd55f6a5603b1aa39fd3542e6 |

文件链接使用仓库可阅读的分支路径。后续版本可能变化，正式借鉴某个实现前按记录版本复查对应文件。
