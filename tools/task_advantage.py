# -*- coding: utf-8 -*-
"""task_advantage — JitRL 借鉴：任务收尾 LLM 打分，写回事件账本 advantage 字段

背景（2026-10-06 点亮 9/4 部署的空壳开关 FINGERPRINT_ADVANTAGE_ENABLED）：
- 9/4 只落了配置+前端开关+audit_check 检索端，"打分写入"一直没实现；
- 本模块补齐写入侧：任务收尾时用 reflection_loop 的 LLM 通道对任务轨迹打分，
  把"坑情境下做法优劣结论"（JitRL 的 advantage 概念降维借用）写进
  当前任务工作区 self_audit/事件账本.json 的 records。
- 门控在 server.py /api/task/finish 处（_runtime_switch("fingerprint_advantage")），
  本模块保持纯净不查开关——单元测试可脱机注入 llm_call。
- 任何失败只打日志返回 None，绝不影响收尾主流程。
"""
import json
import re
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def is_enabled(db=None, default=True):
    """运行时开关：前端设置(db.meta '1'/'0') > 环境变量 FINGERPRINT_ADVANTAGE_ENABLED > 代码默认。
    每次现读热生效（与 server._runtime_switch 同语义，抽出成纯函数便于脱机单测）。"""
    try:
        if db is None:
            from db import get_db
            db = get_db()
        raw = db.get_meta("fingerprint_advantage")
        if raw is None or raw == "":
            import config
            return bool(getattr(config, "FINGERPRINT_ADVANTAGE_ENABLED", default))
        return raw == "1"
    except Exception as e:
        print(f"[task_advantage] 开关读取失败按默认处理: {e}")
        return default


def _ledger_path(task_id: str) -> Path:
    """任务工作区标准路径下的账本文件。

    ID 空间修正（2026-10-06）：DAG/任务体系用任务ID，data/missions 目录按话题ID组织——
    必须经 DAG 记录把任务ID翻译成 topic_id 再拼路径，否则会写进幽灵目录。
    DAG 加载失败时退回原 task_id 拼法（脱机测试注入 ledger_path，不依赖此分支）。"""
    tid = task_id
    try:
        from task.dag import DAG
        t = DAG(task_id).get_task()
        if isinstance(t, dict) and t.get("topic_id"):
            tid = t["topic_id"]
    except Exception as e:
        print(f"[task_advantage] topic_id 解析失败，退回 task_id: {e}")
    return ROOT_DIR / "data" / "missions" / tid / "workspace" / "self_audit" / "事件账本.json"


def _collect_trace(task_id: str, trace_text: str = None) -> str:
    """收集任务轨迹：优先用调用方给的文本，否则从 DAG 节点拼。失败返回占位。"""
    if trace_text:
        return trace_text[:4000]
    try:
        from task.dag import DAG
        dag = DAG(task_id)
        nodes = dag.get_nodes() or []
        lines = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            task = (n.get("task") or "").strip()
            result = (n.get("result") or "").strip()
            status = n.get("status", "?")
            lines.append(f"[{status}] {task}\n  产出: {result[:300]}")
        if lines:
            return "\n".join(lines)[:4000]
    except Exception as e:
        print(f"[task_advantage] 轨迹收集失败: {e}")
    return "（无可用轨迹）"


def _build_prompt(trace: str) -> str:
    return (
        "你是任务复盘器。基于下面的任务执行轨迹，给出优势标注（JitRL 思路："
        "经验要带优劣结论才能指导后续检索）。请输出且只输出一个 JSON 对象：\n"
        '{"score": 0到10的整数, "advantage": "一句话：坑情境下的做法相对常规做法的优劣结论（没踩坑就写顺利完成的最佳实践）", "lesson": "一句话教训或可复用经验"}\n\n'
        "【任务轨迹】\n" + trace
    )


def _parse_score_response(resp: str) -> dict:
    """解析 LLM 返回的 JSON；失败时降级用原文。"""
    if not resp:
        return {}
    try:
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if m:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"advantage": resp.strip()[:200], "score": None, "lesson": ""}


def score_task(task_id: str, trace_text: str = None, llm_call=None, ledger_path: str = None) -> dict:
    """打分并写回账本。返回写入的记录 dict；任何失败返回 None。

    llm_call: 注入替代 LLM（测试用）；默认复用 memory.reflection_loop._default_llm_call
    ledger_path: 注入账本路径（测试用）；默认当前任务工作区 self_audit/事件账本.json
    """
    try:
        trace = _collect_trace(task_id, trace_text)
        if llm_call is None:
            from memory.reflection_loop import _default_llm_call
            llm_call = _default_llm_call
        parsed = _parse_score_response(llm_call(_build_prompt(trace)))

        score = parsed.get("score")
        try:
            score = int(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        record = {
            "ts": time.time(),
            "task_id": task_id,
            "class": "task_reflection",
            "fingerprint": "",  # 留空：audit_check 会跳过无指纹记录，避免误命中
            "lesson": (parsed.get("lesson") or "").strip()[:300],
            "advantage": {"score": score, "text": (parsed.get("advantage") or "").strip()[:200]},
        }

        path = Path(ledger_path) if ledger_path else _ledger_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = {"records": []}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                    ledger = loaded
            except Exception as e:
                print(f"[task_advantage] 账本读取失败，将新建: {e}")

        # 幂等：同任务重跑收尾时更新而不是重复追加
        records = [r for r in ledger["records"] if r.get("task_id") != task_id]
        records.append(record)
        ledger["records"] = records
        path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[task_advantage] 已写回 {path.name} (score={score})")
        return record
    except Exception as e:
        print(f"[task_advantage] 失败(不影响收尾): {e}")
        return None
