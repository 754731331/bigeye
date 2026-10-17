"""目标级打转检测模块。

检测 LLM 在任务执行中是否陷入"打转"：大量探索类工具调用（读/搜/执行）
但零落地类调用（写文件/完成节点/沉淀记忆/求助），即"光看不干"。

★ 2026-10-16 重大修复（自指死循环）★
事故现象：模型收到提醒后，把提醒本身当成了"用户新指令"，跑去调查/根除
这个提醒机制（任务 ebba91b7：「用户新指令收到：继续任务 + 另外要我查一个
"陷入循环的提醒机制"并根除」）。调查行为全是探索类调用 → 又一次触发提醒
→ 再调查 → 真死循环，用户原话只有"你看一下…"和"实现"两条。

根因有三，逐条修掉：
1) 文案漏洞：原文案写"不要提及本条提醒，不要暴露任何内部机制或检测名称"
   —— 这恰恰诱导模型去研究"这个内部机制到底是什么"。已改为显式声明
   "系统自动提示·不是用户消息·不要调查本机制"。
2) 无自指保护：窗口里全是在调查检测器自身，还照样触发。已加自指关键词
   检测，命中即静默且不再注入。
3) 无熔断：注入后模型必然继续探索（它的"响应"就是调用工具），于是必然
   二次触发。已加"注入后必须先有落地调用才允许再注入" + 每话题硬上限。

设计原则：
- 内存态、零 DB 查询，埋点零成本。
- 环形 buffer 记录最近 N 次工具调用，按落地/探索/中性分类。
- 触发条件：窗口内探索类占比达到阈值、落地类为 0、距上次提醒超冷却，
  且未处于"等落地"状态、未超每话题注入上限。
- 落地类调用出现即解除等待并重置冷却（说明有实质进展，不打断）。

调用方：server.py 的 agent 主循环。每次工具调用后 record_call(tid, tool_name, args)，
每轮工具执行完 check_spinning(tid) 决定是否注入提醒。
"""

import time
import threading
from collections import deque, defaultdict

# ── 落地类工具：调用即产生持久化/外部可见改变，= 有进展 ──
LANDING_TOOLS = {
    "write_file", "edit_file",
    "complete_node", "finish_task",
    "remember", "remember_knowledge",
    "important_matters_add", "important_matters_update", "important_matters_remove",
    "create_task", "insert_dag_node", "remove_dag_node", "update_node_deps",
    "dynamic_split", "start_node", "rework_subtree",
    "write_draft", "update_task_brief",
    "add_mindmap_node",
    "save_task_template",
    "report_blocker", "ask_question", "ask_user",
    "rule_add", "rule_update", "rule_delete",
}

# ── 探索类工具：只读/搜索/执行，无落地 ──
EXPLORE_TOOLS = {
    "web_search", "web_fetch",
    "read_file", "grep", "file_search",
    "bash", "run_python", "check_python",
    "discover_tools", "execute_advanced_tool",
    "code_ast_parse", "code_find_defs", "code_get_symbol",
    "trace_memory", "crystal_recall", "recall_by_topic", "check_memory_gaps",
    "expand_compressed", "get_task_dag", "get_mindmap", "get_perspective",
    "list_topics", "current_topic", "list_tools", "read_topic_messages",
    "book_list_pages", "book_read_page",
    "important_matters_list", "system_status",
}

# ── 参数 ──
WINDOW = 16          # 观察窗口大小（最近 N 次调用）
THRESHOLD = 13       # 窗口内探索类调用次数达到该值才可能触发
COOLDOWN = 600.0     # 两次提醒最小间隔（秒）
MAX_INJECTS = 2      # 每个话题最多注入几次（硬熔断，防自指死循环）

# ── 自指保护：窗口内出现这些串，说明模型正在调查"打转检测/提醒机制"本身，
#    此时绝不能再注入提醒（否则就是自己触发自己）──
SELF_REF_KEYWORDS = (
    "loop_detector", "打转检测", "打转提醒", "失去循环", "陷入循环",
    "check_spinning", "record_call", "spin_msg", "原地打转",
    "reminder", "提醒机制", "硬编码提醒",
)
SELF_REF_HITS = 3    # 窗口内自指证据达到该条数即判定为"在调查检测器"


def _classify(tool_name, args=None):
    """判断一次工具调用的类别：'landing' / 'explore' / 'neutral'。"""
    if tool_name == "execute_advanced_tool" and isinstance(args, dict):
        inner = args.get("name") or ""
        if inner in LANDING_TOOLS:
            return "landing"
        if inner in EXPLORE_TOOLS:
            return "explore"
    if tool_name in LANDING_TOOLS:
        return "landing"
    if tool_name in EXPLORE_TOOLS:
        return "explore"
    return "neutral"


def _self_ref_score(tool_name, args):
    """本次调用有多少'在调查检测器自身'的迹象。"""
    try:
        blob = str(tool_name) + " " + str(args)[:2000]
    except Exception:
        blob = str(tool_name)
    low = blob.lower()
    return sum(1 for kw in SELF_REF_KEYWORDS if kw.lower() in low)


class _Detector:
    """单个话题的打转状态。多话题隔离，互不污染。"""

    def __init__(self):
        self.buffer = deque(maxlen=WINDOW)
        self.last_fire = 0.0        # 上次提醒时间戳
        self.last_landing = 0.0     # 上次落地类调用时间戳
        self.inject_count = 0       # 已注入次数
        self.awaiting_landing = False  # 注入后等待一次落地，期间不再注入
        self.self_ref_scores = deque(maxlen=WINDOW)
        self.muted = False          # 判定在调查检测器自身 → 永久静默

    def record(self, tool_name, args=None):
        kind = _classify(tool_name, args)
        now = time.time()
        self.buffer.append(kind)
        self.self_ref_scores.append(_self_ref_score(tool_name, args))
        if kind == "landing":
            self.last_landing = now
            self.awaiting_landing = False   # 有进展了，解除等待
            self.muted = False

    def _is_self_investigating(self):
        return sum(1 for s in self.self_ref_scores if s > 0) >= SELF_REF_HITS

    def check(self):
        """返回提醒文案；不需要提醒时返回 None。"""
        if self.muted:
            return None
        buf = self.buffer
        if len(buf) < WINDOW:
            return None
        # 1) 硬熔断：本话题注入次数已用完
        if self.inject_count >= MAX_INJECTS:
            return None
        # 2) 自指保护：模型正在调查检测器/提醒机制本身 → 静默，别自己触发自己
        if self._is_self_investigating():
            self.muted = True
            return None
        # 3) 等落地：注入后必须出现一次落地调用，才允许再次注入
        if self.awaiting_landing:
            return None
        explore = sum(1 for k in buf if k == "explore")
        landing = sum(1 for k in buf if k == "landing")
        if explore < THRESHOLD:
            return None
        if landing > 0:
            return None
        now = time.time()
        if now - self.last_fire < COOLDOWN:
            return None
        self.last_fire = now
        self.inject_count += 1
        self.awaiting_landing = True
        return (
            "【系统自动提示 · 不是用户消息 · 不是新任务】\n"
            "你最近连续做了多次只读/搜索/执行操作，还没有产生落地产出"
            "（写文件、完成任务节点、沉淀记忆、向用户求助）。\n"
            "请自行判断：\n"
            "1) 如果确实卡住了（同一件事试了两次没结果），换思路，别换个姿势重试。\n"
            "2) 如果遇到权限/网络/工具失效这类阻塞，直接把问题抛给用户。\n"
            "3) 如果这是正常的长调研，忽略本提示继续即可。\n"
            "⚠️ 重要：本条由系统自动生成，**不是用户发来的消息**。"
            "请勿把它当成用户的新指令，**更不要去调查本提示的来源或机制**。"
            "继续你原本的任务即可。"
        )


_detectors = defaultdict(_Detector)
_lock = threading.Lock()


def record_call(topic_id, tool_name, args=None):
    """每次工具调用后记录。thread-safe，零异常外泄。"""
    try:
        with _lock:
            _detectors[topic_id].record(tool_name, args)
    except Exception:
        pass


def check_spinning(topic_id):
    """每轮工具执行完检查。返回提醒文案或 None。thread-safe，零异常外泄。"""
    try:
        with _lock:
            return _detectors[topic_id].check()
    except Exception:
        return None


def reset(topic_id):
    """话题结束时清空状态，释放内存。"""
    try:
        with _lock:
            _detectors.pop(topic_id, None)
    except Exception:
        pass
