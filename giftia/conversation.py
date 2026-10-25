"""Small, evidenced conversation state. All reads/writes remain on the caller thread."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

LIMITS = {"topic": 2, "open_loop": 3, "interaction_feedback": 6, "preference": 6, "self_interest": 2}
TTL = {"topic": 72, "open_loop": 24 * 14, "self_interest": 24 * 14}
LABELS = {"topic": "最近在聊", "open_loop": "还没结束的事", "interaction_feedback": "相处时留意",
          "preference": "他说过的偏好", "self_interest": "你自己还在关注"}
QUESTION_FEEDBACK = r'别.*(?:一直问|追问|问细节)|查户口|不要.*追问'
QUESTION_PERMISSION = r'可以问我|你问吧|问我也行'


def question_restraint(text: str, recent: list[dict]) -> bool:
    for line in reversed([m['content'] for m in recent if m['role']=='user'][-3:] + [text]):
        if re.search(QUESTION_PERMISSION, line):
            return False
        if re.search(QUESTION_FEEDBACK, line):
            return True
    return False


def explicit_feedback(text):
    if re.search(QUESTION_PERMISSION,text):
        return 'questions','用户允许正常提问','dismissed'
    if re.search(QUESTION_FEEDBACK,text):
        return 'questions','用户不希望被连续追问；先回应已有信息，不把交流变成盘问。','open'
    if re.search(r'像在演|动作太多|偏头转杯|直接.*说话',text):
        return 'actions','用户希望直接说话，减少动作旁白；自然调整，不为习惯辩解。','open'
    return None


def asks_question(text):
    return bool(re.search(r'[？?]|(?:吗|么|什么|怎么|哪一个)[。！…\s]*$',text))


def reply_focus(text: str, recent: list[dict]) -> str:
    """Only evidence-based turn guidance; no canned replies and no extra model call."""
    notes = []
    if re.search(QUESTION_FEEDBACK, text):
        notes.append('他明确不想被连续提问。本轮接受这条反馈，回应已有内容，不换个问题接着问。')
    elif re.search(r'像在演|动作太多|偏头转杯|直接.*说话', text):
        notes.append('这是对交流方式的反馈。直接接受并调整，不辩解你到底有没有演，也不追加新问题。')
    elif re.search(r'不是.{1,24}[，,、]|是指|你.*理解错|没接住|记错了', text):
        notes.append('他在澄清意思。按他说的更新理解，不争论自己有没有说错，不添加他没确认的安排。')
    elif re.search(r'拜拜|再见|先去忙|开始干活|开始工作|我得去', text):
        notes.append('他在结束这一段或去做事。自然回应即可，不开启新话题、不询问行程，不把工作假定为出门。')
    elif re.search(r'不用.*(?:教|劝)|别.*(?:劝|教)|不想.*计划|就想.*说说|在这儿就行', text):
        notes.append('他此刻希望被听见。回应具体感受，不安排计划、诊断情绪或提出替代方案。')
    else:
        recent_replies = [m['content'] for m in recent if m['role'] == 'assistant'][-3:]
        questions = sum(bool(re.search(r'[？?]|吗[。！…\s]*$|什么|怎么|哪[个里种]|是不是|要不要', s)) for s in recent_replies)
        if questions >= 2:
            notes.append('最近你已连续向他提问。本轮先表达自己的具体反应或看法，不再采集新细节。')
        elif not re.search(r'[？?]|帮我|你觉得|你.*(?:喜欢|想|会)|怎么|为什么|要不要', text):
            notes.append('他在分享一件事。先对其中一个具体内容作出自己的反应，不必自动追问。')
    if question_restraint(text,recent):
        notes.append('他最近明确要求少问。先让这几轮有自己的回应，不追加疑问句，除非他重新明确允许提问。')
    notes.append('历史里你自己的提问、猜测和建议不等于他确认的事实；不要把假设变成他的行程或经历。')
    return '\n'.join(notes)


def behavior_key(text: str) -> str:
    for key, pattern in [("farewell", r"拜拜|道别|告别|再见"), ("questions", r"追问|一直问|反复问|查户口"),
                         ("actions", r"动作|括号|旁白"), ("naming", r"称呼|叫我|别叫")]:
        if re.search(pattern, text):
            return key
    return re.sub(r"\W", "", text)[:24]


def is_performance(text: str) -> bool:
    """Narrow historical-pattern detector; never erase the original event."""
    return bool(re.search(r"(?:艾拉|她|Giftia).{0,40}(?:答得很快|特意.{0,12}(?:快|道别)|问.{0,12}够不够快)", text))


class Conversation:
    def __init__(self, store, instance: int, scope: str, subject: str, epoch: str = ""):
        self.store = store
        self.instance = instance
        self.scope = scope
        self.subject = subject
        self.key = "conversation:" + json.dumps([instance, scope, subject, epoch], ensure_ascii=False)

    def items(self, now: datetime, floor: int = 0) -> list[dict]:
        try:
            items = json.loads(self.store.state_get(self.key, "[]") or "[]")
        except (ValueError, TypeError):
            return []
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("status") != "open":
                continue
            try:
                if item.get("expires") and datetime.fromisoformat(item["expires"]) <= now:
                    continue
            except (ValueError, TypeError):
                continue
            ids = item.get("source_ids", [])
            # A retracted/deleted source can no longer justify state.
            evidence = self.store.messages_by_ids(ids)
            if not evidence or any(int(m["id"]) <= floor or m.get("recalled") or
                                   int(m["instance_no"]) != self.instance or
                                   m.get("scope") != self.scope or m.get("subject_id", "") != self.subject
                                   for m in evidence) or len(evidence) != len(set(ids)):
                continue
            forgotten = self.store.conn.execute(
                "SELECT 1 FROM memory_evidence e JOIN memories m ON m.id=e.memory_id "
                "WHERE e.message_id IN (" + ",".join("?" * len(ids)) + ") AND m.forgotten=1 LIMIT 1", ids
            ).fetchone()
            if forgotten:
                continue
            out.append(item)
        return out

    def update(self, draft, pending: list[dict], now: datetime, *, commit: bool = True):
        kind = getattr(draft, "kind", "event")
        if kind not in LIMITS:
            return
        sources = {int(m["id"]): m for m in pending}
        ids = [i for i in getattr(draft, "source_ids", []) if i in sources]
        if not ids or (kind in ("interaction_feedback", "preference") and
                       not any(sources[i]["role"] == "user" for i in ids)):
            return
        key = (getattr(draft, "behavior_key", "") or behavior_key(draft.summary))[:40]
        current = self.items(now)
        previous = next((x for x in current if x["kind"] == kind and x["key"] == key), None)
        current = [x for x in current if not (x["kind"] == kind and x["key"] == key)]
        if getattr(draft, "status", "open") == "open":
            item = {"kind": kind, "key": key, "text": draft.summary[:180], "source_ids": ids,
                    "status": "open", "updated": now.isoformat(), "expires": ""}
            if kind in TTL:
                item["expires"] = (now + timedelta(hours=TTL[kind])).isoformat()
            # Identical feedback doesn't become a fresh event every time it is repeated.
            current.append(item if previous is None or previous["text"] != item["text"] else previous)
        bounded = []
        for category, limit in LIMITS.items():
            bounded.extend([x for x in current if x["kind"] == category][-limit:])
        self.store.state_set(self.key, json.dumps(bounded, ensure_ascii=False), commit=commit)

    def render(self, now: datetime, floor: int = 0, stage=None, for_extraction=False) -> str:
        from .persona import _one_line
        items = self.items(now, floor)
        if stage is not None and stage.blur > 0:
            from .memory import blur_text
            import zlib
            items = [dict(x) for x in items if stage.blur < 0.6 or x['kind'] in ('preference', 'interaction_feedback')]
            for item in items:
                if item['kind'] not in ('preference', 'interaction_feedback'):
                    item['text'] = blur_text(item['text'], stage.blur,
                                             zlib.crc32(item['key'].encode()))[0]
        return "\n".join(f"{LABELS[x['kind']]}" + (f" [behavior_key={_one_line(x['key'])}]" if for_extraction else '')
                         + f"：{_one_line(x['text'])}" for x in items)
