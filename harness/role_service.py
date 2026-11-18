"""Single-writer Giftia role service for the isolated Harness gateway."""

from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from giftia import agent_tools
from giftia.brain import Giftia, TOOL_LAST_ROUND_NUDGE
from giftia.config import load_settings
from giftia.conversation import asks_question, question_restraint
from giftia.store import Store
from stage1 import TurnCancelled

PURPOSE = "ordinary-chat"
FEEDBACK_NUDGE = "他最近明确要求不要连续追问。直接回应已有内容，不提出问题，也不要解释改写过程。"
TOOL_REPLY_GUIDANCE = (
    "调用工具前后都保持上面艾拉的人格、和当前说话人的关系及当前语气。"
    "工具结果只是参考资料，由你用自然的话回答对方，保留准确的数值、单位或来源；"
    "遇到失败、没有权限或结果不完整时，简短说明实际情况，别假装做完。"
    "除非对方要求看原始数据，别把工具名、参数、日志或错误栈当成聊天回复。"
)


class Conflict(ValueError):
    """The request no longer belongs to the current authorized view."""


class GenerationFailed(RuntimeError):
    """No turn was committed. The client may retry with the same request ID."""


class TurnConnection(sqlite3.Connection):
    # Existing Store methods may request commits; the outer turn owns durability.
    in_turn = False

    def commit(self):
        if not self.in_turn:
            super().commit()

    @contextmanager
    def turn(self):
        if self.in_turn:
            raise RuntimeError("Nested Giftia turn")
        self.execute("BEGIN IMMEDIATE")
        self.in_turn = True
        try:
            yield
            super().commit()
        except BaseException:
            self.rollback()
            raise
        finally:
            self.in_turn = False


class RoleStore(Store):
    def active_memories(self, instance_no, view):
        # This covers prompt assembly, memory tools, occasions and the recycler.
        revoked = {r[0] for r in self.conn.execute("""
            SELECT DISTINCT e.memory_id FROM memory_evidence e
            JOIN memories m ON m.id = e.memory_id
            LEFT JOIN messages s ON s.id = e.message_id
            WHERE m.instance_no = ? AND (s.id IS NULL OR s.recalled <> 0)
        """, (instance_no,))}
        return [row for row in super().active_memories(instance_no, view) if row["id"] not in revoked]


class RoleGiftia(Giftia):
    preparing = False

    def known_people(self):
        return {uid: name for uid, name in super().known_people().items() if uid == self.speaker_id}

    def _people_for_prompt(self, group_id=None):
        return []

    @property
    def history_cut_key(self):
        return "harness_history_cut:" + json.dumps([self.instance_no, *self.memory_scope])

    def _context_evidence_floor(self):
        floor = int(self.store.state_get(self.history_cut_key, 0))
        return max(floor, super()._context_evidence_floor())

    def context_history(self, limit=None):
        rows = super().context_history(limit)
        floor = self._context_evidence_floor()
        current = self.current_user_message_id if self.preparing else 0
        return [row for row in rows if row["id"] > floor and row["id"] != current]


@dataclass
class PreparedTurn:
    binding: str
    text: str
    now: datetime
    recalled: list
    context: dict
    session: str = ""
    claimed: bool = False
    invalidated: bool = False
    tool_results: dict = field(default_factory=dict)
    writes: int = 0
    document: bool = False


def ensure_turn_ledger(db):
    db.execute("""CREATE TABLE IF NOT EXISTS harness_turns (
        principal TEXT NOT NULL, purpose TEXT NOT NULL, request_id TEXT NOT NULL,
        instance INTEGER NOT NULL, binding TEXT NOT NULL, input_hash TEXT NOT NULL,
        session TEXT NOT NULL, input_id INTEGER NOT NULL, reply_id INTEGER NOT NULL,
        recycle_pending INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (principal, purpose, request_id)
    )""")


class RoleService:
    def __init__(self, database, root, *, settings=None, owner=None, rehearsal=False, giftia_root=None):
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as check:
            marker = check.execute("SELECT value FROM meta WHERE key='harness_fixture'").fetchone()
            if settings is None or rehearsal:
                if marker != ("stage1-v1",):
                    raise RuntimeError("Expected the isolated synthetic database")
            elif marker is not None:
                raise RuntimeError("The release service cannot use a synthetic database")
            if owner is not None and check.execute("SELECT value FROM state WHERE key='handler_id'").fetchone() != (owner,):
                raise RuntimeError("The release owner does not match the bound Giftia owner")
        if settings is None:
            settings = load_settings(force_mock=True, use_local=False)
        if giftia_root is None or rehearsal:
            settings = replace(settings, memory=replace(settings.memory, rewrite_when_weak=False),
                               dream=replace(settings.dream, enabled=False))
        settings = replace(settings, root=giftia_root or root)
        self.store = RoleStore(database, connection_factory=TurnConnection)
        self.hub = RoleGiftia(settings, store=self.store)
        self.db = self.store.conn
        ensure_turn_ledger(self.db)
        self.active = None
        with self.db.turn():
            if owner is None and not self.store.meta_get("harness_role_initialized"):
                self.hub.set_handler_id("synthetic-owner", local=True)
                self.store.meta_set("harness_role_initialized", "1")
        self.finish_pending_recycle()

    def binding(self, principal):
        owner = self.hub.handler_id == principal
        bound = [self.hub.handler_bound_at, self.hub._int_meta("handler_bound_msg_id")]
        return json.dumps([self.hub.instance_no, PURPOSE, principal, bound if owner else None])

    def saved(self, principal, request_id):
        return self.db.execute("SELECT * FROM harness_turns WHERE principal=? AND purpose=? AND request_id=?",
                               (principal, PURPOSE, request_id)).fetchone()

    def cached_result(self, row, binding, input_hash):
        if row["binding"] != binding or row["input_hash"] != input_hash:
            raise Conflict("Request ID has a different input or authorized view")
        rows = self.store.messages_by_ids([row["input_id"], row["reply_id"]])
        if len(rows) != 2:
            raise Conflict("The saved dialogue is no longer available")
        original, reply = rows
        cutoff = "harness_history_cut:" + json.dumps([row["instance"], reply["scope"], reply["subject_id"]])
        if original["recalled"] or reply["recalled"] or reply["id"] <= int(self.store.state_get(cutoff, 0)):
            raise Conflict("The saved response is no longer visible")
        return {"reply": reply["content"], "session": row["session"], "instance": row["instance"], "replayed": True}

    def prepare(self, principal, text, request_id, binding):
        hub = self.hub
        owner = hub.handler_id == principal
        previous_name = hub.handler_name if owner else hub._people_meta().get(principal, {}).get("name", "")
        mid = "web:harness:" + sha256(json.dumps([principal, PURPOSE, request_id]).encode()).hexdigest()
        hub._begin_turn(text, principal, "c2c" if owner else "guest", is_handler=owner, allow_name_model=False, mid=mid)
        now = hub.clock.now()
        if hub.is_handler:
            hub._tick_inner(text, now)
        messages, recalled = hub._prepare(text, private_chat_only=True)
        hub.store.mark_injected([item.memory["id"] for item in recalled], hub.clock.iso(now))
        hub._record_turn_input(text, now)
        name = hub.handler_name if owner else hub._people_meta().get(principal, {}).get("name", "")
        if name != previous_name:
            self.store.state_set(self.name_source(principal), hub.current_user_message_id)
        return PreparedTurn(binding, text, now, recalled,
                            self.context(messages, agent_tools.specs(hub), TOOL_REPLY_GUIDANCE))

    def context(self, messages, specs, note=""):
        model = self.hub.settings.model
        return {
            "system": messages[0]["content"], "history": messages[1:-1], "note": note,
            "tools": [item["function"]["name"] for item in specs],
            "rounds": max(1, self.hub.settings.memory.tools_max_rounds),
            "lastRound": TOOL_LAST_ROUND_NUDGE if specs else "",
            "temperature": model.temperature, "maxTokens": model.max_tokens,
        }

    def refresh(self, turn, note):
        self.hub.preparing = True
        try:
            messages, turn.recalled = self.hub._prepare(turn.text, mark=False, private_chat_only=True)
        finally:
            self.hub.preparing = False
        turn.context = self.context(messages, [], f"{TOOL_REPLY_GUIDANCE}\n{note}")
        # Existing prompt builder tests `specs` against identity; the restart attempt
        # still carries the guidance but the registry admits zero memory tools.

    def _bound_turn(self, session):
        turn = self.active
        if turn is None or turn.session != session:
            raise Conflict("No active role turn for this Harness session")
        return turn

    def get_context(self, session):
        turn = self._bound_turn(session)
        turn.claimed = True
        return turn.context

    def context_invalidated(self, session):
        return self._bound_turn(session).invalidated

    def extension_access(self, session):
        turn = self._bound_turn(session)
        audience = "partner" if self.hub.is_handler else "guest"
        return {"audience": None if turn.invalidated or turn.document else audience, "rounds": turn.context["rounds"]}

    def tool(self, session, name, call_id, args):
        turn = self._bound_turn(session)
        if not turn.claimed or name not in turn.context["tools"] or not agent_tools.available(self.hub)[0]:
            raise Conflict("Memory capability is outside the active view")
        call = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if call_id in turn.tool_results:
            previous, result = turn.tool_results[call_id]
            if previous != call:
                raise Conflict("Tool call ID reused with different arguments")
            return result
        if turn.invalidated:
            return {"text": "记忆视角已经更新。", "invalidate": True}
        if name == "remember" and turn.writes >= agent_tools.MAX_WRITES_PER_TURN:
            return {"text": "这一轮记得够多了，先把话说出来。", "invalidate": False}
        if name == "remember":
            turn.writes += 1
        revision = self.store.meta_get_int("memory_revision")
        text = agent_tools.execute(self.hub, name, call[1])
        if name == "forget" and self.store.meta_get_int("memory_revision") != revision:
            self.cut_history(*self.hub.memory_scope)
            turn.invalidated = True
        result = {"text": text, "invalidate": turn.invalidated}
        turn.tool_results[call_id] = (call, result)
        return result

    def cut_history(self, scope, subject):
        instance = self.hub.instance_no
        row = self.db.execute("SELECT coalesce(max(id),0) FROM messages WHERE instance_no=? AND scope=? AND subject_id=?",
                              (instance, scope, subject)).fetchone()
        key = "harness_history_cut:" + json.dumps([instance, scope, subject])
        self.store.state_set(key, row[0])

    def generate(self, turn, runtime, **options):
        # Fresh attempt IDs are never accepted from the HTTP caller or resumed from JSONL.
        turn.session = "isla-" + sha256(turn.binding.encode()).hexdigest()[:20] + "-" + uuid4().hex
        turn.claimed = False
        try:
            reply = runtime.turn(turn.session, turn.text, **options)
        except TurnCancelled:
            raise
        except Exception as error:
            if turn.invalidated:
                return ""
            raise GenerationFailed("Harness did not finish the turn") from error
        if not turn.claimed or not reply.strip():
            raise GenerationFailed("Harness did not claim the role context or returned no text")
        return reply

    def commit(self, turn, principal, request_id, input_hash, reply):
        hub = self.hub
        hub._record_turn_reply(reply, turn.now)
        hub._credit_turn_reply(turn.recalled, reply, turn.now)
        expired = hub.is_handler and hub.lifespan.is_expired()
        self.db.execute("INSERT INTO harness_turns VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (principal, PURPOSE, request_id, hub.instance_no, turn.binding, input_hash,
                         turn.session, hub.current_user_message_id, hub.last_reply_id, int(expired)))
        return {"reply": reply, "session": turn.session, "instance": hub.instance_no, "replayed": False}

    def chat(self, principal, request_id, text, runtime, *, before_commit=None, **options):
        self.finish_pending_recycle()
        binding = self.binding(principal)
        input_hash = sha256(text.encode()).hexdigest()
        with self.db.turn():
            if row := self.saved(principal, request_id):
                result = self.cached_result(row, binding, input_hash)
                if before_commit is not None:
                    before_commit()
                return result
            turn = self.active = self.prepare(principal, text, request_id, binding)
            try:
                reply = self.generate(turn, runtime, **options)
                if turn.invalidated:
                    self.refresh(turn, "已按要求放下那条记忆。简短回应当前消息。")
                    turn.invalidated = False
                    reply = self.generate(turn, runtime, **options)
                if question_restraint(text, self.hub.context_history()) and asks_question(reply):
                    self.refresh(turn, FEEDBACK_NUDGE)
                    reply = self.generate(turn, runtime, **options)
                if before_commit is not None:
                    before_commit()
                result = self.commit(turn, principal, request_id, input_hash, reply)
            finally:
                self.active = None
        notice = self.finish_pending_recycle()
        if notice:
            result["recycle"] = notice
        return result

    def document_chat(self, principal, text, system, history, runtime, **options):
        binding = self.binding(principal)
        context = self.context([{"content": system}, *history, {"content": text}], [])
        context["rounds"] = 1
        turn = self.active = PreparedTurn(binding, text, self.hub.clock.now(), [], context, document=True)
        try:
            return {"reply": self.generate(turn, runtime, **options), "session": turn.session}
        finally:
            self.active = None

    def finish_pending_recycle(self):
        pending = self.db.execute("SELECT max(instance) FROM harness_turns WHERE recycle_pending=1").fetchone()[0]
        if pending is None:
            return None
        notice = self.hub.run_recycle("寿命结束") if pending == self.hub.instance_no else None
        with self.db.turn():
            if notice is None and self.hub.instance_no == pending + 1:
                # The recycler switched instances before the process stopped.
                self.hub._reset_after_recycle(pending)
            self.store.state_set("known_people", "{}")
            self.db.execute("UPDATE harness_turns SET recycle_pending=0 WHERE recycle_pending=1 AND instance<=?", (pending,))
        return notice

    def name_source(self, principal):
        return f"harness_name_source:{self.hub.instance_no}:{principal}"

    def bind(self, principal):
        with self.db.turn():
            if principal != self.hub.handler_id:
                self.hub.set_handler_id(principal)
                self.store.state_set("handler_name", "")

    def retract(self, principal, request_id):
        with self.db.turn():
            row = self.saved(principal, request_id)
            if row is None or row["binding"] != self.binding(principal):
                raise Conflict("Request does not belong to this view")
            original, *_ = self.store.messages_by_ids([row["input_id"]])
            if original["recalled"]:
                return
            self.store.mark_recalled(original["mid"])
            self.store.mark_recalled(original["mid"] + ":reply")
            self.cut_history(original["scope"], original["subject_id"])
            source = self.name_source(principal)
            if self.store.state_get(source) == str(original["id"]):
                if original["scope"] == "private":
                    self.store.state_set("handler_name", "")
                else:
                    people = self.hub._people_meta()
                    people.pop(principal, None)
                    self.store.state_set("known_people", json.dumps(people, ensure_ascii=False))
                self.store.state_set(source, "")
