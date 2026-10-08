# -*- coding: utf-8 -*-
"""
asset_lib.py — 资产索引层核心（库=索引，登记制，不搬文件）

设计共识（2026-09-19 与用户定稿）：
1. 库=索引不是容器：资产零搬动，库里只登记引用（路径+描述+标签+状态+来源任务）
2. 登记制入馆：不做全盘扫描，AI 在产出时提议、用户点头后写入
3. 分类双轨：Windows 文件夹=物理分类（库不建第二棵树），标签=跨盘横向聚合
4. 索引文件独立可搬：index.json 放在库目录下，整个库目录拷走即迁移
5. 入口唯一：前端 btnAsset 书架页；设置页只留"资产库位置"一项
"""
import json
import os
import re
import time
import uuid

from db import get_db

# meta 键：库位置（用户唯一可配项）
LIB_DIR_KEY = "asset_lib_dir"
DEFAULT_LIB_NAME = "assets"

_INDEX_FILE = "index.json"


# ── 库位置 ───────────────────────────────────────────

def get_lib_dir():
    """库目录：db meta 配置优先，默认 BASE_DIR/data/assets。"""
    db = get_db()
    configured = db.get_meta(LIB_DIR_KEY)
    if configured and str(configured).strip():
        return os.path.normpath(str(configured).strip())
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bigeyeZero 根
    return os.path.join(base, "data", DEFAULT_LIB_NAME)


def set_lib_dir(new_dir):
    """设置库位置。若新位置无 index.json 且旧位置有 → 自动拷贝过去（索引可搬走）。
    返回 (lib_dir, copied)"""
    new_dir = os.path.normpath(str(new_dir or "").strip())
    if not new_dir:
        raise ValueError("empty lib dir")
    old_dir = get_lib_dir()
    os.makedirs(new_dir, exist_ok=True)
    copied = False
    new_idx = os.path.join(new_dir, _INDEX_FILE)
    old_idx = os.path.join(old_dir, _INDEX_FILE)
    if not os.path.isfile(new_idx) and os.path.isfile(old_idx) \
            and os.path.normcase(old_dir) != os.path.normcase(new_dir):
        _copy_index(old_idx, new_idx)
        copied = True
    db = get_db()
    db.set_meta(LIB_DIR_KEY, new_dir)
    return new_dir, copied


def _copy_index(src, dst):
    """拷贝索引文件：合法 JSON 才拷，坏文件拒绝扩散。"""
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "assets" not in data:
            return
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 坏索引不拷贝，新位置从空白开始


# ── 索引读写 ─────────────────────────────────────────

def _index_path():
    return os.path.join(get_lib_dir(), _INDEX_FILE)


def _empty_index():
    return {"version": 1, "assets": []}


def load_index():
    """读索引；文件缺失/损坏时自动重建骨架（坏档不阻塞，返回空库+损坏标记由调用方决定）。"""
    p = _index_path()
    if not os.path.isfile(p):
        return _empty_index()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
            return _empty_index()
        return data
    except Exception:
        # 备份坏档再重建，数据不静默丢失
        try:
            os.replace(p, p + ".corrupt-%d" % int(time.time()))
        except Exception:
            pass
        return _empty_index()


def save_index(data):
    os.makedirs(get_lib_dir(), exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _index_path())  # 原子替换，断电不留半截文件


# ── 记录规范化 ───────────────────────────────────────

_TAG_RE = re.compile(r"^[\w\u4e00-\u9fff\- ]{1,20}$")


def _norm_tags(tags):
    """标签规范化：去重、去空、每条 ≤20 字符、≤12 个。"""
    out, seen = [], set()
    for t in (tags or []):
        t = str(t).strip()
        if not t or len(t) > 20:
            continue
        if not _TAG_RE.match(t):
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= 12:
            break
    return out


def _norm_path(path):
    if not path or not str(path).strip():
        raise ValueError("path is required")
    return os.path.normpath(str(path).strip())


def _norm_rec(rec):
    """补齐/规范化一条记录的字段。"""
    now = int(time.time())
    return {
        "id": str(rec.get("id") or uuid.uuid4().hex[:12]),
        "name": str(rec.get("name") or "").strip()[:60] or "未命名资产",
        "path": _norm_path(rec.get("path")),
        "type": rec.get("type") if rec.get("type") in ("project", "dir", "file") else "dir",
        "tags": _norm_tags(rec.get("tags")),
        "status": "archived" if rec.get("status") == "archived" else "active",
        "note": str(rec.get("note") or "").strip()[:300],
        "source_task": str(rec.get("source_task") or "").strip()[:40],
        "auto_publish": bool(rec.get("auto_publish", False)),
        "created_at": int(rec.get("created_at") or now),
        "updated_at": now,
    }


# ── CRUD ────────────────────────────────────────────

def list_assets():
    """全部记录 + 逐条路径校验（exists 字段）。标签聚合交给前端。"""
    data = load_index()
    out = []
    for rec in data["assets"]:
        r = dict(rec)
        try:
            r["exists"] = os.path.exists(r["path"])
        except Exception:
            r["exists"] = False
        out.append(r)
    # 活跃在前，其余按更新时间倒序
    out.sort(key=lambda x: (x["status"] != "active", -x.get("updated_at", 0)))
    return out


def _count_dir_files(path):
    """⑱ 目录登记：数目录一层内的文件数（不递归平铺）。"""
    try:
        return sum(1 for e in os.scandir(path) if e.is_file())
    except Exception:
        return 0


def _explicit_children(path):
    """⑱ 目录登记：显式列出子目录名（只报名字，不平铺登记）。"""
    try:
        return [e.name for e in os.scandir(path) if e.is_dir()][:80]
    except Exception:
        return []


def add_asset(fields):
    """登记新资产。同路径（Windows 大小写不敏感）拒绝重复登记。"""
    rec = _norm_rec(fields)
    if rec.get("type") == "dir":
        rec["dir_file_count"] = _count_dir_files(rec["path"])
        rec["dir_children"] = _explicit_children(rec["path"])
    data = load_index()
    key = os.path.normcase(rec["path"])
    if rec.get("type") == "dir":
        # ⑱ 同目录重复登记 → 视为已入库（幂等），不报错
        for old in data["assets"]:
            if os.path.normcase(old.get("path", "")) == key:
                return dict(old, merged=True)
        # ⑱ 就近归属去重：库内已有资产落在本目录内 → 旧登记被吸收（删旧保新，备注留痕）
        absorbed = []
        for old in list(data["assets"]):
            okey = os.path.normcase(old.get("path", ""))
            if okey and okey != key and (okey.startswith(key + os.sep) or okey.startswith(key + "\\") or okey.startswith(key + "/")):
                absorbed.append(old)
        for old in absorbed:
            data["assets"] = [a for a in data["assets"] if a.get("id") != old.get("id")]
        if absorbed:
            rec["note"] = ("就近归属，吸收登记: " + "、".join((o.get("name") or "") for o in absorbed))[:200]
        # ⑱ 父目录已在库 → 本子目录仍显式登记（不并入父），仅备注提示
        pkey = os.path.normcase(os.path.dirname(rec["path"].rstrip("\\/")))
        for old in data["assets"]:
            if old.get("type") == "dir" and os.path.normcase(old.get("path", "")) == pkey:
                rec["note"] = ((rec["note"] + "；") if rec.get("note") else "") + ("父目录「%s」已登记，本条为显式子目录登记" % old.get("name", ""))
                break
    for old in data["assets"]:
        if os.path.normcase(old.get("path", "")) == key:
            raise ValueError("已登记过: %s" % old.get("name"))
    data["assets"].append(rec)
    save_index(data)
    return rec


def update_asset(asset_id, fields):
    """按 id 更新，None 字段不动。"""
    data = load_index()
    for i, old in enumerate(data["assets"]):
        if old.get("id") == asset_id:
            merged = dict(old)
            for k in ("name", "type", "status", "note", "source_task"):
                if fields.get(k) is not None:
                    merged[k] = fields[k]
            if fields.get("auto_publish") is not None:
                merged["auto_publish"] = bool(fields["auto_publish"])
            if fields.get("path") is not None:
                merged["path"] = _norm_path(fields["path"])
            if fields.get("tags") is not None:
                merged["tags"] = _norm_tags(fields["tags"])
            merged = _norm_rec(merged)
            if merged.get("type") == "dir":
                merged["dir_file_count"] = _count_dir_files(merged.get("path", ""))
                merged["dir_children"] = _explicit_children(merged.get("path", ""))
            merged["id"] = asset_id
            merged["created_at"] = old.get("created_at", merged["created_at"])
            data["assets"][i] = merged
            save_index(data)
            return merged
    return None


def delete_asset(asset_id):
    """删登记不删文件（库=索引，物理文件永远不动）。"""
    data = load_index()
    before = len(data["assets"])
    data["assets"] = [a for a in data["assets"] if a.get("id") != asset_id]
    if len(data["assets"]) == before:
        return False
    save_index(data)
    return True


def find_by_path(path):
    """路径反查登记（AI 产出时判断是否已入馆用）。"""
    key = os.path.normcase(_norm_path(path))
    for a in load_index()["assets"]:
        if os.path.normcase(a.get("path", "")) == key:
            return a
    return None


def all_tags():
    """标签清单（含计数），书架页标签栏用。"""
    cnt = {}
    for a in load_index()["assets"]:
        for t in a.get("tags", []):
            cnt[t] = cnt.get(t, 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])


# ── AI 工具注册（供对话流调用）────────────────────────

from tools.registry import register_tool


@register_tool(
    name="asset_register",
    description="把一个路径登记进资产库（库=索引，不搬文件，文件原地不动）。产出任务成果后提议用户入库用；同路径重复登记会报错。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "资产绝对路径（文件或目录）"},
            "name": {"type": "string", "description": "显示名，默认取路径末段"},
            "type": {"type": "string", "enum": ["project", "dir", "file"], "description": "project=整个项目文件夹"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表，最多12个，如 合同/2026"},
            "note": {"type": "string", "description": "一句话描述"},
        },
        "required": ["path"],
    },
    label="登记资产",
)
def asset_register(path, name=None, type="dir", tags=None, note="", source_task=""):
    """登记资产。source_task 尽量由调用方传入当前任务 id。"""
    if not name:
        name = os.path.basename(str(path).rstrip("\\/")) or str(path)
    rec = add_asset({"path": path, "name": name, "type": type, "tags": tags,
                     "note": note, "source_task": source_task})
    return "已登记: %s → %s (id=%s, tags=%s)" % (rec["name"], rec["path"], rec["id"], rec["tags"])


@register_tool(
    name="asset_find",
    description="按路径或标签查资产库，判断某路径是否已入馆、或列出同标签资产。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要反查的路径（可选）"},
            "tag": {"type": "string", "description": "按标签过滤（可选）"},
        },
    },
    label="查资产库",
)
def asset_find(path=None, tag=None):
    if path:
        a = find_by_path(path)
        if a:
            return "已登记: %s (id=%s, tags=%s, status=%s)" % (a["name"], a["id"], a["tags"], a["status"])
        return "未登记: %s" % path
    assets = list_assets()
    if tag:
        assets = [a for a in assets if tag in (a.get("tags") or [])]
    return json.dumps([{"id": a["id"], "name": a["name"], "path": a["path"], "tags": a["tags"],
                        "status": a["status"], "exists": a["exists"]} for a in assets], ensure_ascii=False, indent=1)


@register_tool(
    name="asset_update",
    description="更新资产库登记：改标签/状态(active/archived)/备注/路径（文件搬家后修复路径用），或删除登记。文件本体永远不动。",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "登记 id（asset_find 可查）"},
            "path": {"type": "string", "description": "新路径（修复失联用）"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "替换全部标签"},
            "status": {"type": "string", "enum": ["active", "archived"], "description": "归档状态"},
            "note": {"type": "string", "description": "备注"},
            "delete": {"type": "boolean", "description": "true=删除这条登记（不动文件）"},
        },
        "required": ["id"],
    },
    label="更新资产",
)
def asset_update(id, path=None, tags=None, status=None, note=None, delete=False):
    if delete:
        ok = delete_asset(id)
        return "已删除登记 %s" % id if ok else "未找到 id=%s" % id
    rec = update_asset(id, {"path": path, "tags": tags, "status": status, "note": note})
    if not rec:
        return "未找到 id=%s" % id
    return "已更新: %s (path=%s, tags=%s, status=%s)" % (rec["name"], rec["path"], rec["tags"], rec["status"])
