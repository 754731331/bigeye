"""Task-scoped runtime snapshots. Never mutate global provider configuration.

注意：本文件是源码仓库缺失、从 release v1.6 打包版（server.exe 内 PYZ 字节码）
反汇编还原得到。字节码含行号/变量名/常量，逻辑按原样还原，仅补充注释。
"""
import copy
import json
import threading
import time

_lock = threading.RLock()
_usage = {}


def load_selection(db, topic_id):
    """从 db.topic_meta 读该任务保存的模型选择（JSON），无则返回 {}。"""
    raw = db.get_topic_meta(topic_id, "model_selection") if topic_id else None

    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def save_selection(db, topic_id, provider, model):
    """把某任务的模型选择持久化到 db.topic_meta，返回 {"provider", "model"}。"""
    if not (topic_id and db.get_topic(topic_id)):
        raise ValueError("Unknown task")
    if not (provider and model):
        raise ValueError("Provider and model are required")
    value = {"provider": str(provider), "model": str(model)}
    db.set_topic_meta(topic_id, "model_selection", json.dumps(value))
    return value


def snapshot(default_config, selection, resolve_provider):
    """返回某任务最终使用的配置：深拷贝全局配置，再叠加任务级模型选择。

    selection 存在时，用 resolve_provider(provider, model, cfg) 得到解析结果，
    经 clone_with 只更新 LLMConfig 已有字段，避免污染全局 provider 配置。
    """
    cfg = copy.deepcopy(default_config)
    if selection:
        resolved = resolve_provider(selection["provider"], selection["model"], cfg)
        cfg = cfg.clone_with(**resolved)
    return cfg


def begin(topic_id, model, budget):
    """开始一轮任务：保留已有历史，但峰值归零并写入新 model/budget。"""
    with _lock:
        previous = _usage.get(topic_id, {})
        _usage[topic_id] = dict(previous, peak=0, model=model, budget=budget)


def record(topic_id, input_tokens, model, budget):
    """记录一轮 input token 使用，返回该任务运行时的深拷贝快照。"""
    with _lock:
        state = _usage.setdefault(topic_id, {})
        recent = (state.get("recent", []) + [int(input_tokens)])[-10:]
        state.update(
            latest=int(input_tokens),
            recent=recent,
            peak=max(state.get("peak", 0), int(input_tokens)),
            model=model,
            budget=budget,
            updated_at=time.time(),
        )
        return copy.deepcopy(state)


def usage(topic_id):
    """返回该任务的运行时使用快照（latest/recent/peak/model/budget/updated_at）。"""
    with _lock:
        return copy.deepcopy(_usage.get(topic_id, {}))
