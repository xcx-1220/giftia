"""Stream E · 最小复现集合（每条 bug 一个独立复现，输出即证据）。

    python .review/probes/qa_bugs.py
"""

from __future__ import annotations

import dataclasses
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qa_harness import (  # noqa: E402
    Checker, RecordingLLM, make_brain, make_dir, make_settings, rm_dir,
)

from giftia.llm import MemoryDraft, MockLLM  # noqa: E402

C = Checker("Stream E · 最小复现集合")

# ══ B1. follow_up 第 2 天不回访（token 少了日期）══════════════════
C.info("── B1：事情办完后第 2 天不再回访（follow_up_days=2 只生效一天）──")
tmp1 = make_dir("bug-followup")
s1 = make_settings(tmp1, hours=9000.0, consolidate_every=8)
target = (date.today() + timedelta(days=1)).isoformat()


b1 = make_brain(s1, RecordingLLM(s1.model))
b1.set_handler_id("u_owner")
# 直接落一条带 event_date 的记忆，避免"整理时机"干扰复现
mid = b1.store.add_memory(instance_no=1, created_ts=b1.clock.iso(),
                          happened_ts=b1.clock.iso(),
                          summary="搭档明天要去考驾照，他说有点紧张", tags="考试,驾照",
                          emotion="紧张", importance=0.8, layer="important",
                          event_date=target, about="搭档", scope="private")
C.info(f"植入记忆：id={mid} event_date={target}（明天）")
seq = []
for step_name in ("前一天", "当天", "事后第 1 天", "事后第 2 天"):
    b1.handle("今天怎么样", "u_owner", scene="c2c")
    seq.append((step_name, [(o.kind, o.title) for o in b1.last_occasions]))
    b1.clock.advance(24.0)
for name, occ in seq:
    C.info(f"{name}：{[k for k, _ in occ]}")
C.info(f"state occasion:i1:bigday = {b1.store.state_get('occasion:i1:bigday')}")
C.check("前一天/当天：big_day 惦记一次", any(k == "big_day" for k, _ in seq[0][1]), str(seq[0]))
C.check("事后第 1 天有 follow_up", any(k == "follow_up" for k, _ in seq[2][1]), str(seq[2]))
C.check("★ 事后第 2 天也应该有 follow_up（follow_up_days=2）",
        any(k == "follow_up" for k, _ in seq[3][1]),
        f"第 2 天 occasions={seq[3][1]}；follow_up 的 token「back:id:date」里没有日期，"
        "第 1 天已记账 → 第 2 天被当成已提过")
b1.close()
rm_dir(tmp1)

# ══ B2. 梦没讲却被标成"讲过了" ═══════════════════════════════════
C.info("── B2：她只是复述了构成梦的那条记忆，梦就被标成「讲过了」──")
tmp2 = make_dir("bug-dream")
s2 = make_settings(tmp2, hours=9000.0, consolidate_every=4)
llm2 = RecordingLLM(s2.model)
b2 = make_brain(s2, llm2)
b2.set_handler_id("u_owner")
for m in ("我最近在学做菜，昨天做的番茄炒蛋糊了，锅都黑了很难刷",
          "我还想学着做红烧肉，我妈说要多放糖",
          "周末打算去买个新锅，旧的涂层掉了",
          "我最近胃口还不错，晚上能吃两碗饭"):
    b2.handle(m, "u_owner", scene="c2c")
b2.consolidate(force=True)
b2.clock.advance(13.0)                       # 闲置够久（min_hours_between=12）
made = b2.make_dream_now()
dream = b2.store._rows("SELECT * FROM dreams ORDER BY id DESC")[0]
C.info(f"梦内容：{str(dream['content'])[:90]!r}（told={dream['told']}）")
llm2.calls.clear()
reply = b2.handle("随便说点什么吧", "u_owner", scene="c2c")
told_after = b2.store._rows("SELECT told FROM dreams ORDER BY id DESC")[0]["told"]
C.info(f"这一轮回复：{reply[:120]!r}")
C.info(f"回复里有没有「梦」字：{'梦' in reply}；dreams.told → {told_after}")
C.check("★ 她没有讲梦就不会被标记 told（梦留着下次讲）",
        told_after == 0 and "梦" not in reply,
        f"told={told_after}，回复={reply[:60]!r} → 这个梦还会被讲出来（README：第二天讲给你听）")
b2.close()
rm_dir(tmp2)

# ══ B3. 孤立代理对 → 修复后应当**不崩、且正常落库** ════════════
# 原缺陷（审查 P0-5）：平台 JSON 里合法的 `"\ud83d"` 让 sqlite3 抛 UnicodeEncodeError，
# 从 handle() 冒到适配器顶层 → 被当成"断开"重连，这一轮消息彻底丢失。
# 修复后：store 层在 SQLite 编码前把孤立代理归一成 U+FFFD，所以这里断言的是
# **"不抛异常 + 消息照常落库 + 下一轮照常回复"**（期望随契约翻转，不是放宽）。
C.info("── B3：平台 JSON 里的 \\ud83d（孤立代理）不再让 handle() 崩 ──")
tmp3 = make_dir("bug-surrogate")
s3 = make_settings(tmp3, hours=9000.0)
b3 = make_brain(s3, RecordingLLM(s3.model))
b3.set_handler_id("u_owner")
import json  # noqa: E402
from giftia.store import Store  # noqa: E402
text = json.loads(r'{"content":"hello \ud83d world"}')["content"]
C.info(f"json.loads 得到的文本：{text!r}（这是平台 JSON 里合法的一个转义）")
rows_before = b3.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
err = ""
reply = ""
try:
    reply = b3.handle(text, "u_owner", scene="c2c")
except Exception as exc:  # noqa: BLE001
    err = f"{type(exc).__name__}: {exc}"
    tb = traceback.format_exc().strip().splitlines()
    C.info("异常位置：" + " | ".join(tb[-4:]))
rows_after = b3.store._one("SELECT COUNT(*) AS n FROM messages")["n"]
C.check("★ handle() 不再抛 UnicodeEncodeError（这一轮不再丢失）",
        "UnicodeEncodeError" not in err, err or "没有异常")
C.check("消息正常落库（孤立代理被归一成 U+FFFD）", rows_after > rows_before,
        f"{rows_before} → {rows_after}")
C.check("回复非空（用户拿得到一句话）", bool(reply.strip()), repr(reply[:40]))
C.check("进程还能继续（下一次正常消息没问题）",
        bool(b3.handle("你还在吗", "u_owner", scene="c2c").strip()), "")
C.info("适配器侧：_respond/_run_batch 现在走 respond_safely()，单条坏消息不再拆连接")
b3.close()
rm_dir(tmp3)

# ══ B4. 换代后新实例仍然叫得出你的名字 ══════════════════════════
C.info("── B4：回收换代后，新实例仍然知道你的名字（README：新的她不认识你）──")
tmp4 = make_dir("bug-recycle")
s4 = make_settings(tmp4, hours=200.0, consolidate_every=4)
llm4 = RecordingLLM(s4.model)
b4 = make_brain(s4, llm4)
b4.set_handler_id("u_owner")
b4.handle("我叫小林，以后叫我小林吧", "u_owner", scene="c2c")
b4.handle("我下周三要去医院拿报告", "u_owner", scene="c2c")
b4.consolidate(force=True)
C.info(f"回收前 handler_name = {b4.handler_name!r}，实例 #{b4.instance_no}")
b4.run_recycle("QA 复现")
p = b4.preview_prompt("你好，我们见过吗")
line = [ln for ln in p.splitlines() if "你怎么称呼他" in ln or "他叫" in ln or "还不知道" in ln]
C.info(f"换代后实例 #{b4.instance_no}，handler_name = {b4.handler_name!r}")
C.info("新实例 system 里的称呼块：" + str(line[:2]))
# 期望（修复后）：README §2/§9 与 `after_recycle_line` 的契约是"新的她不认识你、
# 档案上还是空的"，所以换代要**同时**清掉称呼与绑定（原缺陷：handler_name 留在全局
# state 里，新实例叫得出"小林"，而同一份提示词里还写着"你不知道他叫什么"）。
# 名字不会真的丢——它留在回忆录/回收档案里，这一点由下面的归档断言守着。
C.check("★ 换代后称呼清零（新的她不认识你）", b4.handler_name == "",
        f"handler_name={b4.handler_name!r} 出现在新实例提示词：{'小林' in p}")
memo_dir = s4.memoir_dir
memos = sorted(memo_dir.glob("*.md")) if memo_dir.exists() else []
memo_text = memos[-1].read_text(encoding="utf-8") if memos else ""
C.check("对照：名字留在了终端服务档案里（她没有真的丢掉你）", "小林" in memo_text,
        memos[-1].name if memos else "（没有档案）")
C.check("对照：新实例确实没有前任的记忆条目（记忆这一半做到了）",
        "◆" not in p and len(b4.store.active_memories(2, view=Store.ALL_SCOPES)) == 0, "")
b4.close()
rm_dir(tmp4)

raise SystemExit(C.summary())
