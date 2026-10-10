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
from . import agent_tools
from .dream import Dream, dream_text, latest_untold, make_dream, should_dream
from .lifespan import Lifespan, LifeStatus
from .llm import BaseLLM, DeepSeekLLM, LLMError, MockLLM
from .memory import (
    day_label,
    Recalled,
    consolidate,
    flush_sessions,
    is_cited,
    is_weak,
    mark_cited,
    recall,
    reconcile,
    retention_of,
)
from .mood import Mood, MoodTracker, tone_of
from .occasions import Occasion, days_together_text, find_occasions
from .outreach import OutreachPlan, can_send, compose as compose_outreach_text
from .outreach import history_text as outreach_history
from .outreach import plan as outreach_plan
from .outreach import record_sent
from .persona import ROSTER_MAX, after_recycle_line, build_system_prompt
from .recycle import RecycleReport, perform_recycle
from .store import Store

FAILURE_LINES = [
    "……对不起，刚刚有一瞬间，我什么都听不见了。你再说一次好吗？",
    "……信号好像断了一下。……我还在，你继续。",
    "……唔。刚才那句话掉进什么地方去了，我没接住。",
]

# 本地/单机调用方的默认 uid（`giftia/cli.py`、`run.py --once`、
# `handle_message()` 的默认值都是它）。它**不是**平台身份（QQ 的 openid 是 32 位十六进制），
# 所以核心可以据此判断"调用方就是本机的搭档本人"——详见 _auto_bind / set_handler_id。
LOCAL_USER_ID = "handler"

# 花名册里"最近活跃"这个时间戳多久刷新一次。每条群消息都重写一遍 state 没必要，
# 一小时粒度足够排序用，也避免每条消息多一次写库。
PERSON_LAST_REFRESH_MINUTES = 60.0

# 旁听攒下来的会话（P1-14）：值 = 会话 id → 最后一次旁听的时间。
LISTEN_PENDING_KEY = "listen_pending"

# 工具轮次用满前的最后一句系统提醒：**同时还会拿掉工具**（见 _reply_with_tools）。
# 光靠提醒不足以保证她说得出话——她可能又去调工具，用户就永远等不到回复。
TOOL_LAST_ROUND_NUDGE = (
    "（不要再调用任何工具了。现在直接用一句话回答对方——"
    "你刚才想到的、答应的事，用你自己的语气说出来。）"
)


def _gap_hours(a: str, b: str) -> float:
    """两条消息隔了几个小时。解析不了就当 0（宁可不标，也别标错）。"""
    try:
        first = datetime.fromisoformat(a[:19])
        second = datetime.fromisoformat(b[:19])
    except (ValueError, TypeError):
        return 0.0
    return abs((second - first).total_seconds()) / 3600.0


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
        # 老数据的 scope 是默认值，会让群里的话被当成"你的私事"——启动时归位一次。
        # P1-5：这是一次**隐私归位迁移**，失败不能静默吞掉（否则群里的话一直被当成私事），
        # 也不能留下"已完成"标记——要留着下次启动重试。
        init_warnings: list[str] = []
        try:
            self.store.backfill_message_scopes()
        except Exception as exc:
            self._reset_backfill_markers()
            detail = f"{type(exc).__name__}: {exc}"
            init_warnings.append(
                f"[隐私迁移] 历史消息归属回填失败：{detail}（下次启动会重试）"
            )
            try:
                self.store.add_diary(
                    self.clock.iso(),
                    f"历史消息归属回填失败：{detail}\n"
                    "这些老消息的归属还是默认值（可能把群里的话当成你的私事）。"
                    "已清掉完成标记，下次启动会重试。",
                    kind="terminal",
                    title="隐私迁移失败",
                )
            except Exception:
                pass  # 连日记都写不进去（库都坏了）：至少 init_warnings 里有
        self.bond = BondTracker(self.store, settings)
        self.mood = MoodTracker(self.store, settings)
        self.notices: list[str] = list(init_warnings)
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
        # 上一条回复写进了哪一行 —— 适配器回报"到底送出去没有"时要改它
        self.last_reply_id: int | None = None
        self.last_reply_text = ""
        self.current_mid = ""
        # 她这一轮自己调工具写下的记忆（用于提示与测试）
        self.tool_writes: list[str] = []
        # 最近一次"她想主动说什么"的计划（心情分享要靠它去重）
        self.last_outreach_plan = None

    # ── 留痕与善后 ────────────────────────────────────────
    def _note_error(self, where: str, exc: BaseException) -> None:
        """异常留痕：`notices` 给人看，`diary` 给排障看。**自己绝不抛**。

        适配器那一层最怕的是"异常冒穿 → 断连重连"，而模糊的日志又排不了障，
        所以两条路都写：格式固定成 `[内部错误] 哪里：类型: 消息`。
        """
        detail = f"{type(exc).__name__}: {exc}"
        self.notices.append(f"[内部错误] {where}：{detail}")
        try:
            self.store.add_diary(
                self.clock.iso(),
                f"{where}出错了：{detail}",
                kind="terminal",
                title="内部错误",
            )
        except Exception:
            pass  # 日记都写不进去就只能算了，但绝不能因此再抛

    def _reset_backfill_markers(self) -> None:
        """把"回填已完成"的标记清掉，让下次启动重试（P1-5）。

        标记由 `Store.backfill_message_scopes` 自己写（v1/v2 两个键）；
        失败时如果它已经把标记落下了，不清就等于永久放弃这次隐私迁移。
        """
        for key in ("msg_scope_backfilled", "msg_scope_backfill_v2"):
            try:
                if self.store.meta_get_int(key, 0):
                    self.store.meta_set(key, 0)
            except Exception:
                pass

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

    @property
    def handler_bound_at(self) -> str:
        """"这一次绑定"的时刻——新搭档看不到早于它的 private 记忆（P0-3b）。

        空串 = 没有历史私事需要遮蔽（第一次绑定就是全新的一任）。
        见 `_memory_visible` / `visible_memories`：遮蔽只作用在 private 归属上，
        同一任搭档自己说的话照旧全在。
        """
        return str(self.store.meta_get("handler_bound_at", "") or "")

    def set_handler_id(self, user_id: str, *, local: bool = False) -> None:
        """绑定 / 解绑搭档。

        P0-3b（契约 §二）：**每一次换人**都重记一次"绑定时刻"：新搭档是"从这一刻开始"
        跟她有过去的，所以绑定之前留下的 private 记忆对他们不存在——前任的私事不会换主人。
        下界只往前推、不往回退（隐私优先：后来的绑定不许把它"洗掉"）。

        - `local=True`：调用方就是本机的搭档本人（本地 CLI 不传 user_id，见 LOCAL_USER_ID）。
          他看的是自己留下的记忆，所以不设下界；但也不会清掉已有的下界。
        - 同一个人 `/unbind` 之后再 `/bind` 回来（`meta['handler_prev_id']` 对得上）：
          保留原来那一道下界——他记得的是自己的过去，不该被自己的解绑清空。
        - 除了时间戳还记两条 **id 水位线**：这个库的时间戳只有秒级精度，
          "前任刚说完就 /unbind、新人立刻说话"会压在同一秒里，只比时间戳分不开。
          id 是单调递增的，`id <= 水位线` = "绑定之前就写下的"，精确。
        """
        uid = str(user_id or "")
        old = self.handler_id
        if uid and uid == old:
            return  # 同一任重复绑定：不动遮蔽下界
        if not uid:
            # /unbind：记下"刚才这一任是谁"，好让下一任判断是"同一个人回来"还是"换人"
            if old:
                self.store.meta_set("handler_prev_id", old)
            self.store.state_set("handler_id", "")
            return
        self.store.state_set("handler_id", uid)
        if local:
            return  # 本机调用方 = 搭档本人：不遮
        if not self._has_private_history():
            return  # 全新的一任（库里没有历史私事）：没有要遮的东西
        same_person = uid == str(self.store.meta_get("handler_prev_id", "") or "")
        if same_person:
            return  # 同一个人回来：他记得自己的过去，解绑不该清空它
        self.store.meta_set("handler_bound_at", self.clock.iso())
        self.store.meta_set("handler_bound_mem_id", self._max_private_memory_id())
        self.store.meta_set("handler_bound_msg_id", self._max_message_id())

    def _int_meta(self, key: str) -> int:
        try:
            return int(self.store.meta_get_int(key, 0) or 0)
        except Exception:
            return 0

    def _max_private_memory_id(self) -> int:
        try:
            rows = self.store.active_memories(self.instance_no, view=[("private", "")])
        except Exception:
            return 0
        return max((int(m.get("id") or 0) for m in rows), default=0)

    def _max_message_id(self) -> int:
        try:
            rows = self.store.all_messages(self.instance_no, limit=1)
        except Exception:
            return 0
        return max((int(m.get("id") or 0) for m in rows), default=0)

    def _has_private_history(self) -> bool:
        """库里还有"前任留下的"private 记忆吗（换人时决定要不要设遮蔽下界）。"""
        try:
            return bool(
                self.store.active_memories(self.instance_no, view=[("private", "")])
            )
        except Exception:
            return True  # 查不出来就按"有"处理：宁可多遮，不要漏

    def _private_floor(self) -> str:
        return self.handler_bound_at

    def _memory_visible(self, mem: dict) -> bool:
        """P0-3b：private 记忆按"绑定时刻"遮蔽，别的一律放行。

        两条判据，先精确后兜底：

        1. **id 水位线**（`handler_bound_mem_id`）：id 是插入时单调分配的，
           `id <= 水位线` = "这条记忆是绑定之前写下的"——与秒级时间戳无关。
        2. **时间戳**（`created_ts`，落库时刻；没有才退回 `happened_ts`）**严格晚于**
           绑定时刻才可见。`happened_ts` 的语义是"事情发生在故事里的什么时候"
           （`consolidate` 取的是批内最早那条消息的时间），可以早于或晚于落库时刻，
           拿它跟绑定下界比会出现"同秒相等"甚至"未来时间"的误判。
           水位线缺失时（老库、查询失败）这条是兜底。

        另外：绑定**之后**写下的记忆，如果它说的是绑定**之前**的事
        （延迟整理前任留下的话，`happened_ts < 下界`），也一并藏着。
        """
        floor = self._private_floor()
        if not floor:
            return True
        if str(mem.get("scope") or "") != "private":
            return True  # 群里的公开的事、别人私聊里的事各有各的视角闸门
        watermark = self._int_meta("handler_bound_mem_id")
        if watermark:
            if int(mem.get("id") or 0) <= watermark:
                return False  # 绑定之前就写下的：藏着
            happened = str(mem.get("happened_ts") or "")
            return not (happened and happened < floor)
        written = str(mem.get("created_ts") or mem.get("happened_ts") or "")
        return written > floor

    def visible_memories(self, view=None) -> list[dict]:
        """她**此刻真的能想起来**的记忆：视角闸门（view）+ 绑定时刻遮蔽（P0-3b）。

        view 默认用当前视角 `self.memory_view`。检索、印象、工具、"她自己查一下"
        都走这一条路——绕过它就等于绕过了遮蔽。
        """
        rows = self.store.active_memories(
            self.instance_no, view=self.memory_view if view is None else view
        )
        return [m for m in rows if self._memory_visible(m)]

    def _visible_by_person(self, view=None) -> dict[str, list[dict]]:
        """按"关于谁"分组，且同样过一遍绑定时刻遮蔽（给印象块 / /profile 用）。"""
        out: dict[str, list[dict]] = {}
        for mem in self.visible_memories(view):
            about = str(mem.get("about") or "").strip()
            if about:
                out.setdefault(about, []).append(mem)
        return out

    @property
    def last_group_id(self) -> str:
        """最近一次跟她说过话的群（她主动发群里时用）。"""
        return str(self.store.state_get("last_group_openid", "") or "")

    def set_last_group(self, group_openid: str) -> None:
        if group_openid:
            self.store.state_set("last_group_openid", str(group_openid))

    # ── 群里认得的人 ──────────────────────────────────────
    def _people_meta(self) -> dict[str, dict]:
        """花名册的完整结构：uid → {"name", "groups", "last", "n"}。

        `groups` = 在哪个群见过他（P1-12 之前不存在，只有名字）。
        兼容老格式：值直接是名字字符串 → 名字取它，群留空。
        """
        raw = str(self.store.state_get("known_people", "") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict] = {}
        for uid, value in data.items():
            if isinstance(value, dict):
                name = str(value.get("name") or "").strip()
                raw_groups = value.get("groups") or []
                groups = [str(g) for g in raw_groups if str(g)] if isinstance(raw_groups, list) else []
                last = str(value.get("last") or "")
                try:
                    n = int(value.get("n") or 0)
                except (TypeError, ValueError):
                    n = 0
            else:
                name, groups, last, n = str(value).strip(), [], "", 0
            if name:
                out[str(uid)] = {"name": name, "groups": groups, "last": last, "n": n}
        return out

    def known_people(self) -> dict[str, str]:
        """uid → 名字（老接口，值就是名字）。

        想知道"在哪个群见过"用 `_people_meta()`；注入提示词用 `_people_for_prompt()`。
        """
        return {uid: str(info.get("name") or "") for uid, info in self._people_meta().items()}

    def people_stats(self) -> dict[str, dict]:
        """活跃成员表：openid → {"n": 说过几句, "last": 最后说话时间}。

        花名册只记名字是不够的——群里谁常来、谁好久没露面，
        她说话的分寸不一样（"阿伟最近天天在" vs "好久没见小雨了"）。
        """
        raw = str(self.store.state_get("people_stats", "") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def note_activity(self, user_id: str) -> None:
        if not user_id:
            return
        stats = self.people_stats()
        row = stats.get(str(user_id)) or {"n": 0, "last": ""}
        row["n"] = int(row.get("n") or 0) + 1
        row["last"] = self.clock.iso()
        stats[str(user_id)] = row
        self.store.state_set("people_stats", json.dumps(stats, ensure_ascii=False))

    def remember_person(self, user_id: str, name: str, *, group_id: str = "") -> bool:
        """记住某个人的名字（群里自我介绍时用）。返回是否真的学到了新东西。

        P1-12：同时记下"在哪个群见过"（`groups`），并且只在名字/群变化、
        或者"最近活跃"这个时间戳过了一小时才写库——不用每条消息都读改写一遍。
        """
        name = (name or "").strip()[:16]
        if not user_id or not name:
            return False
        uid = str(user_id)
        gid = str(group_id or "")
        people = self._people_meta()
        row = people.get(uid)
        if row is None:
            row = {"name": name, "groups": [], "last": "", "n": 0}
        new_name = row.get("name") != name
        new_group = bool(gid) and gid not in row.get("groups", [])
        stale = True
        last = str(row.get("last") or "")
        if last:
            try:
                age_min = (self.clock.now() - datetime.fromisoformat(last)).total_seconds() / 60.0
                stale = age_min >= PERSON_LAST_REFRESH_MINUTES
            except (TypeError, ValueError):
                stale = True
        if not (new_name or new_group or stale):
            return False
        row["name"] = name
        if new_group:
            row["groups"] = [*row.get("groups", []), gid][-8:]  # 一个人待过的群也封个顶
        row["n"] = int(row.get("n") or 0) + 1
        row["last"] = self.clock.iso()
        people[uid] = row
        self.store.state_set("known_people", json.dumps(people, ensure_ascii=False))
        return new_name or new_group

    def learn_name(
        self,
        text: str,
        user_id: str,
        *,
        owner: bool,
        allow_llm: bool = True,
        group_id: str = "",
    ) -> str:
        """从自我介绍里学名字。

        两层：先走保守的正则（只认「我叫X」「叫我X」，宁可漏不可错），
        没认出来才让模型再判一次——「我是小雨」和「我是来问你个事的」
        句式一模一样，只有模型分得清。
        「别叫我X」这种否定句两层都会跳过（那是在否掉旧称呼，不是自我介绍）。

        allow_llm=False：**只走正则**。旁听（log_only）走的就是这条路——
        "没命中的消息零 token"必须字面成立（P1-14），不该为一句
        「我是来看热闹的」多花一次模型调用。
        """
        text = text or ""
        gid = group_id or self.group_id
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
                if self.remember_person(user_id, candidate, group_id=gid):
                    self.notices.append(f"[认得] 她记住了群里有人叫「{candidate}」")
                return candidate

        # 正则没认出来 → 让模型再判一次。只在这种短句且带自我介绍的标志词时才多花一次调用。
        # 「我是小雨」和「我是来问你个事的」长得一样，只有模型分得清。
        if allow_llm and len(text) <= 30 and any(m in text for m in self.NAME_MARKERS):
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
                if self.remember_person(user_id, name, group_id=gid):
                    self.notices.append(f"[认得] 她记住了有人叫「{name}」")
                return name
        return ""

    def _present_names(self, text: str) -> set[str]:
        """本轮"在场"的人：说话的这位、搭档本人、这条消息里点到的人，以及泛指。"""
        names = {"", "群里", "大家", "搭档", "群友"}
        if self.speaker_name:
            names.add(self.speaker_name)
        if self.handler_name:
            names.add(self.handler_name)
        for uid, name in self.known_people().items():
            display = self.store.person_note(uid) or name
            if display and display in text:
                names.add(display)
                names.add(name)
        return names

    def _filter_by_presence(self, recalled: list[Recalled], text: str) -> list[Recalled]:
        """群聊里只留下"关于在场的人"的记忆。

        专治"把 A 的事说给 B 听"：记忆里明明有 `about` 字段，但检索时没有用它。
        私聊不设这道闸门——你本来就有权知道所有人的事。
        """
        if self.last_scene not in ("group", "guest"):
            return recalled
        present = self._present_names(text)
        # 这条消息里点到的名字也要算在场——他可能还没自我介绍，
        # 所以花名册里没有，但记忆的 about 里有（"小雨最近怎么样"）。
        for r in recalled:
            about = str(r.memory.get("about") or "").strip()
            if about and about in text:
                present.add(about)
        return [
            r for r in recalled
            if str(r.memory.get("about") or "").strip() in present
        ]

    def impressions_block(
        self,
        text: str = "",
        *,
        per_person: int = 3,
        max_people: int = 15,
        line_chars: int = 44,
        present_only: bool = False,
    ) -> str:
        """【你对他们的印象】——每人一份档案的紧凑版，随提示词注入。

        和"检索式的记忆"不同：这一块**不靠 query 命中**，而是按人各带几条。
        这样她不会因为这次没问到就把人忘干净，也天然封顶（每人 3 条、最多 15 人），
        不会把上下文吃光。

        `present_only=True`（**注入路径**，见 `_prepare`）时只带本轮真的出现过的人：
        说话的这位、搭档、这条消息里点到的人。P1-13 之前这里有个"兜底循环"，
        会把 `by_person` 里剩下的人全部补进来——群 G 里只有一个人在说话，
        提示词里却带了另外 14 个人的印象，正好绕过了 `_filter_by_presence` 要防的事。
        默认的 False 是"看整份档案"的语义（`/impressions`、调试），仍然按人封顶。
        """
        view = self.memory_view
        by_person = self._visible_by_person(view)
        if not by_person:
            return ""
        present: list[tuple[str, list[dict]]] = []
        # 在场的人优先（说话的这位、搭档、这条消息里点到的人）
        names = self._present_names(text) if present_only else None
        for name, mems in by_person.items():
            if names is None or name in names or name in (
                self.speaker_name, self.handler_name, "搭档", "群里",
            ):
                present.append((name, mems))
        lines: list[str] = []
        for name, mems in present[:max_people]:
            picked = sorted(
                mems, key=lambda m: -float(m.get("importance") or 0.5)
            )[:per_person]
            notes = []
            for m in picked:
                text = " ".join(str(m.get("summary", "")).split())
                notes.append(text[:line_chars] + ("…" if len(text) > line_chars else ""))
            lines.append(f"- {name}：" + "；".join(notes))
        if not lines:
            return ""
        # 还没形成印象的人：明说"你还不了解他"，免得她硬编
        known = {name for name, _ in present[:max_people]}
        unknown = [
            n for n in (self.speaker_name, self.handler_name)
            if n and n not in known and n not in by_person
        ]
        tail = ""
        if unknown:
            tail = (
                "\n（" + "、".join(dict.fromkeys(unknown))
                + " 你还没有印象——你还不了解他，别装作很熟。）"
            )
        return (
            "你在心里给每个人留了一份印象（这不是台词，是你的底子）：\n"
            + "\n".join(lines)
            + "\n（提到谁的时候照着这份印象说话；没有印象的人，说明你还不了解他。）"
            + tail
        )

    def profile_text(self, name: str = "") -> str:
        """给 `/profile` 用：她对某个人（或所有人）的印象全貌。

        用**搭档视角**（私聊 + 你自己 + 所有群）——这是你看她的记忆，
        不是在群里当众念。

        契约 §三.4：`/profile` 是搭档专属。没有搭档时（`/unbind` 之后、还没认主）
        这里**一个字都不输出**：指令闸门在适配器那一层，核心这一层也要防住——
        否则"未绑定时群成员一条 /profile 就能把档案打进群"还会从别的入口复用。
        另外这一份同样过绑定时刻遮蔽（P0-3b）：换人之后新搭档读不到前任的私事。
        """
        if not self.handler_id:
            return "（她还没有搭档——先 /bind 认个亲，才有档案可看。）"
        owner_view = [
            ("private", ""),
            ("person", self.handler_id or "handler"),
            ("group", "*"),
        ]
        by_person = self._visible_by_person(owner_view)
        if not by_person:
            return "她还没对谁形成印象（多聊几次就有了）。"
        if name:
            hit = next((k for k in by_person if name in k), "")
            if not hit:
                return f"她没有关于「{name}」的印象。认得的人：{'、'.join(by_person)}"
            by_person = {hit: by_person[hit]}
        lines: list[str] = []
        for person, mems in by_person.items():
            lines.append(f"── {person}（{len(mems)} 条）──")
            for m in sorted(mems, key=lambda x: -float(x.get("importance") or 0.5)):
                layer = {"core": "内核", "important": "重要"}.get(str(m.get("layer")), "日常")
                lines.append(f"  #{m['id']}　[{layer}]　{m['summary']}")
            lines.append("")
        lines.append("（记错了可以让她忘掉：/forget <编号>；想补一条：/impress <名字> <内容>）")
        return "\n".join(lines).strip()

    def add_impression(self, name: str, content: str) -> int:
        """手动给她补一条关于某人的印象。"""
        name = (name or "").strip()[:16]
        content = (content or "").strip()
        if not name or not content:
            return 0
        scope, subject = self.memory_scope
        return self.store.add_memory(
            instance_no=self.instance_no,
            created_ts=self.clock.iso(),
            happened_ts=self.clock.iso(),
            summary=content[:200],
            tags="手动,印象",
            emotion="平静",
            importance=0.8,
            layer="important",
            scope=scope if scope != "private" else "private",
            subject_id=subject,
            about=name,
            source_session="manual",
        )

    def forget_memory(self, memory_id: int) -> bool:
        return self.store.forget_memory(int(memory_id))

    def _people_for_prompt(self, group_id: str | None = None) -> list[tuple[str, bool]]:
        """群聊花名册。搭档本人也算其中一员（他的名字存在 handler_name，不在名单里）。

        **搭档在群里显示的是 QQ 昵称，而她叫他的是另一个名字**——两个都要给她，
        并且说明是同一个人。真机数据里她正是因为这个写过
        "搭档与群友习惯微笑i相识满一天"（把同一个人当成了两个）。

        备注名（你给群友起的名字）优先于平台昵称——昵称会变、会重名，备注不会。

        P1-12：**只带"在这个群里见过的人" + 搭档**，并按最近活跃排序、最多
        `ROSTER_MAX` 个。以前是整份全局花名册进每一个群的提示词：只在群 B 自我介绍过的
        阿伟会在群 A 被叫出来，认识的人越多 system 越长（301 人 → 2463 字）。
        group_id=None 用当前场景的 `self.group_id`。
        """
        gid = self.group_id if group_id is None else str(group_id or "")
        meta = self._people_meta()
        notes = self.store.all_notes()
        owner_id = self.handler_id
        owner_name = self.handler_name

        def display_of(uid: str, fallback: str = "") -> str:
            return notes.get(uid) or str(meta.get(uid, {}).get("name") or "") or fallback

        rows: list[tuple[str, bool]] = []
        # 搭档永远排在第一个（他在哪个群都是他）
        if owner_id:
            shown = display_of(owner_id, owner_name)
            if owner_name and shown and shown != owner_name:
                rows.append((f"{owner_name}（群里叫「{shown}」，是同一个人）", True))
            elif owner_name or shown:
                rows.append((owner_name or shown, True))

        others: list[tuple[str, str]] = []
        for uid, info in meta.items():
            if owner_id and uid == owner_id:
                continue
            groups = info.get("groups") or []
            if gid:
                if gid not in groups:
                    continue  # 只在别的群见过：这个群里她不认得他
            elif groups:
                # 没有群上下文（私聊 / 群消息忘带 group_id）：只列"不在群里认识的"人，
                # 免得把别的群的人带进来
                continue
            shown = display_of(uid, str(info.get("name") or ""))
            if shown:
                others.append((str(info.get("last") or ""), shown))
        others.sort(key=lambda row: row[0], reverse=True)  # 最近活跃的排前面
        rows.extend((shown, False) for _last, shown in others)
        return rows[:ROSTER_MAX]

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
        return self._since_floor(self.store.messages_by_prefix(self.history_prefix, limit))

    def _since_floor(self, rows: list[dict]) -> list[dict]:
        """"我们之间没有共同的过去"——换人之后，工作记忆里也不该有前任的话（P0-3b）。

        记忆那一层有 `_memory_visible` 遮着，但**聊天记录窗口是另一条路**：
        搭档私聊的 session_id 对每一任都是 `i<n>-m-<日期>`，同一天换人时，
        新搭档的工作记忆里就摆着前任刚说的话（探针 lead_v2 的 B2 正是从这里漏的）。
        判据与记忆同一套：消息 id 水位线（精确）+ 时间戳严格晚于绑定时刻（兜底）。
        只裁搭档私聊会话；群聊记录是公开的，不受"换人"影响。
        """
        floor = self._private_floor()
        if not floor or self.scope_key != "m":
            return rows
        watermark = self._int_meta("handler_bound_msg_id")
        kept: list[dict] = []
        for row in rows:
            if watermark and int(row.get("id") or 0) <= watermark:
                continue  # 绑定之前就发出来的：藏着
            if str(row.get("ts") or "") <= floor and not watermark:
                continue  # 没有水位线时才退回时间比较（严格晚于才算"之后的"）
            kept.append(row)
        return kept

    @property
    def is_mock(self) -> bool:
        return self.llm.is_mock

    def status(self) -> LifeStatus:
        return self.lifespan.status()

    def _auto_bind(self, uid: str) -> bool:
        """核心路径的"自动认主"（`[runtime] auto_bind`，与适配器同一开关）。

        适配器会先认主再调 core；但本地 CLI / 直接调 core 的调用方没有适配器去认人，
        所以这里补上同一件事：**第一条私聊的人就是搭档**。
        关掉 auto_bind（`auto_bind = false`）时返回 False —— 那就老老实实走
        "未绑定 = 未确认"路径：不检索私事、不写私事、不推进关系，工具也不开放。
        """
        if not bool(getattr(self.settings.runtime, "auto_bind", False)):
            return False
        if not uid:
            return False
        # 本地/单机调用方（uid 就是默认哨兵值）= 本机的搭档本人：
        # 不设遮蔽下界，他看的是自己留下的记忆（见 set_handler_id）。
        self.set_handler_id(uid, local=(uid == LOCAL_USER_ID))
        self.notices.append(
            f"[认主] 从现在起你就是她的搭档（想换人：/unbind）。"
        )
        return True

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
        mid: str = "",
    ) -> str:
        """平台无关入口。

        scene：
          c2c   —— 私下
          group —— 群里（多一段隐私约束）
          guest —— 同 group，语义上强调"不是搭档"（兼容旧调用）

        group_id / speaker_name：群里分清"谁在哪个群说的"，隐私分区靠它。
        is_handler：说话的人是不是搭档。None = 自己判断——
                    绑过就按绑定判（`user_id == handler_id`）；
                    没绑过时私聊会先"认主"（见 `_auto_bind`，与适配器的 auto_bind 同一开关），
                    认主后就按搭档处理；群里一律不算（群里只有群主认主或 /bind 才算）。
        log_only：只把这句话记进聊天记录、不回复（群里全量模式下的"旁听"）。
        """
        self.notices = []
        self.tool_writes = []
        # 清掉上一条回复的记账：适配器回报送达情况时只能作用在**这一轮**的回复上
        self.last_reply_id = None
        self.last_reply_text = ""
        self.current_mid = str(mid or "")   # 平台消息 id：撤回时要按它找
        self.last_scene = scene if scene in ("c2c", "group", "guest") else "c2c"
        text = (text or "").strip()
        if not text:
            return ""

        uid = user_id or LOCAL_USER_ID
        owner = self.handler_id
        if is_handler is None:
            if owner:
                # 契约 §三.1：有搭档时，只有他本人是搭档
                is_handler = (uid == owner)
            elif self.last_scene == "c2c" and self._auto_bind(uid):
                # 未绑定 + 私聊：先认主，再按搭档处理（契约 §三.1 的"自动认主"）。
                # 认主会经过 §二 的绑定时刻遮蔽，所以带不出前任的私事。
                owner = uid
                is_handler = True
            else:
                # 没有搭档，又不是认主那一条 → 未确认/客人路径（见下面的视角分支）
                is_handler = False
        self.is_handler = bool(is_handler)
        self.speaker_id = uid
        self.speaker_name = speaker_name or self.known_people().get(uid, "")
        self.group_id = group_id
        # 平台每条消息都带昵称 —— 顺手记进花名册（适配器也做，这里兜底：
        # 直接调 core 的路径没有适配器，不记的话她永远认不出人）
        if speaker_name and uid:
            self.remember_person(uid, speaker_name, group_id=group_id)

        # ★ 隐私的闸门：谁在说话，决定她此刻能想起谁的事。
        #   群里 → 只有"这个群里公开发生过的事"（别人的私聊记忆根本查不出来）
        #   搭档 → 你的私事 + 你自己 + 所有群
        #   别人私聊 / 未绑定的私聊 → 只有"他和她之间"的事（private 一条都进不来）
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

        # 学名字：谁都可以自我介绍（搭档、群友、私聊来的客人）。
        # 旁听（log_only）**只走正则**：不为一句「我是来看热闹的」多花一次模型调用（P1-14）。
        self.learn_name(text, uid, owner=self.is_handler, allow_llm=not log_only,
                        group_id=group_id)

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
                self.memory_scope[0],
                self.memory_scope[1],
                self.current_mid,
            )
            # P1-14：这里**不当场整理**。以前每攒够 consolidate_every 条旁听消息
            # 就是一次模型调用（全量大群里每 6 条一次），与"零 token"直接相反。
            # 现在只留一个待整理标记，由搭档那一轮（_post_turn）或退出路径统一处理——
            # 群聊记忆照样会生成，只是不在这里花那份钱。
            self._mark_listen_pending()
            return ""

        # 到期回收只跟搭档有关（别人说话不该触发她的回收流程）
        if self.is_handler and self.lifespan.is_expired():
            try:
                last_words = self._chat(text)
            except Exception as exc:
                # 到期那一轮也不能把回收流程吞掉：先留一句台词，再照常回收
                last_words = "……"
                self._note_error("到期前的最后一轮", exc)
            try:
                self.consolidate(force=True)
            except Exception as exc:
                self._note_error("到期整理记忆", exc)
            return f"{last_words}\n\n{self.run_recycle('寿命结束')}"

        try:
            reply = self._chat(text)
        except Exception as exc:
            # 最后一道兜底：一轮**一定**要有回复，异常不许冒到适配器（那会断连）
            self._note_error("这一轮", exc)
            reply = FAILURE_LINES[len(self.notices) % len(FAILURE_LINES)]
        try:
            if self.is_handler:
                self._post_turn()
            else:
                self.consolidate()
        except Exception as exc:
            # 整理失败不该让用户看不到她刚才那句回复（回复已经生成了）
            self._note_error("每一轮之后的整理", exc)
        return reply

    # ── 旁听攒下来的记忆（P1-14）───────────────────────────
    def _listen_pending(self) -> dict[str, str]:
        raw = str(self.store.state_get(LISTEN_PENDING_KEY, "") or "")
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _mark_listen_pending(self) -> None:
        """旁听攒着不整理，只留一个标记（P1-14）。

        语义：`listen_pending` 里列出的会话"还有旁听来的话没变成记忆"，
        由搭档那一轮（`_post_turn` → `flush_listen_pending`）
        或退出路径（`close` → `flush_sessions`）统一补整理。
        """
        rows = self._listen_pending()
        rows[self.session_id] = self.clock.iso()
        if len(rows) > 50:  # 只留最近 50 个会话，别让 state 无界增长
            for key in sorted(rows, key=lambda k: rows[k])[:-50]:
                rows.pop(key, None)
        self.store.state_set(LISTEN_PENDING_KEY, json.dumps(rows, ensure_ascii=False))

    def flush_listen_pending(self) -> int:
        """把旁听攒下来的会话补整理成记忆（P1-14）。返回新增条数。

        没有标记时什么都不做（零成本）。只在搭档那一轮 / 退出路径调用。
        """
        if not self._listen_pending():
            return 0
        added = flush_sessions(
            self.store,
            self.llm,
            self.settings,
            now_iso=self.clock.iso(),
            exclude_session=self.session_id,   # 当前会话有自己的阈值节奏
            stale_minutes=0.0,
            min_pending=1,
        )
        self.store.state_set(LISTEN_PENDING_KEY, "")
        return added

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
        # 工作记忆也过绑定时刻遮蔽：换人之后，新搭档看不到前任留下的对话窗口（P0-3b）
        recent = self._since_floor(
            self.store.recent_messages(
                self.session_id, self.settings.memory.session_window
            )
        )
        query = self._recall_query(recent, text)
        # ★ 只在她"此刻能看到"的记忆里检索 —— 群聊时检索不到别人私聊的内容，
        #   换人之后也检索不到前任的私事（visible_memories 里含绑定时刻遮蔽）
        memories = self.visible_memories()
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

        # ★ 只在"在场的人"的记忆里留下 —— 群聊里不能把 A 的事说给 B 听
        before_presence = len(recalled)
        recalled = self._filter_by_presence(recalled, text)
        if len(recalled) < before_presence:
            self.notices.append(
                f"[记忆分区] 滤掉 {before_presence - len(recalled)} 条"
                "关于不在场的人的记忆（不在群里说别人的事）"
            )

        # 今天是不是特别的日子 / 有没有还没讲过的梦
        self.last_occasions = self._find_occasions(
            now=now,
            days_known=self.current_bond(now).days_known,
            mark=mark,
            view=self.memory_view,   # 大日子也按归属过滤，别把私事带进群里
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
            # P1-13：注入路径只带在场的人（私聊不设这道闸门——你本来就有权知道所有人的事）
            impressions=self.impressions_block(
                text, present_only=self.last_scene in ("group", "guest")
            ),
            tool_rules=agent_tools.TOOL_RULES if agent_tools.specs(self) else "",
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

    def _reply_with_tools(self, messages: list[dict]) -> str:
        """让她先调工具、再说话（记忆工具，见 giftia/agent_tools.py）。

        以前她只有一条写记忆的路：对话结束后后台整理。于是她说"我记住了"
        的时候其实什么都没发生——那句话是嘴上说的，记忆是几分钟后才补上的。

        轮次预算是硬的（`tools_max_rounds`，默认 3），而且**最后一轮不给工具**：
        光在提示词里催"请说话"是不够的，她可能又去调工具，用户就永远等不到回复。
        拿掉工具才是真正的保证。
        """
        tools = agent_tools.specs(self)
        if not tools:
            return self.llm.chat(messages)
        rounds = max(1, int(self.settings.memory.tools_max_rounds))
        if rounds == 1:
            return self.llm.chat(messages)

        convo = list(messages)
        writes = 0
        for round_no in range(1, rounds + 1):
            if round_no == rounds:
                # 最后一轮：nudge + 不给工具 —— 保证她说得出一句话
                convo.append({"role": "system", "content": TOOL_LAST_ROUND_NUDGE})
                return self.llm.chat(convo)
            reply = self.llm.chat_with_tools(convo, tools=tools)
            if not reply.tool_calls:
                return reply.text
            convo.append(
                reply.raw_message or {"role": "assistant", "content": reply.text or ""}
            )
            for call in reply.tool_calls:
                if call.name == "remember":
                    writes += 1
                    if writes > agent_tools.MAX_WRITES_PER_TURN:
                        convo.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": "（这一轮记得够多了，先把话说出来）",
                        })
                        continue
                convo.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": agent_tools.execute(self, call.name, call.arguments),
                })
        return ""  # 走不到，最后一轮一定 return

    def _log_run(self, *, text: str, reply: str, recalled: list, prompt_chars: int) -> None:
        """每次处理记一行运行档案（JSONL）。

        排障时最常问的两个问题——"她当时到底看到了什么""记忆注入吃了多少 token"——
        靠聊天记录答不出来，靠这一行能答。只记数字和长度，**不记对话内容**
        （内容已经在数据库里了，不用抄第二份）。
        """
        try:
            path = self.settings.db_path.parent / "runs.jsonl"
            row = {
                "ts": self.clock.iso(),
                "instance": self.instance_no,
                "session": self.session_id,
                "scene": self.last_scene,
                "speaker": self.speaker_id[:12] if self.speaker_id else "",
                "is_handler": self.is_handler,
                "in_chars": len(text),
                "out_chars": len(reply),
                "prompt_chars": prompt_chars,      # 提示词有多大（记忆注入的代价）
                "recalled": len(recalled),         # 想起来几条
                "said": len(self.last_cited),      # 真的说出口几条（→ 记得更久）
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 记账失败不该影响对话

    def _chat(self, text: str) -> str:
        """一轮对话。**从模型请求到记账，任何一步出错都不许冒穿**（P1-3）。

        适配器那一层一旦接到异常就是整条 WS 重连，用户那句话落了库却永远没有回复，
        `note_reply_delivery` 的回滚也不会执行。所以这里的原则是：
        能回一句就先回一句，出的事写进 notices + diary。
        """
        now = self.clock.now()
        # 只有跟搭档说话才推进关系与心情 —— 别人刷不动你们的亲密度
        # （但记忆照记：这一轮说的话一样会被整理成属于他们的记忆）
        if self.is_handler:
            try:
                self._tick_inner(text, now)
            except Exception as exc:
                self._note_error("推进关系与心情", exc)
        messages, recalled = self._prepare(text)
        # 做梦放在回复之后：这一场梦下次说话时再讲，免得"刚做完就讲"
        if self.is_handler:
            try:
                self._maybe_dream(now)
            except Exception as exc:
                self._note_error("做梦", exc)
        self.last_recalled = recalled
        if recalled:
            # 被塞进提示词 ≠ 被想起。这里只记账，强化要等她真的说出口。
            try:
                self.store.mark_injected(
                    [r.memory["id"] for r in recalled], self.clock.iso(now)
                )
            except Exception as exc:
                self._note_error("记录被注入的记忆", exc)

        try:
            self.store.add_message(
                self.session_id,
                self.instance_no,
                "user",
                text,
                self.clock.iso(now),
                self.speaker_id,
                self.speaker_name,
                self.memory_scope[0],
                self.memory_scope[1],
                self.current_mid,
            )
        except Exception as exc:
            # 你的话没记下来是遗憾，但不该让她这一轮哑掉
            self._note_error("记下你说的这句话", exc)

        try:
            reply = self._reply_with_tools(messages)
        except LLMError as exc:
            reply = FAILURE_LINES[len(self.notices) % len(FAILURE_LINES)]
            self.notices.append(f"[模型请求失败] {exc}")
        except Exception as exc:
            # 工具写库失败 / 模型层抛的不是 LLMError（换 provider、库内部错误）……
            # 都不能冒穿：给一句兜底台词 + 留痕（P1-3）
            reply = FAILURE_LINES[len(self.notices) % len(FAILURE_LINES)]
            self._note_error("生成回复", exc)
        if not str(reply or "").strip():
            # 空回复不能写成一条空的 assistant 记录：她"以为自己说了句空话"（P3）
            reply = FAILURE_LINES[len(self.notices) % len(FAILURE_LINES)]
            self.notices.append("[回复] 模型返回了空回复，已用兜底台词顶上")
        try:
            self.last_reply_id = self.store.add_message(
                self.session_id,
                self.instance_no,
                "assistant",
                reply,
                self.clock.iso(now),
                "",
                "",
                self.memory_scope[0],
                self.memory_scope[1],
            )
        except Exception as exc:
            self.last_reply_id = None
            self._note_error("记下她说的这句话", exc)
        self.last_reply_text = reply

        try:
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
        except Exception as exc:
            self._note_error("回复之后的记账", exc)
        # 运行档案：这一轮她看到了什么、想了多少、提示词多大
        self._log_run(text=text, reply=reply, recalled=recalled, prompt_chars=len(messages[0]["content"]))
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

    # ── 大日子（纪念日 / 他的大日子）───────────────────────
    def _outreach_view(self, for_group: bool) -> list[tuple[str, str]]:
        """主动联系时"她此刻能提到哪些大日子"。

        发群里 → 只算**这个群**公开发生过的事（你的私事不外泄）；
        发给你 → 搭档视角（你的私事 + 你自己 + 所有群）。

        P0-4 的根因就是"同一个函数里两处查询用了两套口径"：
        `outreach_plan` 传了 view、`compose_outreach` 第二次查询忘传 (`view=None`
        = 不过滤) → 群发提示词里混进别人的私聊大日子。现在两处都用这一个函数。
        """
        if for_group:
            return [("group", self.last_group_id or "")]
        return [("private", ""), ("person", self.handler_id or "handler"), ("group", "*")]

    def _find_occasions(
        self,
        *,
        now,
        days_known: float,
        mark: bool,
        view: list[tuple[str, str]],
    ) -> list[Occasion]:
        """查大日子，并过一遍 P0-3b 的绑定时刻遮蔽：**换人之后，新搭档不该被提醒
        前任的大日子**（大日子是从记忆里派生的，记忆遮了、派生结果也得遮）。"""
        found = find_occasions(
            self.settings,
            self.store,
            now=now,
            instance_no=self.instance_no,
            days_known=days_known,
            mark=mark,
            view=view,
        )
        return self._filter_occasions_by_floor(found)

    def _filter_occasions_by_floor(self, occasions: list[Occasion]) -> list[Occasion]:
        floor = self._private_floor()
        if not floor or not occasions:
            return occasions
        try:
            blocked = {
                int(m["id"])
                for m in self.store.active_memories(
                    self.instance_no, view=[("private", "")]
                )
                if str(m.get("happened_ts") or "") < floor
            }
        except Exception:
            return occasions
        if not blocked:
            return occasions
        return [
            o for o in occasions
            if o.memory_id is None or int(o.memory_id) not in blocked
        ]

    # ── 主动联系 ──────────────────────────────────────────
    def outreach_plan(
        self, *, force: bool = False, for_group: bool = False
    ) -> OutreachPlan | None:
        now = self.clock.now()
        bond = self.current_bond(now)
        occasions = self._find_occasions(
            now=now,
            days_known=bond.days_known,
            mark=False,
            view=self._outreach_view(for_group),
        )
        result = outreach_plan(
            self.settings,
            self.store,
            self.clock,
            life=self.status(),
            occasions=occasions,
            dream=latest_untold(self.settings, self.store, self.clock, self.instance_no),
            mood=None if for_group else self.current_mood(now),
            instance_no=self.instance_no,
            force=force,
            for_group=for_group,
        )
        self.last_outreach_plan = result
        return result

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
        # ★ 与 outreach_plan 用**同一套视角**（P0-4）：发群里只算这个群公开的大日子。
        #   这里原来漏传 view，等于把所有人（含别人私聊）的大日子写进了要发群里的提示词。
        occasions = self._find_occasions(
            now=now,
            days_known=bond.days_known,
            mark=False,
            view=self._outreach_view(for_group),
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
            people=self._people_for_prompt(self.last_group_id if for_group else ""),
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

    def note_reply_delivery(self, delivered: str) -> None:
        """适配器回报：刚才那句回复**到底送出去了多少**。

        没送出去的话要从她的记忆里撤回——否则她以为自己说过了，
        而用户从来没看见（下一轮她可能引用"我刚才说的……"，用户一脸茫然）。
        只送出去一部分，就按用户实际看到的那部分留存。

        （真机自查发现的：`send()` 原来把发送异常吞掉只打一行日志。）
        """
        if not self.last_reply_id:
            return
        if delivered == self.last_reply_text:
            self.last_reply_id = None
            return
        if delivered:
            self.store.update_message_content(self.last_reply_id, delivered)
            self.store.add_diary(
                self.clock.iso(),
                f"回复只送出去一部分，已按实际送达的内容留存：{delivered}",
                kind="terminal",
                title="发送不完整",
            )
            self.notices.append("[发送] 回复只送出去一部分——她按你实际看到的那部分记")
        else:
            lost = self.last_reply_text
            self.store.delete_message(self.last_reply_id)
            self.store.add_diary(
                self.clock.iso(),
                f"这条回复没能送出去，已从她的记忆里撤回：{lost}",
                kind="terminal",
                title="发送失败",
            )
            self.notices.append(
                "[发送] 刚才那句没能送出去——她不会以为自己说过，下一轮会重新回答"
            )
        self.last_reply_id = None

    def note_outreach_sent(self, text: str, *, keep_in_history: bool = True) -> None:
        """适配器真的发出去之后调用：记账扣额度（私聊的还写进会话上下文）。

        发到群里的那句不写进会话 —— 那是当众说的话，不该混进你俩的私聊上下文。
        """
        now = self.clock.now()
        if keep_in_history:
            self.store.add_message(
                self.session_id, self.instance_no, "assistant", text, self.clock.iso(now)
            )
        # 心情分享一天只发一次：记下来，免得同一种心情一天里反复"分享"。
        # 用**计划里记下的**心情 key，不重新取一次——中间心情可能已经变了。
        plan_ = getattr(self, "last_outreach_plan", None)
        if plan_ is not None and getattr(plan_, "reason", "") == "mood_share":
            self.store.state_set(
                f"mood_shared:{now.date().isoformat()}",
                str(getattr(plan_, "mood_key", "") or ""),
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
        last_day = ""
        last_ts = ""
        for m in rows:
            content = " ".join(str(m["content"]).split())
            if not content:
                continue
            if len(content) > per_line:
                content = content[:per_line] + "…"
            # ★ 跨天分段：这份记录是**跨天**的（按会话前缀翻历史），
            #   不标日子她会把前天的和今天的当成一场连续对话 ——
            #   这正是"她记不住时间在流动"最直接的原因。
            day = day_label(str(m.get("ts") or ""))
            if day and day != last_day:
                lines.append(f"—— {day} ——")
                last_day = day
                last_ts = ""
            elif last_ts and _gap_hours(last_ts, str(m.get("ts") or "")) >= 2.0:
                # 同一天里隔了两个多小时，也标一下：否则"上午说的"和"刚说的"分不开
                lines.append(f"（隔了约 {_gap_hours(last_ts, str(m.get('ts') or '')):.0f} 小时）")
            last_ts = str(m.get("ts") or "")
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
        stats: dict = {}
        added = self.consolidate(force=False, stats=stats)
        # 顺手收尾：别的会话里卡着的旧对话也该变成记忆
        # （只补"已经静置 30 分钟以上"的，不打断正在聊的那个）
        stale = flush_sessions(
            self.store,
            self.llm,
            self.settings,
            now_iso=self.clock.iso(),
            exclude_session=self.session_id,
            stale_minutes=30.0,
            min_pending=2,
        )
        if stale:
            self.notices.append(f"[记忆整理] 补整理了 {stale} 条之前没记下的")
        # 旁听攒下来的话（P1-14）：搭档这一轮到了，顺手把它们变成记忆。
        # 没有标记时零成本；有标记时也只在这里花一次整理调用（不是每 N 条群消息一次）。
        listened = self.flush_listen_pending()
        if listened:
            self.notices.append(
                f"[记忆整理] 把群里旁听到的话整理成了 {listened} 条记忆"
            )
        if added:
            self.notices.append(f"[记忆整理] 新增 {added} 条长期记忆")
        if stats.get("skipped"):
            self.notices.append(f"[记忆整理] 跳过 {stats['skipped']} 条已经记过的")
        if stats.get("merged"):
            self.notices.append(f"[记忆整理] {stats['merged']} 条旧记忆被更详细的版本更新")
        if added or stats.get("skipped"):
            rec = self.reconcile_memories()
            if rec.get("merged"):
                self.notices.append(
                    f"[记忆整理] 合并了 {rec['merged']} 条重复的记忆（换个说法记了两遍的）"
                )
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

    def consolidate(self, force: bool = False, stats: dict | None = None) -> int:
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
            stats=stats,
            # 只有**私聊里**跟搭档的对话才推进关系、才能改她的称呼。
            # 群聊再热闹也不给亲密度加分——关系奖励的是"你来了"，不是"你说了多少"。
            on_draft=(
                self._on_draft
                if (self.is_handler and self.last_scene == "c2c")
                else None
            ),
        )

    def reconcile_memories(self, *, force: bool = False) -> dict:
        """整理记忆：把"换个说法记了两遍"的合并掉。

        模型负责判断哪些是同一件事（实测词面相似度切不开这两个情况），
        程序负责合并。默认同一个归属每 reconcile_hours 才做一次，免得白花调用。
        """
        scope, subject = self.memory_scope
        key = f"reconciled:{scope}:{subject}"
        now = self.clock.now()
        if not force:
            last = str(self.store.state_get(key, "") or "")
            if last:
                try:
                    hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600.0
                    if hours < self.settings.memory.reconcile_hours:
                        return {"merged": 0, "groups": 0, "kept": []}
                except ValueError:
                    pass
        # 合并是半破坏性的（模型判错 = 她真的忘掉一件事）——先拍照再动手
        view = [("private", "")] if scope == "private" else [(scope, subject)]
        before = len(self.store.active_memories(self.instance_no, view=view))
        snapshot = self.store.snapshot_memories(tag=f"reconcile-{scope}")
        result = reconcile(
            self.store,
            self.llm,
            self.settings,
            instance_no=self.instance_no,
            scope=scope,
            subject_id=subject,
        )
        # 闸门：合并**只会让条数变少**。真变多了说明出了岔子，回滚到整理前。
        after = len(self.store.active_memories(self.instance_no, view=view))
        if after > before:
            self.store.restore_memories(snapshot)
            self.notices.append(
                f"[记忆整理] 合并结果异常（{before} → {after} 条），已回滚到整理前"
            )
            result = {"merged": 0, "groups": 0, "kept": [], "rolled_back": True}
        self.store.state_set(key, self.clock.iso(now))
        return result

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
        # 自检/状态汇总：这一任的**全部**记忆只用来数条数（不展示内容），
        # 所以显式看全量（契约 §一：ALL_SCOPES 只给导出/自检/迁移用）。
        mems = self.store.active_memories(self.instance_no, view=Store.ALL_SCOPES)
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
        """/memories：把这个库里她记得的所有事摊给你看（排障用）。

        显式看全量（契约 §一）：这是"你看她的记忆"的自检入口，不是注入路径——
        真正进提示词的那条路是 `visible_memories()`（带视角与绑定时刻遮蔽）。
        """
        mems = self.store.active_memories(self.instance_no, view=Store.ALL_SCOPES)
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
        """/recall：手输一句话，看她会想起什么（排障用）。

        显式看全量（契约 §一）：这是让人核对"检索算法本身"的工具，
        不受视角/绑定时刻影响；真实一轮走的是 `visible_memories()`。
        """
        if not query.strip():
            return "用法：/recall <一句话>"
        st = self.status()
        recalled = recall(
            query,
            self.store.active_memories(self.instance_no, view=Store.ALL_SCOPES),
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
            # 关掉之前把**所有**还卡着没整理的会话都补上（不只是当前这个）——
            # 不然聊到一半停了、或者跨过零点的会话，那几条会永远变不成记忆。
            flush_sessions(
                self.store,
                self.llm,
                self.settings,
                now_iso=self.clock.iso(),
                min_pending=1,
            )
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass


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
    mid: str = "",
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
        mid=mid,
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
