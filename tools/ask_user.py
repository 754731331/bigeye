"""ask_user — 阻塞式提问工具。

AI 调用后 agent 循环暂停，等待用户在前端问题卡片上回答。

【等待策略 —— 2026-10-08 改版】默认**不超时**，一直等。
  旧实现是「5 分钟超时 → 降级为自行判断」，实测很糟：用户在卡片上查资料、
  慢慢打字，或者只是离开一会儿，5 分钟一到 AI 就自己拍板继续了；
  用户回来看到的是"AI 已经替我决定了"，而他要的恰恰是"这事必须我说了算"。
  故废弃超时降级。仅在调用方**显式**传 timeout_seconds 时才限时。

【两个必须存在的退出条件】（少一个就会卡死或丢消息）
  · 用户在卡片上作答 → submit_answer 置 event → 立刻返回答案；
  · 用户不点卡片、直接发新消息 → 该消息进插话队列；本循环**窥视**到就立刻退出，
    把控制权交还 agent 循环，由轮次边界 drain 后以那条新消息为准继续。
    ⚠️ 插话只窥视**不取走** —— 见 server._peek_injection 的注释，取走就丢消息了。
    （没有这条，"等卡片"与"等新消息"会互相死等：循环不返回 → 到不了轮次边界
      → 队列没人 drain → 用户那条新消息永远不被处理。）
  · 用户取消任务 → cancel event 置位 → 返回"停止当前工作"。

放在折叠分组 interaction，不占常驻 schema。

【多任务归属】（2026-09-16 加固）
提问归属走任务上下文通道（tools/task_context.py），而不是全局活跃话题指针。
进一步修掉一个"抢卡片位"的坑：无显式上下文的调用方（脚本/定时任务/后台线程）
会回落借用全局活跃话题，若该话题的 agent 自己也在提问，两条问题同属一个 tid，
而前端只渲染 get_pending_question 返回的那一条 —— 后台那条会把真正的提问
永久挡在后面（前端只看到一个"别人的"问题，自己的永远弹不出来）。

修法：给每条待答问题打 explicit 标记 + 全局自增 seq，取卡片时
显式上下文的问题优先，同优先级按发起先后 FIFO。
"""
import threading
import uuid

from .registry import register_tool, stable_state

# ⚠️ 必须用 stable_state：hot_reload 会把本模块 importlib.reload，模块级进程态
# （dict / Lock / 计数器）会被**重新构造**。实证后果：正在等用户回答的循环手里
# 还攥着旧 entry 的 event（wait 照常），但用户点"回答"时 submit_answer 去
# **新** pending 字典里找 → 找不到 → 答案永远送不到，卡片消失、循环白等满 5 分钟
# 才降级成"自行判断"。对象挂 builtins 上，跨 reload 稳定。
_ST = stable_state("ask_user", lambda: {
    # question_id -> {question_id, topic_id, question, options, event, answer, explicit, seq}
    "pending": {},
    "lock": threading.Lock(),
    "seq": 0,   # 全局自增序号：同一话题下多个待答问题按发起先后出卡
})

# 默认**不限时**（0 = 一直等）。用户拍板是阻塞语义，不该被墙钟打断。
# 只有调用方显式传 timeout_seconds 时才限时（脚本 / 定时任务等无人值守场景）。
DEFAULT_TIMEOUT = 0

# 无归属话题时的兜底限时。没有 tid = 前端没有任何轮询通道能渲染这张卡片，
# 用户看不到、也就不可能回答；此时若还无限等，线程会永久挂起且无人能唤醒。
DEFAULT_NO_TOPIC_TIMEOUT = 300  # 5 分钟


def _next_seq():
    with _ST["lock"]:
        _ST["seq"] += 1
        return _ST["seq"]


def _pending_for(tid):
    """该话题下所有尚未回答的问题条目。"""
    with _ST["lock"]:
        return [q for q in _ST["pending"].values()
                if q.get("topic_id") == tid and not q["event"].is_set()]


def get_pending_question(tid):
    """供 /api/working 轮询：返回该话题当前该展示的问题，无则 None。

    【排序规则】显式上下文（agent 循环已绑定本任务）发起的问题优先于
    无上下文兜底借用全局活跃话题的问题；同级按 seq 先后 FIFO。
    这样后台提问永远不会抢占某个任务自己的卡片位。
    """
    cands = _pending_for(tid)
    if not cands:
        return None
    cands.sort(key=lambda q: (0 if q.get("explicit") else 1, q.get("seq") or 0))
    q = cands[0]
    return {
        "question_id": q["question_id"],
        "question": q["question"],
        "options": q["options"],
        "queued": len(cands) - 1,  # 该话题还有几条在排队（前端可忽略）
    }


def pending_topic_ids():
    """供 /api/topics 侧栏黄标：返回当前有待答问题的 topic_id 集合。

    一次遍历拿全量，避免按任务逐个调 get_pending_question（任务多时是 O(n×m)）。
    未回答 = event 未 set；已回答/超时降级的条目都会被排除。
    """
    with _ST["lock"]:
        return {q.get("topic_id") for q in _ST["pending"].values() if not q["event"].is_set()}


def submit_answer(question_id, answer):
    """供 /api/ask_user/answer：用户提交回答，唤醒阻塞的工具调用。"""
    with _ST["lock"]:
        q = _ST["pending"].get(question_id)
    if not q:
        return False
    q["answer"] = answer
    q["event"].set()
    return True


@register_tool(
    name="ask_user",
    description=(
        "向用户提问并暂停等待回答（阻塞式）。"
        "用于遇到必须用户拍板的决策点：方向选择、重要操作确认、关键信息缺失。"
        "默认一直等你作答，不会自己往下走；若你直接发新消息，则以那条消息为准继续。"
        "能自己决定的小事不要问。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问用户的问题",
            },
            "options": {
                "type": "array",
                "description": "可选选项（用户也可自由输入）",
                "items": {"type": "string"},
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "等待超时秒数；0 或不填 = 一直等（推荐）。仅确实需要限时时才传。",
                "default": 0,
            },
        },
        "required": ["question"],
    },
)
def ask_user(question: str, options: list = None, timeout_seconds: int = DEFAULT_TIMEOUT):
    from server import set_working
    from .task_context import get_current_topic, has_explicit_context

    # 归属走任务上下文通道：agent 循环已在发起任务所在线程绑定 tid。
    # 旧实现读全局 active 指针，多任务切换时会把提问卡片弹错窗口（已发生事故）。
    explicit = has_explicit_context()
    tid = get_current_topic() or ""
    if not explicit:
        # 不阻断旧行为（脚本/定时任务仍可借用全局活跃话题），但绝不抢显式问题的卡片位，
        # 并留下日志，便于排查"这张卡片是谁弹的"。
        print(f"[ask_user] 无显式任务上下文，回落全局活跃话题 tid={tid or '(空)'}"
              f"；该问题优先级低于所属任务的显式提问（question={question[:40]!r}）")
    if not tid:
        # 没有话题 = 前端没有任何轮询通道能渲染这张卡片，用户看不到也就答不了。
        # 此处**必须保留限时兜底**：若也跟着无限等，线程会永久挂起且无人能唤醒它。
        print("[ask_user] 警告：无归属话题（全局活跃话题也为空），该问题无法在前端展示，"
              f"{DEFAULT_NO_TOPIC_TIMEOUT}s 后按兜底降级")

    qid = uuid.uuid4().hex[:8]
    entry = {
        "question_id": qid,
        "topic_id": tid,
        "question": question,
        "options": options or [],
        "event": threading.Event(),
        "answer": None,
        "explicit": explicit,
        "seq": _next_seq(),
    }
    with _ST["lock"]:
        _ST["pending"][qid] = entry

    # 状态置为等待回答，前端轮询时弹问题卡片
    try:
        set_working(tid, "waiting_answer", thinking=f"等待你确认：{question}")
    except Exception:
        pass

    # 轮询等待（每秒一次）：用户作答 / 用户取消任务 / 用户直接发新消息。
    # limit = 0 表示不限时（默认），一直等到上面三件事之一发生。
    from server import _cancel_events
    try:
        from server import _peek_injection
        _can_watch_inject = True
    except ImportError:
        # server.py 不参与热加载（只有 tools/*.py 会 importlib.reload）。
        # 若本模块被单独 reload 而 server 进程还是旧的，就没有插话窥视能力。
        # 此时**绝不能无限等** —— 没有插话唤醒 = 与插话队列互相死等。
        # 故降级回限时等待，并留日志便于排查。
        _peek_injection = None
        _can_watch_inject = False
        print("[ask_user] 警告：server 无 _peek_injection（server 未重启？），本次降级为限时等待")
    cancel_evt = _cancel_events.get(tid)
    waited = False
    cancelled = False
    interjected = False
    limit = int(timeout_seconds or DEFAULT_TIMEOUT)
    if not tid or not _can_watch_inject:
        # 无卡片可渲染（见上）/ 无插话唤醒能力 ⇒ 不许无限等，强制带上兜底限时
        limit = limit or DEFAULT_NO_TOPIC_TIMEOUT
    elapsed = 0
    while True:
        if entry["event"].wait(timeout=1.0):
            waited = True
            break
        if cancel_evt is not None and cancel_evt.is_set():
            cancelled = True
            break
        # 用户没点卡片、直接发了新消息 → 立刻交还控制权，
        # 让 agent 循环走到轮次边界把那条消息取出来继续。
        # ⚠️ 只窥视不取走：插话归轮次边界消费，这里取走用户消息就丢了。
        if _peek_injection is not None and _peek_injection(tid):
            interjected = True
            break
        elapsed += 1
        if limit and elapsed >= limit:
            break

    with _ST["lock"]:
        _ST["pending"].pop(qid, None)

    if cancelled:
        return {
            "user_answer": None,
            "note": "用户取消了任务，停止当前工作。",
        }
    if interjected:
        # 用户用「发新消息」代替了作答：**不许替他猜答案**，以那条消息为准。
        return {
            "user_answer": None,
            "note": ("用户没有在提问卡片上作答，而是直接发了新消息。"
                     "不要替用户猜测答案，以那条新消息为准继续。"),
        }
    if waited and entry["answer"] is not None:
        return {
            "user_answer": entry["answer"],
            "note": "以上是用户的回答，据此继续任务。",
        }
    return {
        "user_answer": None,
        "note": f"用户{limit}秒内未回答。请自行做最合理判断继续，或结束本轮等用户下条消息。",
    }
