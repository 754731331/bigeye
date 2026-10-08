# -*- coding: utf-8 -*-
"""task_context — 任务运行时上下文通道。

【为什么需要它】
工具层过去用 `db.get_active_topic_id()` 反推"当前任务"。那是全局单例指针，
单任务时代没问题；多任务并行/切换窗口时，全局指针只指向一个任务，
其他任务的 agent 循环调用工具就会读错、写错归属。

已确认事故：
  - ask_user 把提问卡片弹进了另一个任务的窗口（tools/ask_user.py L76）
  - 工具层 30+ 处 get_active_topic_id() 都是潜在串台点

【方案】
agent 循环每轮在自己的线程里显式 `set_current_topic(tid)`，工具层优先读它。
没设置时回落到全局活跃话题 —— 保持脚本、定时任务、后台线程等无请求上下文
场景下的旧行为不变。

【线程模型】
server 用 ThreadingHTTPServer，一个请求一个线程，agent 循环与工具调用同线程
（execute_tool 内联调用），所以 threading.local 就够，无需 contextvars。
与 tools/bash.py 的 set_runtime_context 是同一套模式。

【边界】
- 只解决"读对归属"，不解决业务层的并发写冲突。
- keep-alive 下线程会被复用，所以任务结束必须清理（clear_working 里已挂）。
"""
import threading

from .registry import stable_local

# ⚠️ 必须用 stable_local：hot_reload 会把 tools/*.py 全部 reload，
# 模块级 threading.local() 会被重建 → 所有在跑的循环当场丢绑定 → 串任务。
# 详见 tools/registry.stable_local 的说明与 2026-09-24 的实证。
_local = stable_local("task_context")


def set_current_topic(tid):
    """agent 循环入口调用：把本线程绑定到某个任务。返回是否设置成功。"""
    tid = (tid or "").strip() if isinstance(tid, str) else tid
    _local.topic_id = tid or None
    return bool(_local.topic_id)


def get_current_topic(default=None):
    """工具层读当前任务归属。

    优先级：本线程显式绑定 > 全局活跃话题 > default。
    全局兜底是为了兼容无请求上下文的调用方（脚本/定时任务/后台线程）。
    """
    tid = getattr(_local, "topic_id", None)
    if tid:
        return tid
    try:
        from db import get_db
        tid = get_db().get_active_topic_id()
        if tid:
            # 回落不再静默：全局活跃任务 = "用户此刻点开的任务"，和"我这条循环属于谁"
            # 是两回事。这里出现过一次真实的串任务事故（见文件头），所以出声。
            _warn_fallback(tid)
            return tid
    except Exception:
        pass
    return default


def _warn_fallback(tid):
    """回落全局活跃任务时打一条限流日志（每分钟最多一条）。"""
    try:
        import builtins
        import time
        now = time.time()
        if now - getattr(builtins, "_dayan_ctx_fallback_at", 0.0) < 60:
            return
        builtins._dayan_ctx_fallback_at = now
        print(f"[task_context] ⚠ 本线程没有任务绑定，回落到全局活跃任务 {str(tid)[:8]}"
              f" —— 若是 agent 循环内，说明绑定被 hot_reload 或异常清掉了")
    except Exception:
        pass


def clear_current_topic():
    """清空本线程绑定（任务生命周期结束时调用）。"""
    _local.topic_id = None


def clear_if_matches(tid):
    """仅当本线程恰好绑定在 tid 上时才清，避免误清其它任务。"""
    if getattr(_local, "topic_id", None) == tid:
        clear_current_topic()


def has_explicit_context():
    """本线程是否显式绑定了任务（供排查/测试用）。"""
    return bool(getattr(_local, "topic_id", None))


def explicit_topic():
    """本线程显式绑定的任务（**不回落全局**）。无绑定返回 None。

    给 agent 循环每轮自检用：`explicit_topic() != 本轮 tid` 就说明绑定丢了
    （hot_reload 清掉）或被别的请求残留成了另一个任务（keep-alive 复用线程），
    此时必须重建绑定，否则工具层会用错归属 —— 2026-09-24 串任务事故的根因。
    """
    return getattr(_local, "topic_id", None)
