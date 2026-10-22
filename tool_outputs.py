'''有界结果引用 — 超限工具输出的落盘存储（改造②）

【为什么要有这个模块】
agent 主循环里对超过 4000 字符的工具输出做的是"原地砍中间、留头尾"：
    result_json[:1000] + "…[截断 N 字符]…" + result_json[-500:]
被砍掉的部分**永久丢失**——模型想看只能重新调一次工具。

实测基线（2026-10-14，data/chat.db 110,019 条 tool 消息）：
  - 带截断标记 17,557 次（17.4%），累计丢弃 9.69 亿字符
  - bash 占 8.39 亿（均值单次丢 11.6 万字符）
  - read_file 截断组同文件 5 步内续读 58.6% vs 正常组 34.8%（差 23.8pp）

本模块把"丢弃"换成"落盘 + 引用 id"，模型可以按需翻页取回。
照 dsh-ops 那句：References are observations, never authority.
—— 只读，不授予任何写权限。

【独立库的理由】
不塞 11.7GB 的 chat.db：引用结果有 TTL、需要能整体清理、回滚就是删文件。
'''

import os
import sys
import json
import time
import random
import sqlite3
import threading
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(os.path.dirname(BASE_DIR)) if os.path.basename(BASE_DIR) in ('a', 'b') else BASE_DIR
DATA_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else _ROOT
DB_PATH = os.path.join(DATA_DIR, 'data', 'tool_outputs.db')
LOG_PATH = os.path.join(DATA_DIR, 'data', 'tool_outputs_degrade.log')
MAX_STORE = 262144
TTL_SECONDS = 86400
CLEAN_EVERY = 200
_local = threading.local()
_insert_count = 0
_count_lock = threading.Lock()

def _conn():
    '''线程内复用连接。'''

    c = getattr(_local, 'conn', None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.execute('PRAGMA journal_mode=WAL')
        _local.conn = c
    return c

SCHEMA = '\nCREATE TABLE IF NOT EXISTS tool_outputs (\n    id          TEXT PRIMARY KEY,\n    topic_id    TEXT,\n    tool_name   TEXT,\n    ts          REAL NOT NULL,\n    bytes       INTEGER NOT NULL,\n    stored      INTEGER NOT NULL,\n    content     TEXT NOT NULL,\n    is_json     INTEGER DEFAULT 0\n);\nCREATE INDEX IF NOT EXISTS idx_to_ts ON tool_outputs(ts);\n'

def init_db():
    '''建库建表。失败返回 False（不抛，调用方决定是否降级）。'''

    try:
        c = _conn()
        c.executescript(SCHEMA)
        c.commit()
        return True
    except Exception as e:
        _log_degrade(f'init_db failed: {e}')
        return False

def _log_degrade(msg):
    '''降级必须落文件——pythonw 启动时 stderr 会被静默丢弃（全局纪律#14）。'''

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n')
    except Exception:
        return

_B36 = '0123456789abcdefghijklmnopqrstuvwxyz'
_SUMMARY_BUDGET = 3600

def _shrink_summary(summary, ref_id):
    '''确保摘要 json 化后不超 _SUMMARY_BUDGET。

    超了就把 head/tail 对半缩，重算 omitted —— 宁可少给预览，
    也不能让摘要自己触发下一轮落盘（那就成了死循环）。
    '''

    def _jlen(s):
        return len(json.dumps(s, ensure_ascii=False))

    for _ in range(10):
        if _jlen(summary) <= _SUMMARY_BUDGET:
            break
        h, t = summary.get('head', ''), summary.get('tail', '')
        if len(h) <= 50 and len(t) <= 50:
            summary.pop('head', None)
            summary.pop('tail', None)
            summary['preview'] = '已省略（{_ref} 可翻页取回）'.format(_ref=ref_id)
            break
        nh = h[:max(50, len(h) // 2)]
        nt = t[:max(0, len(t) // 2)]
        summary['head'], summary['tail'] = nh, nt
        summary['shown_head'], summary['shown_tail'] = len(nh), len(nt)
        summary['omitted'] = summary.get('bytes', 0) - len(nh) - len(nt)
        continue
    return summary

def _b36(n, width=8):
    s = ''
    while n:
        n, r = divmod(n, 36)
        s = _B36[r] + s
    return s.rjust(width, '0')

def make_id():
    '''r-<base36 8位><3位随机>。

    【绝不用裸自增】重启后自增会重号，同一条引用会被后来的输出覆盖。
    时间戳+随机 + 主键冲突重试 = 跨重启唯一。
    '''

    return 'r-' + _b36(int(time.time() * 1000)) + _b36(random.randint(0, 46655), 3)

def save(content, topic_id=None, tool_name=None, is_json=False):
    '''把完整输出落盘，返回摘要 dict；失败返回 None（调用方回退旧截断行为）。

    关键纪律：**绝不允许因为落盘失败导致工具结果丢失**。
    '''

    global _insert_count
    if content is None:
        return
    if not isinstance(content, str):
        try:
            _log_degrade(f'save got {type(content).__name__} instead of str, auto-dumped (tool={tool_name})')
            content = json.dumps(content, ensure_ascii=False)
            is_json = True
        except Exception as e:
            _log_degrade(f'save auto-dump failed: {e}')
    try:
        original_len = len(content)
        stored = content[:MAX_STORE]
        truncated_tail = len(stored) < original_len
        init_db()
        c = _conn()
        ref_id = None
        for _ in range(5):
            cand = make_id()
        try:
            c.execute('INSERT INTO tool_outputs (id, topic_id, tool_name, ts, bytes, stored, content, is_json) VALUES (?,?,?,?,?,?,?,?)', (cand, topic_id, tool_name, time.time(), original_len, len(stored), stored, 1 if is_json else 0))
            c.commit()
            ref_id = cand
        except sqlite3.IntegrityError:
            pass
        else:
            if ref_id is None:
                _log_degrade(f'id collision x5, content_len={original_len}')
                return
            with _count_lock:
                pass
    except Exception as e:
        _log_degrade(f'save failed (len={len(content) if content else 0}): {e}')
    try:
        if need_clean:
            pass
        try:
            cleanup()
        except Exception as e:
            _log_degrade(f'cleanup failed: {e}')
        else:
            head = stored[:1000]
            tail = stored[-500:] if len(stored) > 1500 else ''
            summary = {'_ref': ref_id, 'tool': tool_name or '', 'bytes': original_len, 'shown_head': len(head), 'shown_tail': len(tail), 'omitted': original_len - len(head) - len(tail), 'head': head, 'tail': tail, 'nextAction': f'完整内容：先 discover_tools("tool_output") 拿 schema，再 execute_advanced_tool(name="read_tool_output", args={{"id":"{ref_id}", "offset":0, "limit":512}})', 'tip': 'tip: 只想看某个字段全文 → field="stdout"；找关键词 → grep="关键词"'}
            if truncated_tail:
                summary['store_note'] = f'单条上限{MAX_STORE}字符，落盘已截尾，存了前{len(stored)}字符'
            summary = _shrink_summary(summary, ref_id)
    except Exception as e:
        _log_degrade(f'save failed (len={len(content) if content else 0}): {e}')
    return summary
    e = None
    del e

_UNWRAP_MAX = 3

def _unwrap_result(obj, max_depth=_UNWRAP_MAX):
    '''剥掉 registry.execute_tool 加的 {"result": X} 包装（可能多层）。

    2026-10-17 定位：execute_tool 统一 `return {"result": result}`，落盘后读取层
    直接拿外层当内容视图 ⇒ 外层只有一个 **dict/list 值**，下面 _view 里的
    str_vals 判定为空 ⇒ 退化成 json_pretty ⇒ stdout/content 这类大字段各被压成
    "一整行几万字符" ⇒ 交给 _budget_lines 时第一行就吃满预算，模型每次只拿到
    标题那几行，而 has_more 又是按原始 8 行算的 false ⇒ 模型以为读完了。

    实测（任务 6d895c38e809，1989 条消息）：read_tool_output 调用 72 次，
    返回字符中位数 291，68% 触发预算裁切或 returned<=1；真实引用
    r-mvbw2buhphc（7728 字节）只交付 2 行 14 字符。

    只剥"单键 + 键名 result"这一种形态，业务字段本身叫 result 且值是标量的
    不受影响（保持原样，field="result" 仍可读）。
    '''

    for _ in range(max_depth):
        if isinstance(obj, dict) and len(obj) == 1 and isinstance(obj.get('result'), (dict, list)):
            obj = obj['result']
            continue
        break
    return obj

def _view(content, is_json):
    '''把落盘载体转成"可读内容视图"，返回 (kind, lines, obj)。

【为什么必须有这一层】落盘的载体是 `json.dumps` 后的字符串，
里面的真实换行全被转义成字面 `\\n` —— 直接 splitlines() 整份输出只有 1 行，
offset/limit 按行分页、grep 行级过滤全部失效（实测踩到：150KB 输出 total_lines=1）。

所以分两层语义：
  · 存储层严格保真（content == 传入的 result_json，sha1 可验）
  · 读取层解包成"内容视图"（模型看的是内容，不是转义）

kind:
  json_fields — dict 里多数是字符串字段，展开成 [字段名] 分段，行分页/grep 才有意义
  json_pretty — 其余 JSON，缩进展开
  text        — 纯文本，原样分行

2026-10-17：进入本函数前先 _unwrap_result 剥掉 {"result": X} 包装，
否则字段视图判定失效（见 _unwrap_result 注释）。
'''

    if not is_json:
        return 'text', content.splitlines(), None
    try:
        obj = json.loads(content)
    except Exception:
        return 'text', content.splitlines(), None
    obj = _unwrap_result(obj)
    if isinstance(obj, dict):
        str_vals = [k for k, v in obj.items() if isinstance(v, str)]
        if str_vals and len(str_vals) >= max(1, len(obj) // 2):
            lines = []
            for k, v in obj.items():
                lines.append(f'[{k}]')
                if isinstance(v, str):
                    lines.extend(v.splitlines() or [''])
                else:
                    lines.extend(json.dumps(v, ensure_ascii=False, indent=2).splitlines())
                lines.append('')
            return 'json_fields', lines, obj
    return 'json_pretty', json.dumps(obj, ensure_ascii=False, indent=2).splitlines(), obj

_LINE_WRAP = 1000

def _wrap_long_lines(lines, width=_LINE_WRAP):
    '''把超长单行切成多段，返回 (新行列表, 被切的原始行数)。

    续段前面加 "…" 提示，避免模型把一段截断误读成语法错。
    '''

    out, n = [], 0
    for ln in lines:
        if len(ln) <= width:
            out.append(ln)
            continue
        n += 1
        for i in range(0, len(ln), width):
            seg = ln[i:i + width]
            out.append(seg if i == 0 else '…' + seg)
    return out, n

def read_page(ref_id, offset=0, limit=512, grep=None, fields=None, field=None):
    '''按需翻页读取。

    limit 上限 512 行。grep 行级过滤。
    field  取单个字段的全文（如 field="stdout"，最常用）
    fields 多字段概览投影（逗号分隔，≤32 个）
    '''

    try:
        limit = max(1, min(int(limit or 512), 512))
    except Exception:
        limit = 512
    try:
        offset = max(0, int(offset or 0))
    except Exception:
        offset = 0
    try:
        init_db()
        c = _conn()
        row = c.execute('SELECT id, tool_name, ts, bytes, stored, content, is_json FROM tool_outputs WHERE id=?', (ref_id,)).fetchone()
    except Exception as e:
        _log_degrade(f'read failed id={ref_id}: {e}')
        return {'error': f'读取引用失败: {e}'}
    if not row:
        return {'error': f'引用不存在或已过期（TTL {TTL_SECONDS // 3600}h）: {ref_id}'}
    rid, tool_name, ts, nbytes, nstored, content, is_json = row
    kind, lines, _obj = _view(content, is_json)
    if field:
        if _obj is not None:
            if isinstance(_obj, dict):
                val = _obj.get(field)
                if val is None:
                    return {'error': f'字段 {field} 不存在，可用: {list(_obj.keys())[:20]}'}
                ftext = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=2)
                flines = ftext.splitlines()
                if grep:
                    flines = [ln for ln in flines if str(grep) in ln]
                flines, _nw = _wrap_long_lines(flines)
                page = flines[offset:offset + limit]
                has_more = offset + limit < len(flines)
                if has_more:
                    return {'id': rid, 'tool': tool_name, 'total_bytes': nbytes, 'mode': 'field', 'field': field, 'total_lines': len(flines), 'offset': offset, 'returned': len(page), 'has_more': has_more, 'next_offset': offset + limit, 'lines': page}
                return {'id': None, 'tool': None, 'total_bytes': None, 'mode': None, 'field': None, 'total_lines': None, 'offset': None, 'returned': None, 'has_more': None, 'next_offset': None, 'lines': page}
    if fields:
        if _obj is not None:
            if isinstance(_obj, dict):
                if isinstance(fields, str):
                    fields = [f.strip() for f in fields.split(',') if f.strip()]
                picked = {}
                for k in fields[:32]:
                    v = _obj.get(k)
                    if isinstance(v, str) and len(v) > 300:
                        picked[k] = v[:300] + f'…[共{len(v)}字符，用 field="{k}" 取全文]'
                    else:
                        picked[k] = v
                return {'id': rid, 'tool': tool_name, 'total_bytes': nbytes, 'mode': 'fields', 'fields': list(picked.keys()), 'obj': picked, 'available': list(_obj.keys())[:32]}
    if grep:
        _all_lines = len(lines)
        try:
            needle = str(grep)
        except Exception:
            hit = lines
        try:
            hit = [ln for ln in lines if needle in ln]
        except Exception:
            hit = lines
        _matched = len(hit)
        hit, _nwg = _wrap_long_lines(hit)
        page = hit[offset:offset + limit]
        if offset + limit < len(hit):
            return {'id': rid, 'tool': tool_name, 'total_bytes': nbytes, 'view': kind, 'total_lines': _all_lines, 'matched_lines': _matched, 'mode': 'grep', 'offset': offset, 'returned': len(page), 'has_more': offset + limit < len(hit), 'next_offset': offset + limit, 'lines': page}
        return {'id': None, 'tool': None, 'total_bytes': None, 'view': None, 'total_lines': None, 'matched_lines': None, 'mode': None, 'offset': None, 'returned': None, 'has_more': None, 'next_offset': None, 'lines': page}
    lines, _nwrapped = _wrap_long_lines(lines)
    total_lines = len(lines)
    page = lines[offset:offset + limit]
    has_more = offset + limit < total_lines
    if has_more:
        return {'id': rid, 'tool': tool_name, 'total_bytes': nbytes, 'view': kind, 'total_lines': total_lines, 'mode': 'lines', 'offset': offset, 'returned': len(page), 'has_more': has_more, 'next_offset': offset + limit, 'lines': page}
    return {'id': None, 'tool': None, 'total_bytes': None, 'view': None, 'total_lines': None, 'mode': None, 'offset': None, 'returned': None, 'has_more': None, 'next_offset': None, 'lines': page}

def cleanup(ttl=None):
    '''清理过期引用。返回删除条数。'''

    ttl = TTL_SECONDS if ttl is None else ttl
    try:
        init_db()
        c = _conn()
        cutoff = time.time() - ttl
        cur = c.execute('DELETE FROM tool_outputs WHERE ts < ?', (cutoff,))
        c.commit()
        n = cur.rowcount
        if n:
            _log_degrade(f'cleanup removed {n} expired refs')
        return n
    except Exception as e:
        _log_degrade(f'cleanup failed: {e}')
        return 0

def stats():
    '''库里现状，排查用。'''

    try:
        init_db()
        c = _conn()
        n, tot = c.execute('SELECT COUNT(*), COALESCE(SUM(stored),0) FROM tool_outputs').fetchone()
        return {'count': n, 'stored_chars': tot, 'db_path': DB_PATH}
    except Exception as e:
        return {'error': str(e), 'db_path': DB_PATH}

if __name__ == '__main__':
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
