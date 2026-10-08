# -*- coding: utf-8 -*-
"""
asset_v3.py — 资产库 v3：混合仓（实体入库 + 引用登记）

v3 定稿（2026-09-22 与用户对齐）：
1. 混合仓：笔记/速记/链接 = 实体住库里；大文件/任务文件 = 记引用不搬家（>10MB 不拷贝）
2. 库目录结构（在资产库根下）：
       notes/    笔记（markdown，YAML frontmatter）
       snippets/ 速记（纯文本碎片）
       links/    链接收藏（markdown 存 URL+摘要）
       files/    收纳的实体文件（≤10MB 拷贝入库，按年月分目录；md5 去重）
3. 极低记录成本：所有写操作一步完成，自动起标题，不弹窗打断
4. 路径安全：所有读写限制在库目录内（防穿越）
5. 复用 asset_lib 的索引与登记制：入库的实体同时登记进 index.json，
   保持"库里能看到一切"的单一口径
"""
import hashlib
import json
import os
import re
import time

import tools.asset_lib as asset_lib

# 收纳实体文件的体积阈值：≤10MB 拷贝入库，超过只登记引用
COPY_MAX_BYTES = 10 * 1024 * 1024

_SUBDIRS = ("notes", "snippets", "links", "files")


# ── 基础 ─────────────────────────────────────────────

def _lib():
    d = asset_lib.get_lib_dir()
    for s in _SUBDIRS:
        os.makedirs(os.path.join(d, s), exist_ok=True)
    return d


def _safe_rel(rel):
    """校验相对路径不越出库目录，返回绝对路径。"""
    lib = os.path.normpath(_lib())
    p = os.path.normpath(os.path.join(lib, str(rel or "")))
    if not (p == lib or p.startswith(lib + os.sep)):
        raise ValueError("path escapes library")
    return p


def _slug(title, fallback="untitled"):
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(title or "").strip())
    t = re.sub(r"\s+", " ", t).strip(" ._")
    t = t[:60] or fallback
    return t


def _uniq(dirpath, name):
    """目录内重名自动加序号：a.md → a-2.md"""
    base, ext = os.path.splitext(name)
    cand = name
    i = 2
    while os.path.exists(os.path.join(dirpath, cand)):
        cand = "%s-%d%s" % (base, i, ext)
        i += 1
    return cand


def _frontmatter(title, tags, source_task):
    lines = ["---",
             "title: %s" % title,
             "created: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "tags: %s" % (", ".join(tags) if tags else ""),
             "source_task: %s" % (source_task or ""),
             "---", ""]
    return "\n".join(lines)


def _register(path, name, kind, tags, source_task):
    """实体入库后登记进 index.json（复用登记制，库里单一口径可见）。"""
    try:
        return asset_lib.add_asset({
            "path": path, "name": name, "type": kind,
            "tags": list(tags or []), "source_task": source_task or "",
        })
    except Exception:
        return None  # 登记失败不阻塞写入（文件本身已落盘）


def _touch(path):
    """更新既有登记的 updated_at（保存笔记时用），找不到登记就静默跳过。"""
    try:
        recs = asset_lib.list_assets()
        norm = os.path.normcase(os.path.normpath(path))
        for a in recs:
            if os.path.normcase(os.path.normpath(a.get("path") or "")) == norm:
                asset_lib.update_asset(a["id"], {"updated_at": int(time.time())})
                return
    except Exception:
        pass


# ── 笔记 ─────────────────────────────────────────────

def new_note(title, body="", tags=None, source_task=""):
    title = _slug(title, "新笔记")
    d = os.path.join(_lib(), "notes")
    fp = os.path.join(d, _uniq(d, title + ".md"))
    content = _frontmatter(title, tags, source_task) + (body or "")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    _register(fp, title, "note", tags, source_task)
    return {"path": fp, "name": title, "title": title}


def new_snippet(text, tags=None, source_task=""):
    text = str(text or "").strip()
    if not text:
        raise ValueError("empty snippet")
    title = _slug(text.split("\n")[0][:24], "速记")
    d = os.path.join(_lib(), "snippets")
    fp = os.path.join(d, _uniq(d, time.strftime("%m%d-%H%M%S") + "-" + title + ".txt"))
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)
    _register(fp, title, "snippet", tags, source_task)
    return {"path": fp, "name": title, "title": title}


def new_link(url, note="", tags=None, source_task=""):
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("not a http(s) url")
    host = re.match(r"^https?://([^/]+)", url, re.I).group(1)
    title = _slug(note.split("\n")[0][:30] if note else host, host)
    d = os.path.join(_lib(), "links")
    fp = os.path.join(d, _uniq(d, title + ".md"))
    content = _frontmatter(title, tags, source_task) + \
        "- 链接: %s\n- 来源: %s\n\n%s\n" % (url, host, note or "")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    _register(fp, title, "link", tags, source_task)
    return {"path": fp, "name": title, "title": title, "url": url}


# ── 文件收纳（混合仓的关键分支） ─────────────────────

def _hashmap_path():
    return os.path.join(_lib(), "files", ".hashes.json")


def _load_hashes():
    try:
        with open(_hashmap_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def import_file(src, tags=None, source_task=""):
    """收纳入口：≤10MB 拷贝入库（md5 去重）；>10MB 只登记引用不搬家。
    返回 {moved, path, name, dedup}"""
    src = os.path.normpath(str(src or ""))
    if not os.path.isfile(src):
        raise ValueError("file not found: %s" % src)
    name = os.path.basename(src)
    size = os.path.getsize(src)
    if size > COPY_MAX_BYTES:
        # 大文件：记引用，原地不动
        _register(src, name, "file", tags, source_task)
        return {"moved": False, "path": src, "name": name, "dedup": False}
    with open(src, "rb") as f:
        h = hashlib.md5(f.read()).hexdigest()
    hm = _load_hashes()
    if h in hm and os.path.isfile(os.path.join(_lib(), hm[h])):
        # 同内容文件已入库：直接指向已有副本，不重复占空间
        rel = hm[h]
        _register(os.path.join(_lib(), rel), name, "file", tags, source_task)
        return {"moved": True, "path": os.path.join(_lib(), rel),
                "name": name, "dedup": True}
    sub = time.strftime("%Y-%m")
    d = os.path.join(_lib(), "files", sub)
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, _uniq(d, name))
    try:
        with open(src, "rb") as s, open(fp, "wb") as t:
            while True:
                buf = s.read(1 << 20)
                if not buf:
                    break
                t.write(buf)
    except Exception:
        try:
            os.remove(fp)
        except OSError:
            pass
        raise
    rel = os.path.relpath(fp, _lib())
    hm[h] = rel
    try:
        with open(_hashmap_path(), "w", encoding="utf-8") as f:
            json.dump(hm, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    _register(fp, os.path.splitext(name)[0], "file", tags, source_task)
    return {"moved": True, "path": fp, "name": name, "dedup": False}


# ── 读取 / 保存（库内安全读写） ───────────────────────

# ── 浏览（库=能逛的仓库） ─────────────────────────

def _walk_files(d):
    out = []
    for root, _dirs, names in os.walk(d):
        for n in names:
            fp = os.path.join(root, n)
            try:
                out.append({"name": n, "path": fp, "mtime": int(os.path.getmtime(fp))})
            except OSError:
                pass
    return out


def lib_overview():
    """库总览：四个分区各自的计数与最近条目（库页首屏）。"""
    lib = _lib()
    out = {"lib_dir": lib, "sections": {}}
    labels = (("notes", "笔记"), ("snippets", "速记"), ("links", "链接"), ("files", "文件"))
    for sub, label in labels:
        d = os.path.join(lib, sub)
        items = _walk_files(d)
        items.sort(key=lambda x: -x["mtime"])
        out["sections"][sub] = {"label": label, "count": len(items), "items": items[:8]}
    return out


def browse(sub, rel=""):
    """浏览某分区的子目录（files/ 下按年月分层，需要能进子目录）。"""
    if sub not in _SUBDIRS:
        raise ValueError("bad section: %s" % sub)
    base = os.path.normpath(os.path.join(_lib(), sub))
    d = os.path.normpath(os.path.join(base, str(rel or "")))
    if not (d == base or d.startswith(base + os.sep)):
        raise ValueError("path escapes section")
    files, dirs = [], []
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        raise ValueError(str(e))
    for n in names:
        fp = os.path.join(d, n)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        item = {"name": n, "path": fp, "mtime": int(st.st_mtime)}
        if os.path.isdir(fp):
            dirs.append(item)
        else:
            item["size"] = st.st_size
            files.append(item)
    return {"sub": sub, "path": os.path.relpath(d, base) if d != base else "",
            "dirs": dirs, "files": files}


def read_text(rel_or_abs):
    fp = _safe_rel(rel_or_abs)
    if not os.path.isfile(fp):
        raise ValueError("not a file")
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def save_text(rel_or_abs, body):
    fp = _safe_rel(rel_or_abs)
    if not os.path.isfile(fp):
        raise ValueError("not a library file")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(str(body or ""))
    _touch(fp)
    return {"path": fp, "saved_at": int(time.time())}


def delete_entry(rel_or_abs):
    """删除库内实体（仅限库目录内的文件），同时从索引移除登记。"""
    fp = _safe_rel(rel_or_abs)
    lib = os.path.normpath(_lib())
    rel = os.path.relpath(fp, lib)
    if rel.split(os.sep)[0] == "files" and os.path.isfile(fp):
        # files/ 下的实体：顺手清 hash 表引用
        try:
            hm = _load_hashes()
            hit = [k for k, v in hm.items()
                   if os.path.normcase(v) == os.path.normcase(rel)]
            for k in hit:
                del hm[k]
            if hit:
                with open(_hashmap_path(), "w", encoding="utf-8") as f:
                    json.dump(hm, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    if not os.path.isfile(fp):
        raise ValueError("not a file")
    os.remove(fp)
    try:
        recs = asset_lib.list_assets()
        norm = os.path.normcase(fp)
        for a in recs:
            if os.path.normcase(os.path.normpath(a.get("path") or "")) == norm:
                asset_lib.delete_asset(a["id"])
                break
    except Exception:
        pass
    return {"deleted": fp}
