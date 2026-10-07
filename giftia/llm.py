"""模型接入层：DeepSeek 客户端 + 离线 mock。零第三方依赖（只用标准库 urllib）。

三种任务分开成三个方法，而不是让上层去拼字符串：
    chat()              —— 她在跟你说话
    extract_memories()  —— 后台把对话压成记忆条目（结构化 JSON）
    write_memoir()      —— 回收日写回忆录

`provider = "mock"` 时用 MockLLM：不需要 API key，整套寿命/记忆/回收的机器照样能跑，
方便先验证"她像不像"，也方便自动化测试。
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from .config import ModelConfig


class LLMError(RuntimeError):
    pass


@dataclass
class MemoryDraft:
    summary: str
    tags: str = ""
    emotion: str = ""
    importance: float = 0.5
    layer: str = ""          # core | important | normal（空 = 由程序按重要性判定）
    preferred_name: str = ""  # 他明确要求改称呼时才填（例如"以后叫我小林"）
    event_date: str = ""      # 他说过的"将来某一天"（YYYY-MM-DD），没有就留空


LAYERS = ("core", "important", "normal")


# ── 提示词（属于模型任务，不属于人格）────────────────────────────
CONSOLIDATE_SYSTEM = """你是 Giftia 的记忆整理模块（她本人看不到这段指令）。
把下面这段对话，压缩成 0～3 条长期记忆条目。
{scene_rule}

只记对长期关系有意义的东西：发生的事、对方的偏好与习惯、情绪变化、约定、说过的要紧话。
不要记寒暄、天气、以及没有信息量的闲聊。宁少勿多；确实没有值得记的，就返回空数组。

严格输出 JSON，不要任何解释文字：
{{"memories":[{{"summary":"一句话，不超过60字，用第三人称，用「搭档」指代对方","tags":"逗号分隔的2-4个关键词","emotion":"当时的情绪，2-6字","importance":0.0到1.0之间的小数,"layer":"core|important|normal","preferred_name":"","event_date":""}}]}}

今天是 {today}。

importance 参考：0.9 以上=非常重要的约定或心事；0.5=日常但有意义；0.2 以下=可记可不记。
layer 判定（很重要，决定这条记忆会不会随时间变模糊）：
- core：**关于这个人本身的核心事实**——名字/称呼、身份、你们的关系、郑重的约定、他明确说过的偏好或禁忌。
  她会一直记得这些，哪怕到了生命尽头。
- important：重要的事件、情绪波动、他正在经历的重要事情（考试、生病、搬家）。
- normal：其余日常。

preferred_name 的判定（**只在下面两种情况填，其余一律留空**）：
1. 他**明确要求你换个称呼**，例如"以后叫我小林""别叫我搭档了" → 填 "小林"；
2. 他**自我介绍**，例如"我叫小林""我是小林" → 填 "小林"。
闲聊里提到别人的名字、或那个名字不是指他自己时，**不要填**。绝大多数情况下这里是空字符串。

event_date 的判定（**只在这一种情况下填**）：
他说了一件**将来某一天**会具体发生的事——考试、面试、生日、约会、约定的期限，
并且这个日期**能推算出确切的一天**（"下周三要考驾照"→ 按今天 {today} 推算）。
填 YYYY-MM-DD。其余情况（"最近""以后""经常会"）一律留空；推算不确定也留空。"""

REWRITE_SYSTEM = """你是记忆检索的查询改写模块。
把搭档的这句话改写成 2～3 个**用于检索长期记忆**的短查询：补上同义词、上位词、可能的说法。
例如「今天面试砸了」→ ["面试", "找工作 求职", "紧张 失败 心情"]。

只输出 JSON，不要解释：{{"queries":["...","..."]}}
每个查询 2～8 个字，不要整句照抄。"""

MEMOIR_SYSTEM = """你是「终端服务」的记录员。Giftia 的寿命已经结束，即将被回收、记忆将被清除。
请根据她的记忆条目，写一份简短的回收记录（回忆录），交给她的人类搭档留存。

要求：
- 以「你」称呼那位搭档，语气平静、克制，像一份官方文件里透出来的一点温柔。
- 只使用给出的记忆条目，不要编造没有发生过的细节。
- 结构：一段两三句的开场 + 每条记忆一小段 + 一句收尾。
- 总长度 200～350 字。不要标题，不要列表符号，不要用引号。
- 结尾必须以这句收束：她的记忆将被清除，而你会记得。"""

DREAM_SYSTEM = """你是 Giftia 的睡眠整理模块（她本人看不到这段指令）。下面是她记忆里的一些碎片——它们已经被时间磨得残缺不全。

请把这些碎片揉成**一场梦**，用她的第一人称写，40～90 字。

要求：
- 梦是有裂缝的：场景可以突然换，人物可以模糊，但**情绪要连贯**。
- 只使用给出的碎片，不要引入新的人、地点或事件。
- 不要解释"这是梦"，不要提到记忆、遗忘、系统、数据库。
- 不要写"我梦见"开头，直接进入场景。结尾可以停在半句话上。"""

SCENE_RULES = {
    "c2c": "这段是「搭档」和「{name}」的一对一私聊：用「搭档」指代对方。",
    "person": "这段是「{name}」和**某一个人**的私聊：用那个人的名字指代他，不要用「搭档」。",
    "group": (
        "这段是**群聊记录**：说话的人不止一个，稿子每行开头的名字就是发言人。"
        "**逐个用他们的名字**指代（例如「阿伟」「小雨」），不要用「搭档」统称，"
        "也不要写成「搭档们」。小林是她的搭档，其余人是群里的人。"
    ),
}

NAME_SYSTEM = """判断下面这句话里，说话人是不是在**介绍自己的名字**。

- 是自我介绍 → **只输出那个名字**（2～16 个字），不要标点、不要解释、不要加引号。
- 只是描述身份或状态 → 输出 NONE。
  例：「我是学生」「我是来问你个事的」「我是新来的」「我是说……」「我是那种人」都是 NONE。
- 完全不是在说自己 → 输出 NONE。

说话人可能在名字后面接着说别的（「我叫小林，今年二十八」），这种情况只输出名字。
名字可以是昵称、网名、外号，允许很长的中二名字（照抄，不要概括）。"""


class BaseLLM:
    """所有实现都遵循这个接口，上层只认它。"""

    label = "base"
    is_mock = False

    def chat(
        self, messages: list[dict], *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        raise NotImplementedError

    def extract_memories(
        self, transcript: str, *, name: str = "Giftia", today: str = "", scene: str = "c2c"
    ) -> list[MemoryDraft]:
        raise NotImplementedError

    def write_dream(self, fragments: list[str], *, name: str = "Giftia") -> str:
        """把记忆碎片揉成一场梦。"""
        raise NotImplementedError

    def extract_self_name(self, text: str) -> str:
        """判断这句话是不是自我介绍；是就返回名字，不是就返回空串。

        存在的理由：「我是小雨」和「我是来问你个事的」句式完全一样，
        正则分不清，但这两者搞错的代价差很多（把"来问你个事"当成名字会很难看）。
        """
        return ""

    def rewrite_query(self, text: str) -> list[str]:
        """把一句话改写成 2～3 个检索查询。返回空列表 = 不改写。"""
        return []

    def write_memoir(self, context: str) -> str:
        raise NotImplementedError


# ── 真实实现 ──────────────────────────────────────────────────────
class DeepSeekLLM(BaseLLM):
    label = "deepseek"

    def __init__(self, cfg: ModelConfig, api_key: str | None = None, retries: int = 3) -> None:
        import os

        self.cfg = cfg
        self.api_key = api_key or os.environ.get(cfg.api_key_env, "").strip()
        self.retries = max(1, retries)
        if not self.api_key:
            raise LLMError(
                f"没有找到 API key。请设置环境变量 {cfg.api_key_env}，"
                f"或先用 --mock 跑离线模式。"
            )

    # 底层 HTTP
    def _post(self, payload: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {self.api_key}")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.request_timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                last = LLMError(f"HTTP {exc.code}：{detail}")
                if exc.code in (400, 401, 403, 404, 422):  # 重试没意义
                    break
            except Exception as exc:  # 超时、断网、DNS……
                last = LLMError(f"{type(exc).__name__}: {exc}")
            if attempt < self.retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("请求失败")

    def chat(
        self, messages: list[dict], *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        payload: dict = {
            "model": self.cfg.name,
            "messages": messages,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.thinking:
            # 思考模式下 temperature 会被忽略，干脆不传
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = (
                self.cfg.temperature if temperature is None else temperature
            )

        data = self._post(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"返回结构异常：{str(data)[:300]}") from exc
        text = (content or "").strip()
        if not text:
            raise LLMError("模型返回了空内容")
        return text

    def extract_memories(
        self, transcript: str, *, name: str = "Giftia", today: str = "", scene: str = "c2c"
    ) -> list[MemoryDraft]:
        raw = self.chat(
            [
                {
                    "role": "system",
                    "content": CONSOLIDATE_SYSTEM.format(
                        name=name,
                        today=today or "（未提供）",
                        scene_rule=SCENE_RULES.get(scene, SCENE_RULES["c2c"]).format(name=name),
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            temperature=0.3,
            max_tokens=800,
        )
        return parse_memory_drafts(raw)

    def rewrite_query(self, text: str) -> list[str]:
        raw = self.chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            max_tokens=200,
        )
        data = _parse_json_loose(raw)
        items = data.get("queries") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [str(x).strip() for x in items if str(x).strip()][:3]

    def write_memoir(self, context: str) -> str:
        return self.chat(
            [
                {"role": "system", "content": MEMOIR_SYSTEM},
                {"role": "user", "content": context},
            ],
            temperature=0.8,
            max_tokens=900,
        )

    def write_dream(self, fragments: list[str], *, name: str = "Giftia") -> str:
        body = "\n".join(f"- {f}" for f in fragments if str(f).strip())
        return self.chat(
            [
                {"role": "system", "content": DREAM_SYSTEM},
                {"role": "user", "content": f"记忆碎片：\n{body}"},
            ],
            temperature=1.0,
            max_tokens=300,
        )

    def extract_self_name(self, text: str) -> str:
        raw = self.chat(
            [
                {"role": "system", "content": NAME_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=40,
        )
        return clean_name(raw)


def clean_name(raw: str) -> str:
    """把模型返回的名字洗干净；NONE / 空 / 超长都当作"没认出来"。"""
    name = str(raw or "").strip().strip("「」『』\"'“”‘’。，,.!?！？：: \n\t")
    if not name or name.upper().startswith("NONE"):
        return ""
    name = name.splitlines()[0].strip()
    if len(name) < 2 or len(name) > 16:
        return ""
    return name


def infer_layer(importance: float, emotion: str = "") -> str:
    """模型没给 layer 时的兜底判定。情感强烈的记忆至少在 important 层。"""
    if importance >= 0.8:
        return "core"
    if importance >= 0.7 or (importance >= 0.5 and emotion.strip()):
        return "important"
    return "normal"


def parse_memory_drafts(raw: str) -> list[MemoryDraft]:
    """宽容地解析模型返回的 JSON（可能裹着 ```json 或前后废话）。"""
    data = _parse_json_loose(raw)
    items = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    drafts: list[MemoryDraft] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            importance = float(it.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        emotion = str(it.get("emotion", "")).strip()
        layer = str(it.get("layer", "")).strip().lower()
        if layer not in LAYERS:
            layer = infer_layer(importance, emotion)
        event_date = str(it.get("event_date", "") or "").strip()[:10]
        if event_date:
            try:
                date.fromisoformat(event_date)
            except ValueError:
                event_date = ""   # 模型算错日期就当作没有，别让错的提醒冒出来
        drafts.append(
            MemoryDraft(
                summary=str(it.get("summary", "")).strip(),
                tags=str(it.get("tags", "")).strip(),
                emotion=emotion,
                importance=importance,
                layer=layer,
                preferred_name=str(it.get("preferred_name", "") or "").strip()[:16],
                event_date=event_date,
            )
        )
    return drafts


def _parse_json_loose(raw: str) -> dict:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


# ── 离线实现 ──────────────────────────────────────────────────────
def _worth_remembering(text: str) -> bool:
    """mock 用的粗筛：太短的不记，提问不记。"""
    text = (text or "").strip()
    if len(text) < 6 or len(text) > 120:
        return False
    if text.endswith(("？", "?")):
        return False
    return not text.startswith(("你还记得", "你记不记得", "记得吗"))


class MockLLM(BaseLLM):
    """不需要 API key 的假模型。

    它不是随便回一句"我是 mock"——它会读系统提示里检索到的记忆并复述其中片段，
    这样"检索 → 注入 → 回复"这条链路在离线状态下也是真的被验证过的。
    """

    label = "mock（离线假装）"
    is_mock = True

    def __init__(self, cfg: ModelConfig | None = None) -> None:
        self.cfg = cfg
        self._rng = random.Random(20260407)

    def chat(
        self, messages: list[dict], *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        system = messages[0]["content"] if messages else ""
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break

        recalled = re.findall(r"^◆\s*(.+)$", system, re.M)
        stage = "clear"
        m = re.search(r"stage=([a-z]+)", system)
        if m:
            stage = m.group(1)

        parts: list[str] = []
        if stage == "terminal":
            parts.append("……嗯。")
            parts.append("我好像，已经想不起很多事了。")
            if recalled:
                frag = recalled[0].split("（")[0].strip()[:18]
                parts.append(f"但是「{frag}」……这句我还留着。")
            parts.append("你还在，就好。")
            return "".join(parts)

        parts.append(self._rng.choice(["……嗯。", "……我在听。", "……", "嗯……"]))
        if recalled:
            # 记忆行形如「2026-03-01 12:00｜内容（这段记忆已经很模糊）」
            frag = recalled[0].split("｜")[-1].split("（")[0].strip()
            frag = frag[:24] + ("……" if len(frag) > 24 else "")
            parts.append(f"你提到这个，我想起之前的事——{frag}")
        if stage in ("blurred", "fading"):
            parts.append("……只是细节，我记不太清了，对不起。")
        elif last_user:
            snippet = last_user.strip().replace("\n", " ")[:16]
            parts.append(f"「{snippet}」……我记下了。")
        parts.append(self._rng.choice(["……", "你今天，还好吗？", "我在。"]))
        return "".join(parts)

    def extract_memories(
        self, transcript: str, *, name: str = "Giftia", today: str = "", scene: str = "c2c"
    ) -> list[MemoryDraft]:
        """启发式兜底：从别人说过的话里挑两条最长的留下来（提问不算记忆）。

        注意是"别人"而不只是"搭档"：群聊稿子里的发言人也一样会被记住，
        真模型读的也是同一份稿子。
        """
        rows: list[tuple[str, str]] = []
        for line in transcript.splitlines():
            if "：" not in line:
                continue
            who, text = line.split("：", 1)
            who, text = who.strip(), text.strip()
            if not text or who == name:  # 她自己说的话不算"记忆素材"
                continue
            rows.append((who, text))
        rows.sort(key=lambda r: len(r[1]), reverse=True)

        drafts: list[MemoryDraft] = []
        for who, text in rows:
            if not _worth_remembering(text):
                continue
            summary = text if len(text) <= 60 else text[:57] + "……"
            # 名字/称呼这类会被当成"内核记忆"，好让离线演示也能看出分层效果
            layer = "core" if any(k in text for k in ("叫我", "名字", "我叫", "别叫")) else "normal"
            importance = 0.85 if layer == "core" else 0.5
            drafts.append(
                MemoryDraft(
                    summary=f"{who or '搭档'}说过：{summary}",
                    tags="搭档,聊天",
                    emotion="平常",
                    importance=importance,
                    layer=layer,
                )
            )
            if len(drafts) >= 2:
                break
        return drafts

    def rewrite_query(self, text: str) -> list[str]:
        """离线不编造同义词：只做一次"去掉语气词、取实词"的确定性切分。

        真正的改写交给真模型；这里返回变体是为了让整条链路在离线时也能被测试。
        """
        cleaned = re.sub(r"[的了呢吧啊呀嘛哦噢，。！？!?、；;：:\s]+", " ", text or "")
        parts = [p for p in cleaned.split(" ") if len(p) >= 2]
        return parts[:2]

    def write_dream(self, fragments: list[str], *, name: str = "Giftia") -> str:
        cleaned: list[str] = []
        for frag in fragments[:3]:
            text = re.sub(r"（[^）]*）", "", str(frag)).strip("。…，, ")
            if text:
                cleaned.append(text)
        if not cleaned:
            return ""
        return "梦里有" + "，还有".join(cleaned) + "。\n中间好像断过一次，接不上了。"

    # 离线版的"这是不是自我介绍"：句式判定 + 停用词，够测试用。
    _NAME_STOP = ("学生", "来问", "新来", "是说", "那种", "想", "要", "在", "个", "的")
    _NAME_CUT = "，。！？,.!? \n\t今年月日岁数第"
    # 名字不会以这些字开头（"我是说你别急" → "说你别急" 就该被否掉）
    _NAME_HEAD = set("说别你我他她是想要来去有没有不在真太很就都也还这那什么")

    def extract_self_name(self, text: str) -> str:
        m = re.search(
            r"我(?:的名字)?(?:叫|是)([\u4e00-\u9fa5A-Za-z0-9_·]{2,20})", str(text or "")
        )
        if not m:
            return ""
        name = m.group(1)
        for ch in self._NAME_CUT:
            name = name.split(ch)[0]
        if len(name) < 2 or len(name) > 16:
            return ""
        if name[0] in self._NAME_HEAD:
            return ""
        if any(word in name for word in self._NAME_STOP):
            return ""
        return name

    def write_memoir(self, context: str) -> str:
        entries = [ln.strip("- ").strip() for ln in context.splitlines() if ln.strip().startswith("-")]
        body = "".join(f"　　{ln}\n" for ln in entries[:6])
        return (
            "档案编号已归档。这名 Giftia 的服务期到此为止。\n\n"
            f"在她被清除之前，她留下的记忆是这样一些片段：\n{body}\n"
            "她大概不会知道自己被记得这么清楚——毕竟到最后，她连你的名字都握不住了。\n\n"
            "她的记忆将被清除，而你会记得。"
        )
