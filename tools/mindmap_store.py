# -*- coding: utf-8 -*-
"""
mindmap_store.py — 思维导图多对象存储层（工作区可挂多张导图）

设计共识（2026-09-19 UI新格局定稿）：
1. 导图是工作区对象，一个工作区可有多张，每张独立命名
2. 存储：data/missions/{tid}/mindmaps/{map_id}.json + index.json 登记表
3. 旧数据自动迁移：首次访问时 legacy mindmap.json → mindmaps/m_primary.json（复制不搬动，
   原文件保留作前端旧读通道镜像，前端切换由节点⑤⑥负责）
4. mindmap_tools.py（主图+历史+undo/redo）不动；本层管多对象，AI 工具接线在节点⑤
5. 窗口引用：canvas 窗口 ref={"map_id": ...} → 本层 load_map
"""
import json
import os
import time
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bigeyeZero 根

INDEX_FILE = "index.json"
PRIMARY_ID = "m_primary"
MAX_TITLE = 120


def _maps_dir(tid):
    tid = str(tid or "").strip()
    if not tid or "/" in tid or "\\" in tid or ".." in tid:
        raise ValueError("非法话题ID")
    return os.path.join(BASE_DIR, "data", "missions", tid, "mindmaps")


def _legacy_path(tid):
    return os.path.join(BASE_DIR, "data", "missions", tid, "mindmap.json")


def _index_path(tid):
    return os.path.join(_maps_dir(tid), INDEX_FILE)


# ── 登记表 ───────────────────────────────────────────

def _load_index(tid):
    """读登记表；首次访问或空表且legacy存在时触发旧数据迁移。"""
    p = _index_path(tid)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                idx = json.load(f)
            if isinstance(idx, dict) and isinstance(idx.get("maps"), list):
                # 增量迁移：登记表为空但 legacy 镜像出现过 → 收编（幂等）
                if not idx.get("maps") and os.path.exists(_legacy_path(tid)):
                    return _migrate_legacy(tid)
                return idx
        except Exception:
            pass  # 坏登记表 → 走重建
    return _migrate_legacy(tid)


def _save_index(tid, idx):
    idx["updated_at"] = time.time()
    p = _index_path(tid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _migrate_legacy(tid):
    """旧单图 mindmap.json → 多对象存储（复制为主图，原文件保留）。幂等。"""
    idx = {"maps": [], "migrated": False}
    legacy = _legacy_path(tid)
    if os.path.exists(legacy):
        try:
            with open(legacy, encoding="utf-8") as f:
                body = json.load(f)
            if isinstance(body, dict) and isinstance(body.get("nodes"), list):
                entry = {"id": PRIMARY_ID, "title": "主导图",
                         "created_at": time.time(), "updated_at": time.time()}
                _write_map(tid, PRIMARY_ID, body)
                idx["maps"].append(entry)
        except Exception:
            pass  # 旧文件坏档：跳过迁移，从空库开始（原文件留着不背走）
    idx["migrated"] = True
    _save_index(tid, idx)
    return idx


# ── 单图文件 ─────────────────────────────────────────

def _map_path(tid, map_id):
    if not map_id or not str(map_id).replace("_", "").replace("-", "").isalnum():
        raise ValueError("非法导图ID")
    return os.path.join(_maps_dir(tid), str(map_id) + ".json")


def _write_map(tid, map_id, body):
    p = _map_path(tid, map_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def load_map(tid, map_id, create_if_missing=False):
    """读某张导图。不存在：默认建空图或抛错。坏档：备份.corrupt后按缺失处理。"""
    p = _map_path(tid, map_id)  # 非法ID先拦（ValueError）
    idx = _load_index(tid)
    entry = next((m for m in idx["maps"] if m["id"] == map_id), None)
    if entry is None and not create_if_missing:
        raise KeyError("导图不存在: " + str(map_id))
    p = _map_path(tid, map_id)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                body = json.load(f)
            if isinstance(body, dict) and isinstance(body.get("nodes"), list):
                return body
        except Exception:
            try:
                os.replace(p, p + ".corrupt")
            except Exception:
                pass
    empty = {"id": map_id, "nodes": [], "edges": [], "meta": {}}
    if entry is not None or create_if_missing:
        _write_map(tid, map_id, empty)
        if entry is None:
            idx["maps"].append({"id": map_id, "title": "", "created_at": time.time(),
                                "updated_at": time.time()})
            _save_index(tid, idx)
    return empty


def save_map(tid, map_id, body):
    """整包保存某张导图（校验+登记表时间戳更新+原子写）。"""
    if not isinstance(body, dict) or not isinstance(body.get("nodes"), list) \
            or not isinstance(body.get("edges", []), list):
        raise ValueError("导图必须含 nodes/edges 列表")
    body["id"] = map_id
    _write_map(tid, map_id, body)
    idx = _load_index(tid)
    entry = next((m for m in idx["maps"] if m["id"] == map_id), None)
    if entry is None:
        entry = {"id": map_id, "title": "", "created_at": time.time()}
        idx["maps"].append(entry)
    entry["updated_at"] = time.time()
    title = str(body.get("meta", {}).get("title") or "").strip()
    if title:
        entry["title"] = title[:MAX_TITLE]
    _save_index(tid, idx)
    return body


# ── 对象级 CRUD ──────────────────────────────────────

def list_maps(tid):
    """登记表快照：[{id,title,created_at,updated_at,node_count}]。"""
    idx = _load_index(tid)
    out = []
    for m in idx["maps"]:
        info = dict(m)
        try:
            body = load_map(tid, m["id"])
            info["node_count"] = len(body.get("nodes", []))
        except Exception:
            info["node_count"] = None
        out.append(info)
    return out


def create_map(tid, title=""):
    """新建空导图，返回登记项。"""
    map_id = "m_" + uuid.uuid4().hex[:8]
    idx = _load_index(tid)
    entry = {"id": map_id, "title": str(title or "").strip()[:MAX_TITLE],
             "created_at": time.time(), "updated_at": time.time()}
    idx["maps"].append(entry)
    _write_map(tid, map_id, {"id": map_id, "nodes": [], "edges": [], "meta": {"title": entry["title"]}})
    _save_index(tid, idx)
    return entry


def rename_map(tid, map_id, title):
    idx = _load_index(tid)
    entry = next((m for m in idx["maps"] if m["id"] == map_id), None)
    if entry is None:
        raise KeyError("导图不存在: " + str(map_id))
    entry["title"] = str(title or "").strip()[:MAX_TITLE]
    entry["updated_at"] = time.time()
    _save_index(tid, idx)
    return entry


def delete_map(tid, map_id):
    """删导图（文件+登记）。主导图允许删（工作区可以一张图都没有）。"""
    idx = _load_index(tid)
    before = len(idx["maps"])
    idx["maps"] = [m for m in idx["maps"] if m["id"] != map_id]
    if len(idx["maps"]) == before:
        raise KeyError("导图不存在: " + str(map_id))
    _save_index(tid, idx)
    try:
        os.remove(_map_path(tid, map_id))
    except OSError:
        pass
    return True


# ── 节点/边操作（任意图；AI 工具接线在节点⑤） ─────────

def _new_node_id():
    return "m_" + uuid.uuid4().hex[:6]


def add_node(tid, map_id, text, ntype="idea", note="", parent_id="", edge_label=""):
    body = load_map(tid, map_id)
    node = {"id": _new_node_id(), "text": str(text or "").strip(),
            "type": ntype if ntype in ("idea", "decision", "question") else "idea",
            "note": str(note or "")}
    body["nodes"].append(node)
    if parent_id:
        if not any(n["id"] == parent_id for n in body["nodes"]):
            raise KeyError("父节点不存在: " + str(parent_id))
        body.setdefault("edges", []).append({
            "id": "e_" + uuid.uuid4().hex[:6], "source": parent_id,
            "target": node["id"], "label": str(edge_label or "")})
    body.setdefault("meta", {}).setdefault("updated", time.time())
    save_map(tid, map_id, body)
    return node


def remove_node(tid, map_id, node_id):
    body = load_map(tid, map_id)
    if not any(n["id"] == node_id for n in body["nodes"]):
        raise KeyError("节点不存在: " + str(node_id))
    body["nodes"] = [n for n in body["nodes"] if n["id"] != node_id]
    body["edges"] = [e for e in body.get("edges", [])
                     if e.get("source") != node_id and e.get("target") != node_id]
    save_map(tid, map_id, body)
    return True
