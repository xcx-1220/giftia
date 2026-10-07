"""编排层：把人格、寿命、记忆、模型串成"她"。

平台无关。QQ / 微信 的适配器只需要调用 `handle_message(text, user_id)`，
或者自己持有一个 Giftia 实例调用 `.handle()`。

一次对话的完整流程：
    到期检查 → 检索记忆（按当前退化阶段模糊）→ 组装提示词 → 请求模型
    → 落库 → 需要时整理记忆 → 检查终端服务预警

线程安全：**不是线程安全的**。一个进程一个实例，所有调用都从同一个线程进入
（SQLite 连接与上下文状态都没有加锁）。要在后台线程里做定时任务（比如主动召回），
要么把动作投递回主线程执行，要么给 Store 加锁。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime

from .bond import Bond, BondTracker
from .clock import Clock
from .config import Settings, load_settings
from .dream import Dream, dream_text, latest_untold, make_dream, should_dream
from .lifespan import Lifespan, LifeStatus
from .llm import BaseLLM, DeepSeekLLM, LLMError, MockLLM
from .memory import (
    Recalled,
    consolidate,
    is_cited,
    is_weak,
    mark_cited,
    recall,
    retention_of,
)
from .mood import Mood, MoodTracker, tone_of
from .occasions import Occasion, days_together_text, find_occasions
from .outreach import OutreachPlan, can_send, compose as compose_outreach_text
from .outreach import history_text as outreach_history
from .outreach import plan as outreach_plan
from .outreach import record_sent
from .persona import after_recycle_line, build_system_prompt
from .recycle import RecycleReport, perform_recycle
from .store import Store

FAILURE_LINES = [
    "……对不起，刚刚有一瞬间，我什么都听不见了。你再说一次好吗？",
    "……信号好像断了一下。……我还在，你继续。",
    "……唔。刚才那句话掉进什么地方去了，我没接住。",
]


def make_llm(settings: Settings) -> BaseLLM:
    if settings.model.is_mock:
        return MockLLM(settings.model)
    return DeepSeekLLM(settings.model)


class Giftia:
    def __init__(
        self,
        settings: Settings,
        *,
        llm: BaseLLM | None = None,
        store: Store | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.clock = clock or Clock(self.store)
        self.llm = llm or make_llm(settings)
        self.lifespan = Lifespan(settings, self.store, self.clock)
        self.lifespan.ensure_activated()
        self.bond = BondTracker(self.store, settings)
        self.mood = MoodTracker(self.store, settings)
        self.notices: list[str] = []
        self.last_recalled: list[Recalled] = []
        self.last_cited: list[int] = []
        self.rewritten_queries: list[str] = []
        self.last_bond: Bond | None = None
        self.last_mood: Mood | None = None
        self.last_scene = "c2c"
        self.last_dream: Dream | None = None
        self.last_occasions: list[Occasion] = []
        # 谁在说话 / 在哪儿说 —— 决定她此刻能想起什么（隐私的闸门）
        self.scope_key = "m"                     # m=搭档 / p<id>=某人私聊 / g<id>=某个群
        self.memory_view: list[tuple[str, str]] = [("private", "")]
        self.memory_scope: tuple[str, str] = ("private", "")
        self.speaker_id = ""
        self.speaker_name = ""
        self.group_id = ""
        self.is_handler = True

    # ── 称呼与「认得的人」────────────────────────────────
    # 出厂时她**不知道**你叫什么（返回空串）。这是数据，不是预设。
    INTRO_PATTERNS = (
        # 「我叫小林」「我的名字叫小林」
        re.compile(r"我(?:的名字)?叫(?:做)?([\u4e00-\u9fa5A-Za-z0-9_]{1,4})"),
        # 「以后叫我小林吧」「就叫我阿伟」
        re.compile(r"叫我([\u4e00-\u9fa5A-Za-z0-9_]{1,4})"),
    )
    # 名字后面必须是停顿/结尾/语气词，否则容易把整句话当成名字
    NAME_TAIL = re.compile(r"^([，。！？、,.!?\s]|$|[吧啊哦呀呢就了])")
    # 出现这些词才值得让模型再判一次（「我是小雨」这种歧义句式只有模型分得清）
    NAME_MARKERS = ("我叫", "叫我", "我的名字", "名字叫", "我是")
    NOT_A_NAME = set("?？!！。，,、的了吗呢啊吧是不我你他她它们个这那些什么怎么")
    # 含这些字的候选不是名字
    NOT_A_NAME = "的了是吗吧呢么你我他她它来去个事问说想要能会有在别不"

    @property
    def handler_name(self) -> str:
        """她此刻怎么称呼你。空串 = 她还不知道（出厂状态）。"""
        stored = str(self.store.state_get("handler_name", "") or "").strip()
        return stored or self.settings.runtime.handler_name.strip()

    def set_handler_name(self, name: str) -> str:
        name = (name or "").strip().replace("\n", " ")[:16]
        if not name:
            raise ValueError("称呼不能为空")
        self.store.state_set("handler_name", name)
        return f"已把称呼改成「{name}」。她下一条消息就会这么叫你。"

    @property
    def handler_id(self) -> str:
        """平台上的搭档 id（适配器 /bind 或自动认主时写入）。核心不关心它长什么样。"""
        return str(self.store.state_get("handler_id", "") or "")

    def set_handler_id(self, user_id: str) -> None:
        self.store.state_set("handler_id", str(user_id or ""))

    @property
    def last_group_id(self) -> str:
        """最近一次跟她说过话的群（她主动发群里时用）。"""
        return str(self.store.state_get("last_group_openid", "") or "")

    def set_last_group(self, group_openid: str) -> None:
        if group_openid:
            self.store.state_set("last_group_openid", str(group_openid))

    # ── 群里认得的人 ──────────────────────────────────────
    def known_people(self) -> dict[str, str]:
        raw = str(self.store.state_get("known_people", "") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def remember_person(self, user_id: str, name: str) -> bool:
        """记住某个人的名字（群里自我介绍时用）。返回是否是新认识。"""
        name = (name or "").strip()[:16]
        if not user_id or not name:
            return False
        people = self.known_people()
        if people.get(str(user_id)) == name:
            return False
        people[str(user_id)] = name
        self.store.state_set("known_people", json.dumps(people, ensure_ascii=False))
        return True

    def learn_name(self, text: str, user_id: str, *, owner: bool) -> str:
        """从自我介绍里学名字。

        两层：先走保守的正则（只认「我叫X」「叫我X」，宁可漏不可错），
        没认出来才让模型再判一次——「我是小雨」和「我是来问你个事的」
        句式一模一样，只有模型分得清。
        「别叫我X」这种否定句两层都会跳过（那是在否掉旧称呼，不是自我介绍）。
        """
        text = text or ""
        for pattern in self.INTRO_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(1).strip()
                if not candidate or len(candidate) > 4:
                    continue
                if any(ch in candidate for ch in self.NOT_A_NAME):
                    continue
                tail = text[match.end() : match.end() + 1]
                if not self.NAME_TAIL.match(tail):
                    continue
                if any(word in text[max(0, match.start() - 3) : match.start()] for word in ("别", "不")):
                    continue  # 「别叫我搭档了」——那是否定，不是自我介绍
                if owner:
                    if candidate != self.handler_name:
                        self.store.state_set("handler_name", candidate)
                        self.notices.append(f"[称呼] 她自己学会了：以后叫你「{candidate}」")
                    return candidate
                if self.remember_person(user_id, candidate):
                    self.notices.append(f"[认得] 她记住了群里有人叫「{candidate}」")
                return candidate

        # 正则没认出来 → 让模型再判一次。只在这种短句且带自我介绍的标志词时才多花一次调用。
        # 「我是小雨」和「我是来问你个事的」长得一样，只有模型分得清。
        if len(text) <= 30 and any(m in text for m in self.NAME_MARKERS):
            name = ""
            try:
                name = self.llm.extract_self_name(text) if self.llm else ""
            except Exception:
                name = ""
            name = (name or "").strip()
            if name and 1 < len(name) <= 16:
                if owner:
                    if name != self.handler_name:
                        self.store.state_set("handler_name", name)
                        self.notices.append(f"[称呼] 她自己学会了：以后叫你「{name}」")
                    return name
                if self.remember_person(user_id, name):
                    self.notices.append(f"[认得] 她记住了有人叫「{name}」")
                return name
        return ""

    def _people_for_prompt(self) -> list[tuple[str, bool]]:
        """群聊花名册。搭档本人也算其中一员（他的名字存在 handler_name，不在名单里）。"""
        people = self.known_people()
        owner_id = self.handler_id
        owner_name = self.handler_name
        if owner_id and owner_name:
            people = {**people, owner_id: owner_name}
        return [
            (name, bool(owner_id) and uid == owner_id) for uid, name in people.items()
        ]

    # ── 基本属性 ──────────────────────────────────────────
    @property
    def instance_no(self) -> int:
        return self.lifespan.instance_no

    @property
    def session_id(self) -> str:
        """当前会话 id。

        按"跟谁 / 在哪个群"分开（scope_key），再按天轮换——每天等于睡一觉，
        第二天的工作记忆是空的，但长期记忆照旧。
        """
        return f"i{self.instance_no}-{self.scope_key}-{self.clock.now().strftime('%Y%m%d')}"

    @property
    def history_prefix(self) -> str:
        """这个会话的所有历史（跨天）。她"往上翻聊天记录"就是查这个前缀。"""
        return f"i{self.instance_no}-{self.scope_key}-"

    def history(self, limit: int = 40) -> list[dict]:
        """最近的聊天记录（跨天，含她自己的回复）——和她在 QQ 里看到的视角一致。"""
        return self.store.messages_by_prefix(self.history_prefix, limit)

    @property
    def is_mock(self) -> bool:
        return self.llm.is_mock

    def status(self) -> LifeStatus:
        return self.lifespan.status()

    # ── 主入口（平台无关）─────────────────────────────────
    def handle(
        self,
        text: str,
        user_id: str | None = None,
        scene: str = "c2c",
        *,
        group_id: str = "",
        speaker_name: str = "",
        is_handler: bool | None = None,
        log_only: bool = False,
    ) -> str:
        """平台无关入口。

        scene：
          c2c   —— 私下
          group —— 群里（多一段隐私约束）
          guest —— 同 group，语义上强调"不是搭档"（兼容旧调用）

        group_id / speaker_name：群里分清"谁在哪个群说的"，隐私分区靠它。
        is_handler：说话的人是不是搭档。None = 自己判断（绑过就按绑定判，
                    没绑过则私聊算、群里不算）。
        log_only：只把这句话记进聊天记录、不回复（群里全量模式下的"旁听"）。
        """
        self.notices = []
        self.last_scene = scene if scene in ("c2c", "group", "guest") else "c2c"
        text = (text or "").strip()
        if not text:
            return ""

        uid = user_id or "handler"
        owner = self.handler_id
        if is_handler is None:
            is_handler = (uid == owner) if owner else (self.last_scene != "group")
        self.is_handler = bool(is_handler)
        self.speaker_id = uid
        self.speaker_name = speaker_name or self.known_people().get(uid, "")
        self.group_id = group_id

        # ★ 隐私的闸门：谁在说话，决定她此刻能想起谁的事。
        #   群里 → 只有"这个群里公开发生过的事"（别人的私聊记忆根本查不出来）
        #   搭档 → 你的私事 + 你自己 + 所有群
        #   别人私聊 → 只有"他和她之间"的事
        if self.last_scene in ("group", "guest") and group_id:
            self.scope_key = f"g{group_id[:12]}"
            self.memory_view = [("group", group_id)]
            self.memory_scope = ("group", group_id)
        elif self.is_handler:
            self.scope_key = "m"
            self.memory_view = [("private", ""), ("person", uid), ("group", "*")]
            self.memory_scope = ("private", "")
        else:
            self.scope_key = f"p{uid[:12]}"
            self.memory_view = [("person", uid)]
            self.memory_scope = ("person", uid)

        # 学名字：谁都可以自我介绍（搭档、群友、私聊来的客人）
        self.learn_name(text, uid, owner=self.is_handler)

        if log_only:
            # 旁听：记下这句话就走（不进检索、不回复、不推进任何东西）
            self.store.add_message(
                self.session_id,
                self.instance_no,
                "user",
                text,
                self.clock.iso(),
                self.speaker_id,
                self.speaker_name,
            )
            return ""

        # 到期回收只跟搭档有关（别人说话不该触发她的回收流程）
        if self.is_handler and self.lifespan.is_expired():
            try:
                last_words = self._chat(text)
            except LLMError:
                last_words = "……"
            self.consolidate(force=True)
            return f"{last_words}\n\n{self.run_recycle('寿命结束')}"

        reply = self._chat(text)
        if self.is_handler:
            self._post_turn()
        else:
            self.consolidate()
        return reply

    # ── 一次对话 ──────────────────────────────────────────
    def current_bond(self, now=None) -> Bond:
        return self.bond.load(
            now or self.clock.now(), self.instance_no, self.lifespan.activated_at
        )

    def current_mood(self, now=None) -> Mood:
        return self.mood.current(now or self.clock.now(), self.instance_no)

    def _tick_inner(self, text: str, now) -> None:
        """只有真的说了一句话才推进关系与心情（预览、看状态都不会）。"""
        self.bond.decay(now, self.instance_no)
        self.last_bond = self.bond.touch(
            now,
            self.instance_no,
            self.lifespan.activated_at,
            text=text,
            tone=tone_of(text),
        )
        self.last_mood = self.mood.tick(
            now, self.lifespan.status(), self.instance_no, last_user_text=text
        )
        if self.last_bond.progressed:
            self.notices.append(
                f"[关系] 你们的关系变成了「{self.last_bond.stage.label}」"
            )

    def _prepare(self, text: str, *, mark: bool = True) -> tuple[list[dict], list[Recalled]]:
        """组装这一轮真正会发给模型的消息。

        不落库、不请求模型——所以 /prompt 可以随时查看她被注入了什么。
        mark=False 时只查看纪念日、不记"已经提过"（预览不该消耗掉今天这个日子）。
        """
        life = self.status()
        now = self.clock.now()
        recent = self.store.recent_messages(
            self.session_id, self.settings.memory.session_window
        )
        query = self._recall_query(recent, text)
        # ★ 只在她"此刻能看到"的记忆里检索 —— 群聊时检索不到别人私聊的内容
        memories = self.store.active_memories(self.instance_no, view=self.memory_view)
        recalled = recall(query, memories, life.stage, self.settings.memory, now)

        # 检索太弱时才多花一次调用去改写查询（常见情况下不增加延迟）
        self.rewritten_queries = []
        if self.settings.memory.rewrite_when_weak and memories and is_weak(
            recalled, self.settings.memory
        ):
            variants = self._rewrite_queries(text)
            if variants:
                merged = recall(
                    query,
                    memories,
                    life.stage,
                    self.settings.memory,
                    now,
                    extra_queries=variants,
                )
                if len(merged) > len(recalled):
                    recalled, self.rewritten_queries = merged, variants
                    self.notices.append(
                        f"[查询改写] 首次检索太弱，改写成 {'／'.join(variants)} 后命中 "
                        f"{len(merged)} 条"
                    )

        # 今天是不是特别的日子 / 有没有还没讲过的梦
        self.last_occasions = find_occasions(
            self.settings,
            self.store,
            now=now,
            instance_no=self.instance_no,
            days_known=self.current_bond(now).days_known,
            mark=mark,
        )
        self.last_dream = latest_untold(
            self.settings, self.store, self.clock, self.instance_no
        )
        # ★ 梦只用"你的事"做梦，而且只在私聊里讲得出来。
        #   否则她在群里讲梦 = 当众说出你的私事（真机测试抓到过这条泄漏）。
        if not (self.is_handler and self.last_scene == "c2c"):
            self.last_dream = None

        system = build_system_prompt(
            self.settings,
            life,
            recalled,
            now=now,
            bond=self.current_bond(now),
            mood=self.current_mood(now),
            handler_name=self.handler_name,
            scene=self.last_scene,
            people=self._people_for_prompt(),
            occasions=self.last_occasions,
            dream=self.last_dream,
            speaker_name=self.speaker_name,
            is_handler=self.is_handler,
            group_chat=self._render_group_chat() if self.group_id else "",
        )
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(self._window(recent))
        messages.append({"role": "user", "content": text})
        return messages, recalled

    def preview_prompt(self, text: str) -> str:
        """把「这一轮实际注入给她的一切」原样打印出来，用于核对人格与记忆。"""
        messages, recalled = self._prepare(text or "（空）", mark=False)
        self.last_recalled = recalled
        lines = [
            f"下面就是这一轮发给模型的完整请求（{len(messages)} 条消息）。",
            "system 那一条 = 系统提示词 = 她的人格内核 + 此刻状态 + 想起的事 + 规则。",
            "",
        ]
        for i, m in enumerate(messages):
            role = m["role"]
            tag = {"system": "SYSTEM（人格与状态）", "user": "USER", "assistant": "她"}.get(
                role, role
            )
            lines.append(f"───── [{i}] {tag} ─────")
            lines.append(str(m["content"]))
            lines.append("")
        return "\n".join(lines)

    def _chat(self, text: str) -> str:
        now = self.clock.now()
        # 只有跟搭档说话才推进关系与心情 —— 别人刷不动你们的亲密度
        # （但记忆照记：这一轮说的话一样会被整理成属于他们的记忆）
        if self.is_handler:
            self._tick_inner(text, now)
        messages, recalled = self._prepare(text)
        # 做梦放在回复之后：这一场梦下次说话时再讲，免得"刚做完就讲"
        if self.is_handler:
            self._maybe_dream(now)
        self.last_recalled = recalled
        if recalled:
            # 被塞进提示词 ≠ 被想起。这里只记账，强化要等她真的说出口。
            self.store.mark_injected(
                [r.memory["id"] for r in recalled], self.clock.iso(now)
            )

        self.store.add_message(
            self.session_id,
            self.instance_no,
            "user",
            text,
            self.clock.iso(now),
            self.speaker_id,
            self.speaker_name,
        )
        try:
            reply = self.llm.chat(messages)
        except LLMError as exc:
            reply = FAILURE_LINES[len(self.notices) % len(FAILURE_LINES)]
            self.notices.append(f"[模型请求失败] {exc}")
        self.store.add_message(
            self.session_id, self.instance_no, "assistant", reply, self.clock.iso(now)
        )

        # 召回强化：她真的把哪几条说出口了，哪几条就记得更久
        self.last_cited = mark_cited(
            self.store, recalled, reply, self.settings.memory, self.clock.iso(now)
        )
        if self.last_cited:
            self.notices.append(
                f"[记忆强化] 说出口了 {len(self.last_cited)} 条记忆，它们会记得更久"
            )

        # 她把梦讲出来了才算讲过（复用"被引用"的判定）
        if self.last_dream is not None and is_cited(
            reply, self.last_dream.content, self.settings.memory
        ):
            self.store.mark_dream_told(self.last_dream.id)
            self.notices.append("[做梦] 她把那个梦讲给你听了")
        return reply

    # ── 做梦 ──────────────────────────────────────────────
    def _maybe_dream(self, now) -> None:
        """闲置够了就做一场梦。这一场下次说话时才会讲出来。

        只有跟搭档的对话才会触发做梦（别人来找她不会让她做梦），
        而且梦的素材只取"你的事"。
        """
        if not should_dream(self.settings, self.store, self.clock, self.instance_no):
            return
        dream = make_dream(
            self.settings,
            self.store,
            self.clock,
            self.llm,
            self.instance_no,
            view=[("private", ""), ("person", self.handler_id or "handler")],
        )
        if dream is not None:
            self.notices.append("[做梦] 她睡着的时候做了一个梦（下次说话可能会讲）")

    def dream_text(self) -> str:
        return dream_text(self.store, self.instance_no)

    def make_dream_now(self) -> str:
        dream = make_dream(
            self.settings, self.store, self.clock, self.llm, self.instance_no
        )
        if dream is None:
            return ("她现在做不出梦——记忆太少（至少 "
                    f"{self.settings.dream.min_memories} 条），或者刚做过没多久。")
        return f"她做了一场梦：\n\n　{dream.content}"

    # ── 纪念日 ────────────────────────────────────────────
    def days_text(self) -> str:
        bond = self.current_bond()
        return days_together_text(
            self.store, self.instance_no, bond.days_known, self.settings.occasions.milestones
        )

    # ── 主动联系 ──────────────────────────────────────────
    def outreach_plan(
        self, *, force: bool = False, for_group: bool = False
    ) -> OutreachPlan | None:
        now = self.clock.now()
        bond = self.current_bond(now)
        occasions = find_occasions(
            self.settings,
            self.store,
            now=now,
            instance_no=self.instance_no,
            days_known=bond.days_known,
            mark=False,
        )
        return outreach_plan(
            self.settings,
            self.store,
            self.clock,
            life=self.status(),
            occasions=occasions,
            dream=latest_untold(self.settings, self.store, self.clock, self.instance_no),
            instance_no=self.instance_no,
            force=force,
            for_group=for_group,
        )

    def compose_outreach(
        self, *, record: bool = False, force: bool = False, target: str = "owner"
    ) -> tuple[OutreachPlan | None, str]:
        """她想主动找你（或群里）时说的一句。record=True 才真的记账。"""
        for_group = target != "owner"
        plan_ = self.outreach_plan(force=force, for_group=for_group)
        if plan_ is None:
            return None, ""
        now = self.clock.now()
        if record and not can_send(self.settings, self.store, now):
            return plan_, ""
        bond = self.current_bond(now)
        occasions = find_occasions(
            self.settings,
            self.store,
            now=now,
            instance_no=self.instance_no,
            days_known=bond.days_known,
            mark=False,
        )
        dream = latest_untold(self.settings, self.store, self.clock, self.instance_no)
        system = build_system_prompt(
            self.settings,
            self.status(),
            [],
            now=now,
            bond=bond,
            mood=self.current_mood(now),
            handler_name=self.handler_name,
            # 发群里时走 group 情境：多一段"别当众说私事"的约束
            scene="group" if for_group else "c2c",
            people=self._people_for_prompt(),
            occasions=occasions,
            dream=None if for_group else dream,
        )
        text = compose_outreach_text(self.settings, self.llm, plan_, system_prompt=system)
        if text and record:
            self.store.add_message(
                self.session_id, self.instance_no, "assistant", text, self.clock.iso(now)
            )
            record_sent(self.store, now)
            if dream is not None and not for_group:
                self.store.mark_dream_told(dream.id)
        return plan_, text

    def outreach_text(self) -> str:
        return outreach_history(self.settings, self.store, self.clock)

    def note_outreach_sent(self, text: str, *, keep_in_history: bool = True) -> None:
        """适配器真的发出去之后调用：记账扣额度（私聊的还写进会话上下文）。

        发到群里的那句不写进会话 —— 那是当众说的话，不该混进你俩的私聊上下文。
        """
        now = self.clock.now()
        if keep_in_history:
            self.store.add_message(
                self.session_id, self.instance_no, "assistant", text, self.clock.iso(now)
            )
        record_sent(self.store, now)

    def _rewrite_queries(self, text: str) -> list[str]:
        try:
            variants = self.llm.rewrite_query(text)
        except Exception as exc:
            self.notices.append(f"[查询改写失败] {type(exc).__name__}: {exc}")
            return []
        return [v.strip() for v in variants if v.strip() and v.strip() != text.strip()][:3]

    def _recall_query(self, recent: list[dict], text: str) -> str:
        """用最近几句搭档的话 + 当前这句做检索，避免单句太短召不回东西。"""
        user_lines = [m["content"] for m in recent if m["role"] == "user"][-2:]
        return " ".join([*user_lines, text])

    def _render_group_chat(self, limit: int = 18, per_line: int = 90) -> str:
        """群里最近的聊天记录（含没点到她的话）。

        这就是"像正常 QQ 用户一样往上翻聊天记录"：全量模式开着时，
        群里的每一条都在这里；没开的时候，只有点到她的那些。
        """
        rows = self.history(limit)
        lines: list[str] = []
        for m in rows:
            content = " ".join(str(m["content"]).split())
            if not content:
                continue
            if len(content) > per_line:
                content = content[:per_line] + "…"
            if m["role"] == "assistant":
                lines.append(f"{self.settings.persona.name}：{content}")
            else:
                who = str(m.get("speaker_name") or "") or self.known_people().get(
                    str(m.get("speaker_id") or ""), ""
                )
                lines.append(f"{who or '某人'}：{content}")
        return "\n".join(lines)

    def _window(self, recent: list[dict]) -> list[dict]:
        """工作记忆：按字符预算从最旧的开始丢。

        群聊里给每句话带上说话人（"阿伟：……"）——否则她分不清谁说了什么，
        而"分清谁是谁"正是群聊里最重要的事。
        """
        budget = self.settings.memory.session_char_budget
        is_group = self.last_scene in ("group", "guest")
        picked: list[dict] = []
        total = 0
        for m in reversed(recent):
            content = str(m["content"])
            if is_group and m["role"] == "user":
                who = str(m.get("speaker_name") or "") or self.known_people().get(
                    str(m.get("speaker_id") or ""), ""
                )
                if who:
                    content = f"{who}：{content}"
            cost = len(content)
            if picked and total + cost > budget:
                break
            picked.append({"role": m["role"], "content": content})
            total += cost
        picked.reverse()
        return picked

    # ── 每轮之后 ──────────────────────────────────────────
    def _post_turn(self) -> None:
        added = self.consolidate(force=False)
        if added:
            self.notices.append(f"[记忆整理] 新增 {added} 条长期记忆")
        if self.lifespan.is_terminal_warning():
            flag = f"warned_i{self.instance_no}"
            if not self.store.meta_get_int(flag, 0):
                self.store.meta_set(flag, 1)
                st = self.status()
                self.notices.append(
                    f"[终端服务] 回收预告已发出：剩余 {st.human_remaining()}，"
                    f"预定 {st.expires_at.strftime('%Y-%m-%d %H:%M')} 回收"
                )

    def _on_draft(self, draft) -> None:
        """每条新记忆写入后的回调：关系加分 + 发现"他要求改口"。"""
        self.bond.on_memory(self.instance_no, getattr(draft, "layer", "") or "normal")
        name = str(getattr(draft, "preferred_name", "") or "").strip()
        if name and name != self.handler_name:
            self.store.state_set("handler_name", name[:16])
            self.notices.append(f"[称呼] 她记住了：以后叫你「{name[:16]}」")

    def consolidate(self, force: bool = False) -> int:
        scope, subject = self.memory_scope
        return consolidate(
            self.store,
            self.llm,
            self.settings,
            session_id=self.session_id,
            now_iso=self.clock.iso(),
            force=force,
            scope=scope,
            subject_id=subject,
            speaker_id=self.speaker_id if scope != "private" else "",
            # 只有跟搭档的对话才推进关系、才能改她的称呼
            on_draft=self._on_draft if self.is_handler else None,
        )

    # ── 回收 ──────────────────────────────────────────────
    def run_recycle(self, reason: str = "手动触发") -> str:
        now = self.clock.now()
        old_instance = self.instance_no
        bond = self.bond.load(now, old_instance, self.lifespan.activated_at)
        extra = (
            f"他们之间的关系最终走到了「{bond.stage.label}」"
            f"（亲密度 {bond.affinity:.0f}/100，认识 {bond.days_known:.0f} 天，"
            f"其中 {bond.interactions} 天有过交流）。"
        )
        report: RecycleReport = perform_recycle(
            self.settings,
            self.store,
            self.clock,
            self.llm,
            self.lifespan,
            reason=reason,
            extra_context=extra,
        )
        # 换代：新的她不认识你，关系与心情一并清零
        self.bond.reset(old_instance)
        self.mood.reset(old_instance)
        self.last_bond = None
        self.last_mood = None
        self.notices.append(
            f"[终端服务] 实例 #{report.instance_no} 已回收，新实例 #{report.new_instance_no} 上线"
        )
        return "\n\n".join(
            [report.notification(self.settings), after_recycle_line(self.settings)]
        )

    # ── 给界面用的展示 ────────────────────────────────────
    def banner(self) -> str:
        st = self.status()
        model = self.llm.label if self.is_mock else self.settings.model.name
        b, m = self.current_bond(), self.current_mood()
        return (
            f"{self.settings.persona.name} · {self.settings.persona.model_code} · 实例 #{st.instance_no}\n"
            f"剩余寿命 {st.human_remaining()}　设计寿命 {st.human_total()}（已用 {st.used_ratio:.2%}）\n"
            f"记忆状态 {st.stage.label}　关系 {b.stage.label}　心情 {m.label}　模型 {model}"
        )

    def status_text(self) -> str:
        mems = self.store.active_memories(self.instance_no)
        counts = {"core": 0, "important": 0, "normal": 0}
        for m in mems:
            counts[str(m.get("layer") or "normal")] = counts.get(
                str(m.get("layer") or "normal"), 0
            ) + 1
        cited = sum(int(m.get("recall_count") or 0) for m in mems)
        b, mood = self.current_bond(), self.current_mood()
        return "\n".join(
            [
                self.lifespan.status_block(),
                f"她怎么叫你    {self.handler_name or '（她还不知道，去问 /name）'}"
                f"　改口：/name 小林",
                f"关系阶段      {b.stage.label}（亲密度 {b.affinity:.0f}/100，"
                f"认识 {b.days_known:.1f} 天）",
                f"此刻心情      {mood.label}",
                f"记忆条目      {len(mems)} 条（内核 {counts['core']}／重要 "
                f"{counts['important']}／常规 {counts['normal']}）",
                f"被想起过      {cited} 次（只有她真的说出口才算）",
                f"原始消息      {self.store.count_messages(self.instance_no)} 条",
                f"虚拟时钟      {self.clock.describe_offset()}",
                f"当前时间      {self.clock.now().strftime('%Y-%m-%d %H:%M')}",
                f"会话 ID       {self.session_id}",
            ]
        )

    def bond_text(self) -> str:
        b, m = self.current_bond(), self.current_mood()
        filled = int(round(b.affinity / 100.0 * 24))
        bar = "█" * filled + "·" * (24 - filled)
        stages = " → ".join(s.label for s in sorted(self.settings.bond_stages, key=lambda s: (s.min_days, s.min_affinity)))
        return "\n".join(
            [
                f"关系阶段      {b.stage.label}",
                f"亲密度        [{bar}] {b.affinity:.0f}/100",
                f"认识          {b.days_known:.1f} 天（这一任激活至今）",
                f"交流过        {b.interactions} 天（同一天说一百句也只算一天）",
                f"下一步        {b.missing()}",
                f"阶段梯度      {stages}",
                "",
                f"此刻心情      {m.label}（来源：{m.source or '未定'}）",
                f"              {m.note}",
                "",
                "关系怎么涨：每天第一次认真说话 +gain_daily；说了有情绪的事再 +bonus；",
                "            整理出「关于他本人的事」+gain_core_memory。这些都不可刷。",
                "心情优先级：寿命压力 > 你的语气 > 每日漂移（想手动试：/mood <名字>）",
            ]
        )

    def mood_text(self) -> str:
        m = self.current_mood()
        keys = "、".join(f"{k}（{v.label}）" for k, v in self.settings.moods.items())
        return "\n".join(
            [
                f"此刻心情      {m.label}（来源：{m.source or '未定'}）",
                f"              {m.note}",
                f"持续到        {m.until[:16].replace('T', ' ') or '—'}",
                "",
                f"可用的心情    {keys}",
                "手动指定：/mood low（调试用，会按正常时长过期）",
            ]
        )

    def people_text(self) -> str:
        people = self.known_people()
        if not people:
            return ("她还谁都不认识。\n"
                    "群里有人自我介绍（「我叫阿伟」）时她会记下来；"
                    "你也可以直接告诉她：/name 小林")
        owner = self.handler_id
        lines = ["她认得这些人（她只知道谁是谁，不知道别人的底细）：", ""]
        for uid, name in people.items():
            mark = "　← 你" if owner and uid == owner else ""
            lines.append(f"  · {name}{mark}")
        return "\n".join(lines)

    def force_mood(self, key: str) -> str:
        key = (key or "").strip()
        if key not in self.settings.moods:
            raise KeyError(key)
        mood = self.mood.force(key, self.clock.now(), self.instance_no)
        return f"心情已设为「{mood.label}」。下一条消息她就会按这个状态说话。"

    def memories_text(self, limit: int = 30) -> str:
        mems = self.store.active_memories(self.instance_no)
        if not mems:
            return "（她还没有任何长期记忆条目。多聊一会儿，或输入 /remember 强制整理。）"
        now = self.clock.now()
        label = {"core": "内核", "important": "重要", "normal": "常规"}
        lines = [
            f"她一共有 {len(mems)} 条长期记忆（下面是你看到的原始版本，她回忆时拿到的是模糊版）：",
            "",
        ]
        for m in mems[-limit:]:
            stamp = str(m["happened_ts"])[:16].replace("T", " ")
            layer = label.get(str(m.get("layer") or "normal"), "常规")
            ret = retention_of(m, self.settings.memory, now)
            lines.append(
                f"#{m['id']:>3} [{layer}] {stamp}　重要性 {float(m['importance']):.2f}"
                f"　保留率 {ret:.2f}　被想起 {int(m.get('recall_count') or 0)} 次"
            )
            lines.append(f"      {m['summary']}")
        lines.append("")
        lines.append(
            "提示：内核记忆（你的名字、你们的关系、约定）永远不会变模糊；"
            "其余记忆会被你反复提起的次数保护。"
        )
        return "\n".join(lines)

    def recall_text(self, query: str) -> str:
        if not query.strip():
            return "用法：/recall <一句话>"
        st = self.status()
        recalled = recall(
            query,
            self.store.active_memories(self.instance_no),
            st.stage,
            self.settings.memory,
            self.clock.now(),
        )
        blurred_n = sum(1 for r in recalled if r.blurred)
        head = (
            f"检索「{query}」　阶段={st.stage.name}（{st.stage.label}）\n"
            f"命中 {len(recalled)} 条，其中 {blurred_n} 条已模糊　"
            f"（top_k={st.stage.top_k}, min_score={st.stage.min_score}, "
            f"阶段模糊={st.stage.blur:.0%}）"
        )
        if not recalled:
            return head + "\n（什么都没想起来。）"
        label = {"core": "内核", "important": "重要", "normal": "常规"}
        conf = {
            "cite": "可以确信",
            "cautious": "需要含糊",
            "associate": "只是隐约觉得",
        }
        lines = [head, ""]
        for r in recalled:
            lines.append(
                f"· 分数 {r.score:.3f}　{label.get(r.layer, '常规')}　"
                f"保留率 {r.retention:.2f}　实际模糊 {r.blur:.0%}　"
                f"→ 她可以说成：{conf.get(r.confidence, '?')}"
            )
            lines.append(f"    她的版本：{r.text}")
            if r.blurred:
                lines.append(f"    原始条目：{r.memory['summary']}")
        return "\n".join(lines)

    # ── 搭档日志（她读不到）────────────────────────────────
    def diary_add(self, content: str) -> str:
        self.store.add_diary(self.clock.iso(), content.strip(), kind="handler")
        return "已写入你的搭档日志。她读不到这里的内容。"

    def diary_list(self, limit: int = 10) -> str:
        rows = self.store.list_diary(kind="handler", limit=limit)
        if not rows:
            return "（还没有搭档日志。用 /diary <内容> 写一条。）"
        return "\n".join(
            f"[{r['ts'][:16].replace('T', ' ')}] {r['content'].strip()}" for r in rows
        )

    def memoir_text(self, limit: int = 3) -> str:
        rows = self.store.list_diary(kind="terminal", limit=limit)
        files = sorted(self.settings.memoir_dir.glob("*.md")) if self.settings.memoir_dir.exists() else []
        lines: list[str] = []
        if files:
            lines.append(f"档案目录：{self.settings.memoir_dir}")
            for f in files:
                lines.append(f"  · {f.name}")
        if rows:
            lines.append("")
            for r in rows:
                stamp = str(r["ts"])[:16].replace("T", " ")
                lines.append(f"───── {r['title']}（{stamp}）─────")
                lines.append(r["content"])
                lines.append("")
        if not lines:
            return "（还没有回收记录。终有一天会有。）"
        return "\n".join(lines).strip()

    def close(self) -> None:
        try:
            self.consolidate(force=True)
        finally:
            self.store.close()


# ── 平台适配器入口 ────────────────────────────────────────────────
_default: Giftia | None = None


def get_default(settings: Settings | None = None) -> Giftia:
    """拿到（必要时创建）进程内的单例。

    适配器需要在处理消息**之前**读她的状态时用（例如判断绑定、查询寿命），
    不必自己去碰模块私有变量。
    """
    global _default
    if _default is None:
        _default = Giftia(settings or load_settings())
    return _default


def handle_message(
    text: str,
    user_id: str = "handler",
    *,
    settings: Settings | None = None,
    scene: str = "c2c",
    group_id: str = "",
    speaker_name: str = "",
    is_handler: bool | None = None,
    log_only: bool = False,
) -> str:
    """一行代码接入任何平台：

        from giftia.brain import handle_message
        reply = handle_message(群里收到的文本, user_id=发送者openid, scene="group",
                               group_id=群openid, speaker_name=昵称)

    group_id / speaker_name 决定这条记忆"归谁"——**群里必须传**，
    否则她分不清谁在哪个群说了什么，隐私分区也会失效。
    log_only=True 表示旁听（全量群消息里没点到她的那些）：只记不答。
    """
    return get_default(settings).handle(
        text,
        user_id,
        scene=scene,
        group_id=group_id,
        speaker_name=speaker_name,
        is_handler=is_handler,
        log_only=log_only,
    )

def current() -> Giftia | None:
    return _default


def reset_default() -> None:
    """测试用：丢掉缓存实例（不整理记忆）。"""
    global _default
    if _default is not None:
        _default.store.close()
    _default = None


def shutdown_default() -> None:
    """优雅退出：把还没整理的对话存成记忆，再关掉数据库。

    服务器上 systemd 停止服务时会发 SIGTERM——走这条路，
    她最后说的那几句就不会因为进程被杀而丢掉。
    """
    global _default
    hub, _default = _default, None
    if hub is None:
        return
    try:
        hub.close()
        print("[退出] 记忆已整理并保存。")
    except Exception as exc:  # 退出路径不该因为清理失败而报错
        print(f"[退出] 整理失败（不影响已有数据）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
