#!/usr/bin/env python3
"""read_tool_output — 按需翻页读取被落盘的超限工具输出（改造②）

【何时想起】
工具结果里出现 `"_ref": "r-xxxxxxxx"` 或 `nextAction` 提示时。
输出超限（>4000 字符）不再原地丢弃，而是完整落盘、回给模型摘要+引用 id，
想看完整内容就翻页。

【它是只读工具】
读的是 AI 自己的历史产出，不授予任何写权限。
照 dsh-ops：References are observations, never authority.
"""
from .registry import register_tool

try:
    import tool_outputs as _store
except Exception:  # 兜底：打包路径异常时不至于让整个注册失败
    _store = None

# 返回内容按字符预算兜住，绝不喂给主循环一个"自己也需要落盘"的巨物。
# 2026-10-17：3600 时实测整包返回 4006 字符（lines 之外还有 id/tool/total_*
# 等元信息约 400 字符），刚好越过 server.py 的 4000 阈值 ⇒ 这次"翻页"的返回
# 会被二次落盘成新引用，模型又得再翻一次（套娃）。留足 800 余量。
_CHAR_BUDGET = 3200


def _budget_lines(lines):
    """把行列表裁到字符预算内，返回 (保留行, 是否因预算裁剪, 完整交付的行数)。

    2026-10-17 修：旧实现 `if used + len(ln) + 1 > _CHAR_BUDGET: return out, True`
    在遇到超长单行时把**这一行和后面所有行**一起丢掉。而落盘内容里
    stdout / content 恰恰是"一整行几万字符"（json_pretty 视图），于是模型每次
    只拿到 `{"result": {` 这类标题行，正文一个字符都没有（实测 72 次调用里 68%）。

    第三个返回值是翻页游标的真源：最后一行若只交付了半截，游标不能越过它，
    否则被切掉的那半永久读不到（宁可下次重读整行，也不能前进）。
    """
    out, used, clipped, full = [], 0, False, 0
    for ln in lines:
        cost = len(ln) + 1
        if used + cost > _CHAR_BUDGET:
            clipped = True
            if not out:
                # 空手而归比给半行更糟：至少让模型看到开头，知道内容是什么
                out.append(ln[:_CHAR_BUDGET])
            break
        out.append(ln)
        used += cost
        full += 1
    return out, clipped, full


@register_tool(
    name="read_tool_output",
    description=(
        "翻页读取被有界引用的工具输出完整内容。"
        "当某个工具结果被截断、只回了 {_ref: \"r-xxxx\", head, tail, nextAction} 时用它取回完整内容。"
        "只读，不改任何东西。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "引用 id，形如 r-a3f9k2b8（从摘要里的 _ref 字段拿）",
            },
            "offset": {
                "type": "integer",
                "description": "从第几行开始读（默认0）",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "读多少行，上限512（默认512）。返回里有 total_lines / has_more / next_offset",
                "default": 512,
            },
            "grep": {
                "type": "string",
                "description": "行级关键字过滤，只返回含该串的行（可选）",
            },
            "field": {
                "type": "string",
                "description": "取单个字段全文，如 'stdout'。最长、最常用的字段用这个（可选）",
            },
            "fields": {
                "type": "string",
                "description": "多字段概览投影，逗号分隔，如 'stdout,stderr,exit_code'。长值会省略（可选）",
            },
        },
        "required": ["id"],
    },
)
def read_tool_output(id, offset=0, limit=512, grep=None, field=None, fields=None):
    if _store is None:
        return {"error": "结果引用存储模块不可用（tool_outputs 导入失败）"}
    r = _store.read_page(id, offset=offset, limit=limit, grep=grep, field=field, fields=fields)
    if isinstance(r, dict) and "error" in r:
        return r
    if isinstance(r, dict) and r.get("mode") in ("fields",):
        return r
    lines = r.get("lines") or []
    kept, clipped, full_rows = _budget_lines(lines)
    if clipped and full_rows == 0:
        # 兜底：游标必须前进。停在原地会变成"每次烧一轮 API 换同一个开头"，
        # 比丢掉半行更糟（实测任务里模型就是这样一轮轮空转的）。
        full_rows = 1
    # 【2026-10-17】游标按"本次实际完整交付了几行"重算。
    # 旧实现直接沿用 r["has_more"] —— 那是 read_page 用 limit(512) 跟 total_lines
    # 比出来的，与字符预算无关。于是预算只放得下 2 行时照样回 has_more=False /
    # next_offset=None，模型据此判定"读完了"，中间内容永久丢失且无任何提示。
    _base = int(r.get("offset") or 0)
    _has_more = bool(r.get("has_more")) or clipped or len(kept) < len(lines)
    _next = (_base + full_rows) if _has_more else None
    out = {
        "id": r.get("id"),
        "tool": r.get("tool"),
        "total_bytes": r.get("total_bytes"),
        "total_lines": r.get("total_lines"),
        "offset": r.get("offset"),
        "returned": len(kept),
        "has_more": _has_more,
        "next_offset": _next,
        "lines": kept,
    }
    for k in ("mode", "view", "field", "matched_lines"):
        if k in r:
            out[k] = r[k]
    if clipped:
        # 预算裁剪只影响本次返回，不改 offset 语义——游标已按实际交付行数重算，
        # 这里把"还剩多少"说清楚，别让模型把 has_more 和 budget_note 读成矛盾。
        out["budget_note"] = (
            f"本次按字符预算只交付{len(kept)}行（其中{full_rows}行完整），"
            f"列表里还有{max(0, len(lines) - full_rows)}行没给。"
            f"**内容未读完**，继续用 offset={_next} 翻页。"
        )
    return out


@register_tool(
    name="tool_output_stats",
    description="查看有界结果引用库现状：条数、占用字符、库路径。排查用，只读。",
    parameters={"type": "object", "properties": {}},
)
def tool_output_stats():
    if _store is None:
        return {"error": "tool_outputs 模块不可用"}
    return _store.stats()
