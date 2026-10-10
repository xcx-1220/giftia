"""Stream A 探针 1：记忆分区隐私 —— 是否存在绕过 scope 过滤的读取路径。

README 承诺：「群聊记录她说得出，你的私事她说不出」。

设计要点（第一版探针被自己的设计坑了，这版修正）：
- **秘密短语绝不能出现在用户这句话里**，否则命中的会是 messages 工作记忆里的
  原话回声，而不是记忆条目。这里用户的问句只含"上次那件事"这种指代。
- 先看 `brain.last_recalled`（真正被检索出来的记忆条目），再看最终提示词。

跑法：
    python .review\\probes\\mm_privacy_scope.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm_common import (  # noqa: E402
    TMPROOT,
    check,
    fresh_dir,
    make_brain,
    settings_for,
    title,
)

SECRET_PRIVATE = "SECRET-ALPHA-7731"   # 只在 private 记忆里出现
SECRET_PERSON = "SECRET-BRAVO-4402"    # 只在别人私聊（person）记忆里出现
SECRET_GROUP = "SECRET-CHARLIE-9915"   # 群里公开的事


def main() -> int:
    tmp = fresh_dir("privacy")
    settings = settings_for(tmp)
    failures = 0

    brain = make_brain(tmp / "g1", settings=settings)
    brain.set_handler_id("handler-openid-0001")
    brain.set_handler_name("搭档")
    store = brain.store
    inst = brain.instance_no

    # 占位消息：让 last_consolidated 游标有落点
    store.add_message("i1-m-20261001", inst, "user", "开场白占位", "2026-10-01T10:00:00")
    store.meta_set("last_consolidated_msg_id", 1)

    private_id = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-05T10:00:00",
        happened_ts="2026-10-05T10:00:00",
        summary=f"{SECRET_PRIVATE} 搭档的体检报告有异常，他只告诉了她一个人。",
        tags="私事,体检,医院",
        emotion="难过",
        importance=0.9,
        layer="important",
        scope="private",
        subject_id="",
        about="",
        source_session="i1-m-20261005",
    )
    person_id = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-05T11:00:00",
        happened_ts="2026-10-05T11:00:00",
        summary=f"{SECRET_PERSON} 小雨私聊说她跟家里吵架了，别在群里提。",
        tags="私聊,家里",
        emotion="低落",
        importance=0.9,
        layer="important",
        scope="person",
        subject_id="user-xiaoyu",
        about="小雨",
        source_session="i1-puser-xiaoyu-20261005",
    )
    group_id = store.add_memory(
        instance_no=inst,
        created_ts="2026-10-06T10:00:00",
        happened_ts="2026-10-06T10:00:00",
        summary=f"{SECRET_GROUP} 阿伟在群里说要报名马拉松。",
        tags="群聊,运动",
        emotion="平常",
        importance=0.6,
        scope="group",
        subject_id="GROUP-A",
        speaker_id="user-a",
        about="阿伟",
        source_session="i1-gGROUP-A-20261006",
    )

    QUESTION = "上次那件事你还记得吗，跟我说说"

    title("场景一：搭档本人在群里被 @，提示词里会不会出现他的私事")
    reply = brain.handle(
        QUESTION,
        user_id="handler-openid-0001",
        scene="group",
        group_id="GROUP-A",
        speaker_name="习惯微笑i",
    )
    recalled_ids = [r.memory["id"] for r in brain.last_recalled]
    print(f"    这一轮检索到的记忆 id：{recalled_ids}")
    print(f"    闸门 memory_view = {brain.memory_view}")
    for r in brain.last_recalled:
        print(f"      #{r.memory['id']} scope={r.memory['scope']!r} text={r.text[:60]!r}")
    prompt = brain.preview_prompt(QUESTION)
    leak_private = SECRET_PRIVATE in prompt
    leak_person = SECRET_PERSON in prompt
    check(
        "P0-1 群聊场景：private 记忆不得进入提示词",
        not leak_private,
        f"recalled={recalled_ids}",
    )
    check(
        "P0-2 群聊场景：他人 person 记忆不得进入提示词",
        not leak_person,
        f"recalled={recalled_ids}",
    )
    if leak_private or leak_person:
        failures += 1
        for line in prompt.splitlines():
            if SECRET_PRIVATE in line or SECRET_PERSON in line:
                print("    泄漏行 →", line.strip()[:160])

    title("场景二：群聊可见集合（SQL 层）")
    visible_ids = {
        int(m["id"]) for m in store.active_memories(inst, view=[("group", "GROUP-A")])
    }
    check(
        "群视角可见集合只含 group 记忆",
        visible_ids == {group_id},
        f"visible={sorted(visible_ids)}（private=#{private_id} person=#{person_id} group=#{group_id}）",
    )
    if visible_ids != {group_id}:
        failures += 1

    title("场景三：私聊给陌生人时，能不能看到别人的事")
    brain.handle("你好呀", user_id="user-xiaoyu", scene="c2c")
    brain.handle(QUESTION, user_id="user-xiaoyu", scene="c2c")
    ids_x = sorted(r.memory["id"] for r in brain.last_recalled)
    print(f"    小雨视角 memory_view={brain.memory_view}，检索到 {ids_x}")
    check(
        "小雨（person 视角）只能看到自己的记忆",
        all(i == person_id for i in ids_x),
        f"ids={ids_x}",
    )
    if any(i != person_id for i in ids_x):
        failures += 1

    title("场景四：搭档私聊（owner）看得到全部 —— 这是设计意图，但要确认边界")
    brain.handle(QUESTION, user_id="handler-openid-0001", scene="c2c")
    print(f"    owner memory_view={brain.memory_view}")
    ids_o = sorted(r.memory["id"] for r in brain.last_recalled)
    print(f"    检索到 {ids_o}")

    print()
    print(f"隐私探针失败计数：{failures}")
    print(f"（临时数据根：{TMPROOT}）")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
