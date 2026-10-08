# -*- coding: utf-8 -*-
"""
canvas_tools.py — AI 画布工具（工作区画布的 AI 写通道）

节点⑤落地：AI 可以读画布布局、摆窗口（导图/文件/草稿纸/临时件）、
改窗口位置、收窗口、往任意导图加节点。用户说"把 xx 摊开给我看"时用。
"""
import os

from tools.registry import register_tool
import tools.canvas as canvas_store

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tid(topic_id=""):
    if topic_id:
        return topic_id
    # 归属优先走任务上下文通道（本线程绑定）；全局活跃任务只是无绑定时的兜底
    try:
        from tools.task_context import get_current_topic
        tid = get_current_topic()
        if tid:
            return tid
    except Exception:
        pass
    from db import get_db
    return get_db().get_active_topic_id()


@register_tool(
    name="canvas_layout",
    description="读当前工作区画布的窗口布局（每个窗口的类型/标题/位置/大小/内容指针）。",
    parameters={
        "type": "object",
        "properties": {"topic_id": {"type": "string", "description": "话题ID，缺省用当前任务"}},
    },
)
def canvas_layout(topic_id: str = ""):
    tid = _tid(topic_id)
    if not tid:
        return {"error": "没有活跃任务"}
    return canvas_store.load_canvas(tid)


@register_tool(
    name="canvas_add_window",
    description="在工作区画布上摆一个窗口。type=mindmap(导图,ref传map_id)/file(文件浏览,ref传path相对工作区)/draft(任务草稿纸)/temp(临时文本件,ref可传text)。返回带id的窗口记录。",
    parameters={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["mindmap", "file", "draft", "temp"]},
            "title": {"type": "string"},
            "x": {"type": "number"}, "y": {"type": "number"},
            "w": {"type": "number"}, "h": {"type": "number"},
            "ref": {"type": "object", "description": "内容指针：mindmap传{map_id}, file传{path}, temp传{text}"},
            "topic_id": {"type": "string", "description": "缺省用当前任务"},
        },
        "required": ["type"],
    },
)
def canvas_add_window(type: str, title: str = "", x=40, y=40, w=420, h=320, ref=None, topic_id: str = ""):
    tid = _tid(topic_id)
    if not tid:
        return {"error": "没有活跃任务"}
    win = canvas_store.add_window(tid, {"type": type, "title": title,
                                        "x": x, "y": y, "w": w, "h": h, "ref": ref})
    return {"window": win}


@register_tool(
    name="canvas_update_window",
    description="更新画布上某个窗口（id 必填，patch 给 x/y/w/h/title/ref 任意组合）。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "patch": {"type": "object"},
            "topic_id": {"type": "string"},
        },
        "required": ["id"],
    },
)
def canvas_update_window(id: str, patch=None, topic_id: str = ""):
    tid = _tid(topic_id)
    if not tid:
        return {"error": "没有活跃任务"}
    try:
        return {"window": canvas_store.update_window(tid, id, patch or {})}
    except KeyError as e:
        return {"error": str(e)}


@register_tool(
    name="canvas_remove_window",
    description="从画布上收掉一个窗口（布局项移除；导图等对象本身不删）。",
    parameters={
        "type": "object",
        "properties": {"id": {"type": "string"}, "topic_id": {"type": "string"}},
        "required": ["id"],
    },
)
def canvas_remove_window(id: str, topic_id: str = ""):
    tid = _tid(topic_id)
    if not tid:
        return {"error": "没有活跃任务"}
    try:
        canvas_store.remove_window(tid, id)
        return {"ok": True}
    except KeyError as e:
        return {"error": str(e)}


@register_tool(
    name="mindmap_add_node",
    description="往工作区的某张导图加节点（map_id 缺省=主导图；parent_id 传父节点则自动连线）。配合 canvas_add_window(type=mindmap) 可把导图摆上画布。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "type": {"type": "string", "enum": ["idea", "decision", "question"]},
            "note": {"type": "string"},
            "map_id": {"type": "string", "description": "缺省=主导图 m_primary"},
            "parent_id": {"type": "string"},
            "topic_id": {"type": "string"},
        },
        "required": ["text"],
    },
)
def mindmap_add_node(text: str, type: str = "idea", note: str = "",
                     map_id: str = "", parent_id: str = "", topic_id: str = ""):
    tid = _tid(topic_id)
    if not tid:
        return {"error": "没有活跃任务"}
    from tools import mindmap_store
    mid = map_id or mindmap_store.PRIMARY_ID
    try:
        node = mindmap_store.add_node(tid, mid, text, ntype=type, note=note, parent_id=parent_id)
        return {"node": node, "map_id": mid}
    except (KeyError, ValueError) as e:
        return {"error": str(e)}
