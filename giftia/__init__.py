"""Giftia —— 一个本地优先的陪伴型对话内核。

设计目标是"她"（人格 + 寿命 + 记忆退化 + 回收日）与平台彻底解耦：
换平台只需要写一层很薄的适配器，调用 giftia.brain.handle_message()。

模块地图：
    config     配置加载（settings.toml / persona.toml）
    clock      虚拟时钟（可以快进，用来演示一生）
    store      SQLite 持久化（记忆、消息、日志、状态）
    lifespan   寿命倒计时与退化阶段
    memory     记忆整理、混合检索、模糊化视图
    llm        DeepSeek 客户端 + 离线 mock
    persona    系统提示词组装
    recycle    回收仪式（回忆录、封存、换代）
    brain      编排层：Giftia 类 + 平台无关入口
    cli        终端界面
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
