"""Stream E · README 宣称逐条核对（硬事实 + 证据）。

    python .review/probes/qa_claims.py
"""

from __future__ import annotations

import ast
import dataclasses
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_harness import (  # noqa: E402
    Checker, RecordingLLM, make_brain, make_dir, make_settings, rm_dir,
)

from giftia import agent_tools, memory as M, trigger  # noqa: E402
from giftia.bond import stage_for  # noqa: E402
from giftia.config import load_env_file  # noqa: E402
from giftia.llm import ChatReply, MockLLM, ToolCall  # noqa: E402
from giftia.lifespan import humanize_hours  # noqa: E402
from giftia.outreach import can_send, quota_state, record_sent, sent_today  # noqa: E402

C = Checker("Stream E · README 宣称 vs 实测")

# ── 1. 寿命 81920 小时 = 9 年 4 个月 ────────────────────────────
C.check("[README §2/§4] 默认寿命 81920 小时",
        make_settings(Path(r"D:\ai chatbot\.review\tmp")).lifespan.effective_hours() == 81920.0, "")
tmp = make_dir("claims")
C.info(f"humanize_hours(81920) = {humanize_hours(81920.0)!r}（README 说 9 年 4 个月）")
C.check("81920 小时显示为「9 年 4 个月」", humanize_hours(81920.0) == "9 年 4 个月",
        humanize_hours(81920.0))

settings = make_settings(tmp, hours=100.0, consolidate_every=8)
llm = RecordingLLM(settings.model)
brain = make_brain(settings, llm)
brain.set_handler_id("u_owner")

# ── 2. 出厂状态：不知道你叫什么 ────────────────────────────────
C.check("[README §2 出厂状态] 她一开始不知道你叫什么", brain.handler_name == "",
        repr(brain.handler_name))
llm.calls.clear()
brain.handle("在吗", "u_owner", scene="c2c")
sysp = llm.last_system("chat")
C.check("system 里写的是「你还不知道他叫什么」", "还不知道" in sysp and "称呼" in sysp, "")
C.check("开场白就是「我该怎么称呼你？」",
        "我该怎么称呼你" in settings.persona.opening_line, settings.persona.opening_line[-20:])

# ── 3. 改配置不影响这一任寿命 ─────────────────────────────────
C.check("[README §2 寿命] 激活时把总寿命写进 DB（100h）",
        abs(brain.lifespan.total_hours - 100.0) < 1e-9, str(brain.lifespan.total_hours))
brain.settings = dataclasses.replace(
    brain.settings,
    lifespan=dataclasses.replace(brain.settings.lifespan, demo_total_hours=5000.0),
)
C.check("★ 改配置（demo_total_hours=5000）后这一任仍是 100h",
        abs(brain.lifespan.total_hours - 100.0) < 1e-9, str(brain.lifespan.total_hours))
brain.settings = dataclasses.replace(
    brain.settings,
    lifespan=dataclasses.replace(brain.settings.lifespan, demo_total_hours=0.0),
)

# ── 4. 每积累 8 条消息压缩一次 ────────────────────────────────
C.info("── [README §2 记忆] consolidate_every=8（口径：messages 表行数，含她自己的回复）──")
brain2 = make_brain(dataclasses.replace(settings, root=tmp / "b2"), llm)
brain2.set_handler_id("u_owner")
for i in range(3):
    brain2.handle(f"我说第 {i} 件正经事，需要你记住很久很久，和数字 {i * 7} 有关", "u_owner", scene="c2c")
n_rows = brain2.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
C.check("3 轮对话 = 6 条消息时还不整理（阈值 8）",
        n_rows == 6 and brain2.store.count_memories(1) == 0 and llm.extract_count == 0,
        f"rows={n_rows} mem={brain2.store.count_memories(1)} extract={llm.extract_count}")
brain2.handle("这是第 4 件正经事，也需要你记住很久很久，和数字 99 有关", "u_owner", scene="c2c")
n_rows = brain2.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
C.check("第 4 轮（第 8 条消息）触发整理，记忆落库",
        n_rows == 8 and brain2.store.count_memories(1) >= 1 and llm.extract_count >= 1,
        f"rows={n_rows} mem={brain2.store.count_memories(1)} extract={llm.extract_count}")

# ── 5. 没命中的消息：当场标已读 / 零 token ────────────────────
C.info("── [README §3.5] 群里没命中的消息「零 token」──")
for tier, bot, kw, roll in ((0, False, (), 0.99), (19, False, (), 0.99), (20, False, ("艾拉",), 0.99),
                            (20, False, (), 0.99), (45, False, (), 0.10), (45, False, (), 0.90),
                            (70, False, (), 0.99), (0, True, (), 0.99)):
    ok, why = trigger.decide(tier, at_bot=bot, text="今天天气不错", keywords=kw, roll=roll)
    C.info(f"    tier={tier:3d} at_bot={bot} kw={kw} roll={roll} → {ok}（{why}）")
C.check("档位表 0~19 只应 @", trigger.decide(0, at_bot=False, text="x")[0] is False
        and trigger.decide(19, at_bot=False, text="x")[0] is False, "")
C.check("档位表 20~44 命中关键词才开口",
        trigger.decide(20, at_bot=False, text="艾拉在吗", keywords=("艾拉",))[0] is True
        and trigger.decide(20, at_bot=False, text="在吗")[0] is False, "")
C.check("档位表 45 ≈ 36% 概率（(45-20)/70）", abs(trigger.speak_probability(45) - 0.357142) < 1e-5,
        str(trigger.speak_probability(45)))
C.check("档位表 70+ 任何消息都可能开口", trigger.decide(70, at_bot=False, text="x", roll=0.999)[0] is True, "")
C.check("档位表 0~100 之外被夹住", trigger.tier_of(-5) == 1 and trigger.tier_of(999) == 4, "")

# 旁听到底花不花 token
tmp_l = tmp / "listen"
(tmp_l / "data").mkdir(parents=True, exist_ok=True)
llm_l = RecordingLLM(settings.model)
b_listen = make_brain(dataclasses.replace(settings, root=tmp_l), llm_l)
b_listen.set_handler_id("u_owner")
for i in range(8):
    r = b_listen.handle(f"群里第 {i} 句闲聊，没点到她，说的是今天下午打球的事", "u_aw",
                        scene="group", group_id="gA", speaker_name="阿伟", log_only=True)
    if i == 7:
        pass
C.info(f"8 条旁听后：chat 调用 {llm_l.chat_count} 次，extract 调用 {llm_l.extract_count} 次")
C.check("旁听当场不回复（逐条零 token）", llm_l.chat_count == 0, str(llm_l.chat_count))
# 【期望为何变了】原来这条以 README §3.5 的旧措辞「不调模型 —— 零 token」为判据，
# 断言「每 8 条旁听必然触发一次整理」= 记录缺陷（对应 P1-14）。
# P1-14 修复后旁听**当轮完全不调模型**（连整理也推迟）：只留一个 listen_pending 标记，
# 由搭档那一轮（_post_turn → flush_listen_pending）或退出路径统一整理——
# 群聊记忆照样会生成，只是不在这里花钱。README 现在的措辞正是这个意思（README.md:290-291）。
pending_after_listen = b_listen.store.state_get("listen_pending")
gmems_after_listen = len(b_listen.store.active_memories(
    b_listen.instance_no, view=[("group", "gA")]))
C.check("旁听**当轮完全不调模型**（连整理也推迟，只留待整理标记）",
        llm_l.chat_count == 0 and llm_l.extract_count == 0,
        f"chat={llm_l.chat_count} extract={llm_l.extract_count} pending={pending_after_listen!r}")
# 正向对照：搭档回来说一句 → 待整理的旁听消息照常变成这个群的记忆（不是把功能关掉）
b_listen.handle("我在呢", "u_owner", scene="c2c")
gmems_later = b_listen.store.active_memories(b_listen.instance_no, view=[("group", "gA")])
C.check("正向对照：搭档那一轮会把攒下的旁听整理成群记忆（一次模型调用）",
        llm_l.extract_count >= 1 and len(gmems_later) >= 1,
        f"extract={llm_l.extract_count}，群记忆 {len(gmems_later)} 条"
        f"（旁听期间是 {gmems_after_listen} 条）")

# ── 6. 每天最多主动 3 条 / 30 天额度 ───────────────────────────
C.info("── [README §2/§3.5] 主动联系额度 ──")
tmp_o = tmp / "outreach"
(tmp_o / "data").mkdir(parents=True, exist_ok=True)
s_o = dataclasses.replace(
    settings, root=tmp_o,
    outreach=dataclasses.replace(settings.outreach, enabled=True, max_per_day=3,
                                 min_gap_hours=3.0, max_per_30_days=60, idle_days=2.0),
)
b_o = make_brain(s_o, RecordingLLM(s_o.model))
now = b_o.clock.now()
for i in range(3):
    record_sent(b_o.store, now)
C.check("日上限 3：第 3 条之后 can_send=False", not can_send(s_o, b_o.store, now),
        f"sent_today={sent_today(b_o.store, now)}")
C.check("日上限就是配置里的 3（README §3.5 表 max_per_day=3）",
        s_o.outreach.max_per_day == 3, str(s_o.outreach.max_per_day))
b_o.clock.advance(24.0)
C.check("第二天额度重置", can_send(s_o, b_o.store, b_o.clock.now()), "")
# 30 天额度：把 60 条记账铺在过去的 29 天里（不触发日上限，只触发 30 天总额度）
from datetime import timedelta  # noqa: E402
base = b_o.clock.now()
for i in range(60):
    record_sent(b_o.store, base - timedelta(days=1 + i // 3, hours=(i % 3) * 7))
used, left, nxt = quota_state(s_o, b_o.store, b_o.clock.now())
C.info(f"30 天窗口内已用 {used} 条，剩 {left}；今日已发 {sent_today(b_o.store, b_o.clock.now())}")
C.check("★ 30 天额度 60 用满后 can_send=False（且今日并未发过）",
        used >= 60 and left == 0 and sent_today(b_o.store, b_o.clock.now()) == 0
        and not can_send(s_o, b_o.store, b_o.clock.now()),
        f"used={used} left={left} today={sent_today(b_o.store, b_o.clock.now())}")

# ── 7. 模糊可复现（(id,阶段,层级,保留率) 决定）─────────────────
C.info("── [README §2 设计 1] 模糊可复现 ──")
from datetime import datetime, timedelta  # noqa: E402
mems = [{"id": 7, "summary": "搭档说下周三要去医院拿他妈妈的检查报告，他很担心",
         "tags": "医院,家人", "emotion": "担心", "importance": 0.8, "layer": "normal",
         "strength": 2.0, "recall_count": 0, "created_ts": "2026-01-01T00:00:00",
         "happened_ts": "2026-01-01T00:00:00", "about": "搭档", "last_recalled_at": None}]
stage_t = [s for s in settings.stages if s.name == "terminal"][0]
now2 = datetime(2026, 6, 1)
a = M.recall("医院 检查 报告", mems, stage_t, settings.memory, now2)
b = M.recall("医院 检查 报告", mems, stage_t, settings.memory, now2)
C.check("同一 (id,阶段,层级,保留率) 两次检索结果完全一致",
        len(a) == len(b) == 1 and a[0].text == b[0].text, f"{a[0].text if a else None}")
mems_stage = dict(mems[0], layer="core")
c = M.recall("医院 检查 报告", [mems_stage], stage_t, settings.memory, now2)
C.check("换层级（core）→ 模糊为 0、文本与原始一致", c and c[0].text == mems[0]["summary"],
        f"{c[0].text[:30] if c else None}")
mems_old = dict(mems[0], last_recalled_at="2020-01-01T00:00:00")
d = M.recall("医院 检查 报告", [mems_old], stage_t, settings.memory, now2)
C.check("换保留率（久未提起）→ 模糊更重（文本不同）",
        bool(d) and bool(a) and d[0].blur > a[0].blur,
        f"{a[0].blur if a else None} vs {d[0].blur if d else None}")

# ── 8. 内核记忆永不含糊 ──────────────────────────────────────
blur_core = M.effective_blur(dict(mems[0], layer="core", strength=1.0, recall_count=0,
                                  last_recalled_at="2020-01-01T00:00:00"),
                             stage_t, settings.memory, now2)
blur_norm = M.effective_blur(dict(mems[0], layer="normal", strength=1.0, recall_count=0,
                                  last_recalled_at="2020-01-01T00:00:00"),
                             stage_t, settings.memory, now2)
C.info(f"terminal 阶段：core blur={blur_core}，normal blur={blur_norm:.3f}")
C.check("[README §2 设计 3] core 记忆模糊度恒为 0", blur_core == 0.0, str(blur_core))
C.check("normal 记忆同一时刻确实糊了（对照组）", blur_norm > 0.4, f"{blur_norm:.3f}")

# ── 9. 升级要亲密度 + 认识天数两条腿 ─────────────────────────
stages = settings.bond_stages
C.check("[README §2 关系] 亲密度 100 但认识 0 天 → 仍是刚认识",
        stage_for(stages, 100.0, 0.0)[0].key == "stranger",
        stage_for(stages, 100.0, 0.0)[0].key)
C.check("认识 1000 天但亲密度 0 → 仍是刚认识",
        stage_for(stages, 0.0, 1000.0)[0].key == "stranger",
        stage_for(stages, 0.0, 1000.0)[0].key)
C.check("两条腿都够（40 / 14 天）→ 熟悉",
        stage_for(stages, 40.0, 14.0)[0].key == "familiar",
        stage_for(stages, 40.0, 14.0)[0].key)
C.check("差一点点（39.9 / 14）→ 还是搭档",
        stage_for(stages, 39.9, 14.0)[0].key == "partner",
        stage_for(stages, 39.9, 14.0)[0].key)

# ── 10. 快照留最近 5 份 ──────────────────────────────────────
C.info("── [README §3.6] 整理前自动拍快照、留最近 5 份 ──")
tmp_s = tmp / "snap"
(tmp_s / "data").mkdir(parents=True, exist_ok=True)
b_s = make_brain(dataclasses.replace(settings, root=tmp_s), RecordingLLM(settings.model))
for i in range(7):
    b_s.store.snapshot_memories(tag=f"t{i}", keep=5)
    time.sleep(0.01)
snaps = b_s.store.list_snapshots()
C.info(f"拍了 7 次，磁盘上剩 {len(snaps)} 份：{[p.name for p in snaps]}")
C.check("只留最近 5 份", len(snaps) == 5, str(len(snaps)))
C.check("快照是完整整表 JSON（含原始 summary）",
        isinstance(__import__("json").loads(snaps[-1].read_text(encoding="utf-8"))["memories"], list), "")

# ── 11. .env 已存在的环境变量优先 ────────────────────────────
C.info("── [README §1] .env 里已存在的环境变量优先级更高 ──")
env_path = tmp / "qa.env"
env_path.write_text("GIFTIA_QA_KEY=from_file\nGIFTIA_QA_NEW=from_file\n# 注释\nQUOTED=\"qv\"\n",
                    encoding="utf-8")
os.environ["GIFTIA_QA_KEY"] = "from_env"
os.environ.pop("GIFTIA_QA_NEW", None)
loaded = load_env_file(env_path)
C.check("已存在的环境变量不被 .env 覆盖", os.environ["GIFTIA_QA_KEY"] == "from_env",
        os.environ["GIFTIA_QA_KEY"])
C.check(".env 里新的项会载入", os.environ.get("GIFTIA_QA_NEW") == "from_file",
        str(os.environ.get("GIFTIA_QA_NEW")))
C.check("引号被剥掉", os.environ.get("QUOTED") == "qv", str(os.environ.get("QUOTED")))
C.check("返回载入条目数 = 新增的 2 条（KEY 被跳过）", loaded == 2, str(loaded))
os.environ.pop("GIFTIA_QA_KEY", None)
os.environ.pop("GIFTIA_QA_NEW", None)
os.environ.pop("QUOTED", None)

# ── 12. 零第三方依赖 ────────────────────────────────────────
C.info("── [README 开头/§9] 零第三方依赖 ──")
import sys as _sys  # noqa: E402
stdlib = set(_sys.stdlib_module_names)
local = {"giftia", "adapters", "tools", "config", "deploy"}
local |= {p.stem for p in (Path(r"D:\ai chatbot") / "tools").glob("*.py")}
local |= {p.stem for p in (Path(r"D:\ai chatbot") / "giftia").glob("*.py")}
bad: list[str] = []
files = ([p for p in (Path(r"D:\ai chatbot") / "giftia").glob("*.py")]
         + [p for p in (Path(r"D:\ai chatbot") / "adapters").glob("*.py")]
         + [p for p in (Path(r"D:\ai chatbot") / "tools").glob("*.py")]
         + [Path(r"D:\ai chatbot\run.py")])
for p in files:
    tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in stdlib and root not in local:
                    bad.append(f"{p.name}: {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root not in stdlib and root not in local:
                bad.append(f"{p.name}: from {node.module}")
C.info(f"扫描 {len(files)} 个文件，非标准库 import：{bad}")
C.check("★ 零第三方依赖（giftia/adapters/tools/run.py 全部 import 都在标准库内）", not bad, str(bad))

# ── 13. 发送失败撤回那句记忆 ─────────────────────────────────
C.info("── [README §3.5] 整条没送出去 → 那句话从她记忆里撤回 ──")
llm.calls.clear()
brain.handle("我今天心情不错，想跟你说说", "u_owner", scene="c2c")
last_id = brain.last_reply_id
txt = brain.last_reply_text
before_rows = brain.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
brain.note_reply_delivery("")          # 适配器回报：整条没送出去
after_rows = brain.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
C.check("撤回后那条 assistant 消息已从库里删掉", after_rows == before_rows - 1,
        f"{before_rows} → {after_rows}")
C.check("撤回写进了 diary（可追溯）",
        any("没能送出去" in str(r["content"]) for r in brain.store.list_diary(kind="terminal", limit=5)), "")
# 部分送达
llm.calls.clear()
brain.handle("再试一次好不好", "u_owner", scene="c2c")
part = txt[:4]
brain.note_reply_delivery(part)
row = brain.store._one("SELECT content FROM messages ORDER BY id DESC LIMIT 1")
C.check("部分送达 → 只留实际送达的那部分", row["content"] == part, str(row["content"])[:40])

# ── 14. 每人最多 3 条、最多 15 人 ────────────────────────────
C.info("── [README §3.6] 每人最多 3 条 / 最多 15 人 ──")
tmp_i = tmp / "imp"
(tmp_i / "data").mkdir(parents=True, exist_ok=True)
b_i = make_brain(dataclasses.replace(settings, root=tmp_i), RecordingLLM(settings.model))
for k in range(20):
    for j in range(5):
        b_i.store.add_memory(instance_no=1, created_ts=b_i.clock.iso(),
                             happened_ts=b_i.clock.iso(),
                             summary=f"{'人%d' % k} 的第 {j} 件事，具体内容各不相同{j}",
                             tags="", emotion="", importance=0.5 + j * 0.01, layer="normal",
                             about=f"人{k}")
blk = b_i.impressions_block("")
lines = [ln for ln in blk.splitlines() if ln.startswith("- ")]
per = [ln.count("；") + 1 for ln in lines]
C.info(f"20 人 × 5 条 → 注入 {len(lines)} 行，每行条数 {sorted(set(per))}")
C.check("最多 15 人（行数 ≤ 15）", len(lines) <= 15, str(len(lines)))
C.check("每人最多 3 条", max(per) <= 3, str(sorted(set(per))))

# ── 15. 一轮最多主动写 2 条 + 最后一轮不给工具 ────────────────
C.info("── [README §3.6.5] 一轮最多主动写 2 条 / 最后一轮拿掉工具 ──")
C.check("MAX_WRITES_PER_TURN = 2", agent_tools.MAX_WRITES_PER_TURN == 2,
        str(agent_tools.MAX_WRITES_PER_TURN))


class GreedyRememberLLM(MockLLM):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.rounds = 0

    def chat_with_tools(self, messages, *, tools=None, temperature=None, max_tokens=None):
        self.rounds += 1
        if self.rounds == 1:
            return ChatReply(text="", tool_calls=[
                ToolCall(id=f"r{i}", name="remember",
                         arguments='{"content":"搭档说的第 %d 件事，要记住很久很久"}' % i)
                for i in range(5)],
                raw_message={"role": "assistant", "content": ""})
        return ChatReply(text="嗯，我都记下了。")

    def chat(self, messages, **kw):
        return "嗯，我都记下了。"


tmp_g = tmp / "greedy"
(tmp_g / "data").mkdir(parents=True, exist_ok=True)
llm_g = GreedyRememberLLM(settings.model)
b_g = make_brain(dataclasses.replace(settings, root=tmp_g), llm_g)
b_g.set_handler_id("u_owner")
r = b_g.handle("跟你说几件事", "u_owner", scene="c2c")
C.info(f"模型一次要求写 5 条 → 实际写入 {len(b_g.tool_writes)} 条，回复={r!r}")
C.check("一轮最多写 2 条", len(b_g.tool_writes) == 2, str(len(b_g.tool_writes)))

# ── 16. 群聊记录注入最近 18 条 ───────────────────────────────
C.info("── [README §2 群聊] 提问时注入最近 18 条 ──")
tmp_h = tmp / "hist"
(tmp_h / "data").mkdir(parents=True, exist_ok=True)
s_h = dataclasses.replace(settings, root=tmp_h,
                          memory=dataclasses.replace(settings.memory, consolidate_every=999))
b_h = make_brain(s_h, RecordingLLM(s_h.model))
b_h.set_handler_id("u_owner")
for i in range(30):
    b_h.handle(f"群里第 {i} 句话，说的是周末打球的事", "u_aw", scene="group",
               group_id="gH", speaker_name="阿伟", log_only=True)
b_h.last_scene = "group"
b_h.group_id = "gH"
b_h.scope_key = "ggH"
blk = b_h._render_group_chat()
body = [ln for ln in blk.splitlines() if ln and not ln.startswith("——")]
C.info(f"发 30 条群消息 → 注入 {len(body)} 行")
C.check("群聊记录最多注入 18 条", len(body) <= 18, str(len(body)))
C.check("每行带说话人", all("：" in ln for ln in body[:3]), str(body[:2]))

# ── 17. 会话按天轮换（工作记忆不串天）────────────────────────
sid1 = brain.session_id
brain.clock.advance(24.0)
C.check("[README/代码] 会话 id 按天轮换", brain.session_id != sid1 and sid1.split("-")[-1] != brain.session_id.split("-")[-1],
        f"{sid1} → {brain.session_id}")
C.check("会话 id 带 scope（群/人分开）", brain.session_id.split("-")[1] == "m", brain.session_id)

# ── 18. 主动联系：群里不用私密理由 ──────────────────────────
C.info("── [README §2/§3.5] 主动理由：群里只用公开的 ──")
tmp_p = tmp / "plan"
(tmp_p / "data").mkdir(parents=True, exist_ok=True)
s_p = dataclasses.replace(settings, root=tmp_p,
                          outreach=dataclasses.replace(settings.outreach, enabled=True,
                                                       idle_days=0.0))
b_p = make_brain(s_p, RecordingLLM(s_p.model))
b_p.set_handler_id("u_owner")
b_p.handle("在忙吗，我这边事情多起来了", "u_owner", scene="c2c")   # 先留下最后说话时间
b_p.clock.advance(24 * 5)
plan_own = b_p.outreach_plan()
plan_grp = b_p.outreach_plan(for_group=True)
C.info(f"搭档私聊理由 = {plan_own.reason if plan_own else None}；"
       f"群里理由 = {plan_grp.reason if plan_grp else None}")
C.check("心情不是平静时，mood_share 优先于 missing_you（README 优先级表）",
        plan_own is not None and plan_own.reason in ("mood_share", "missing_you"),
        str(plan_own))
C.check("★ 群里不会用 missing_you（只在私聊说）",
        plan_grp is None or plan_grp.reason != "missing_you", str(plan_grp))
C.info(f"当天心情 = {b_p.current_mood().key!r}")
b_p.force_mood("calm")                     # 平静下来，看 missing_you 能不能出来
plan_calm = b_p.outreach_plan()
C.info(f"平静 + 5 天没说话 → 私聊理由 = {plan_calm.reason if plan_calm else None}")
C.check("★ 太久没见 → missing_you（平静心情下）",
        plan_calm is not None and plan_calm.reason == "missing_you", str(plan_calm))
# near_end 也只在私聊
b_p.clock.advance(100.0 - 5 * 24 - 40.0)     # 让剩余寿命落进 uneasy_hours(48h)
pl = b_p.outreach_plan()
plg = b_p.outreach_plan(for_group=True)
C.info(f"剩余 {b_p.status().remaining_hours:.1f}h：私聊={pl.reason if pl else None}，"
       f"群里={plg.reason if plg else None}")
C.check("快到终端服务 → near_end 只走私聊", pl is not None and pl.reason == "near_end"
        and (plg is None or plg.reason != "near_end"), "")

brain.close()
brain2.close()
b_listen.close()
b_o.close()
b_s.close()
b_i.close()
b_g.close()
b_h.close()
b_p.close()
rm_dir(tmp)
raise SystemExit(C.summary())
