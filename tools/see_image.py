#!/usr/bin/env python3
"""see_image — 把磁盘上的图片送进主模型，拿回画面描述。

存在理由：大眼的主循环拿不到磁盘图片（read_file 只回原始字节，聊天附件是唯一
入口，且 .mp4 会被 build_multimodal_content 跳过）。本工具在**工具内部**直接调
用主模型的多模态接口（复用项目根 vsee.py），把"看图"这件事封装成一个普通函数调用，
于是任何任务、任何目录下都能看图，不依赖聊天附件、不需要用户手动上传。

2026-10-16 落地：此前 vsee.py 只躺在某个任务工作区里，换任务就找不到，
每次看图都得 cd 到那个目录，属于"工具存在但不可达"。
"""
import os
import sys

from . import registry
from .registry import register_tool

# ── 自注册进折叠分组（无需重启服务）───────────────────────────────
# 分组名单 FOLDED_TOOL_GROUPS 定义在 registry.py 里，而 hot_reload 有意跳过
# registry.py（reload 它会清空 _tools 字典）。所以本模块被热重载时，顺手把
# 自己挂进分组表——本模块在 tools/ 下，是被重载的那一批，改动立即生效。
# 幂等：重复加载不会产生重复条目。
def _self_register_group():
    groups = registry.FOLDED_TOOL_GROUPS
    g = groups.get("vision")
    if g is None:
        groups["vision"] = {
            "description": "看图：把磁盘上的图片/视频帧送进主模型直接查看",
            "tools": [],
        }
        g = groups["vision"]
    if "see_image" not in g["tools"]:
        g["tools"].append("see_image")
    if "see_image" not in registry.ALWAYS_ON_TOOLS:
        pass  # 保持折叠，不占用常驻工具额度


try:
    _self_register_group()
except Exception as _e:  # pragma: no cover
    print(f"[see_image] 自注册分组失败: {_e}")



def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_vsee():
    root = _root()
    if root not in sys.path:
        sys.path.insert(0, root)
    import vsee  # noqa: E402  （项目根 vsee.py）
    return vsee


DEFAULT_Q = (
    "用中文如实、具体地描述这张图，逐项回答："
    "1) 有几个人物/主体，各自的角色定位；"
    "2) 主角色的发色与发型（含长度、刘海、发饰）；"
    "3) 眼睛颜色（瞳色）；"
    "4) 服装：颜色 + 款式 + 材质 + 配饰；"
    "5) 画面构图：是否分栏、分几栏、景别；"
    "6) 整体色调与光线。"
    "只描述你确实看到的；看不清就写「看不清」，不要脑补。"
)


SELF_VISION_MAX_IMAGES = 6


def _self_vision_enabled():
    """读 model_config.json 的 self_vision 开关（默认关）。"""
    try:
        import json
        with open(os.path.join(_root(), "model_config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("self_vision"))
    except Exception:
        return False


def _shrink(path, max_side):
    """缩到最长边 max_side 的 JPEG，存到 data/self_vision_cache；失败则原图。"""
    try:
        from PIL import Image
        im = Image.open(path)
        if max(im.size) <= max_side and path.lower().endswith((".jpg", ".jpeg", ".png")):
            return path
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        d = os.path.join(_root(), "data", "self_vision_cache")
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, f"{abs(hash((path, os.path.getmtime(path), max_side)))}.jpg")
        im.save(out, "JPEG", quality=90)
        return out
    except Exception:
        return path


@register_tool(
    name="see_image",
    description=(
        "看图：把本地磁盘上的图片文件送进主模型直接查看，返回画面内容的文字描述。"
        "适用场景：需要核对图像/视频帧的画面细节（人物姿态、服装、构图、颜色），"
        "或用户提到某张图但你只听得到路径。支持一次传多张图做对比。"
        "也可传视频路径自动抽帧后再看。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "图片路径列表（绝对路径或相对项目根/工作区）。多张时主模型会一起对比。",
            },
            "question": {
                "type": "string",
                "description": "要问画面什么。不传则用默认的逐项描述（人物/发型/瞳色/服装/构图/色调）。",
            },
            "video": {
                "type": "string",
                "description": "可选：视频文件路径，自动抽帧后再看（需 ffmpeg）。",
            },
            "frames": {
                "type": "integer",
                "description": "视频抽帧数量，默认 4。",
            },
            "max_side": {
                "type": "integer",
                "description": "发送前缩放的最长边，默认 1024。细节多可调大到 1536。",
            },
            "max_tokens": {
                "type": "integer",
                "description": "输出上限，默认 4096。推理型模型给小了会返回空 content。",
            },
            "mode": {
                "type": "string",
                "enum": ["self", "proxy"],
                "description": "self=把图片原样送进当前对话，由我自己看（需 model_config.json 的 self_vision=true）；proxy=转给 GLM 看并返回文字描述。默认 self，未开启时自动退回 proxy。",
            },
        },
        "required": [],
    },
)
def see_image(paths=None, question=None, video=None, frames=4,
              max_side=1024, max_tokens=4096, mode="self"):
    try:
        vsee = _load_vsee()
    except Exception as e:
        return {"error": f"加载 vsee.py 失败: {e}"}

    files = list(paths or [])
    if video:
        if not os.path.isabs(video):
            cand = os.path.join(_root(), video)
            video = cand if os.path.exists(cand) else video
        if not os.path.exists(video):
            return {"error": f"视频不存在: {video}"}
        got = vsee._extract_frames(video, frames)
        if not got:
            return {"error": "抽帧失败（找不到 ffmpeg 或视频不可读）"}
        files = got + files

    if not files:
        return {"error": "没有可看的图片：paths 为空且未提供 video"}

    resolved = []
    for p in files:
        if os.path.exists(p):
            resolved.append(p)
            continue
        cand = os.path.join(_root(), p)
        if os.path.exists(cand):
            resolved.append(cand)
            continue
        return {"error": f"文件不存在: {p}"}

    # ── self 模式：不转述，把图交给 server 注入当前对话 ──
    if (mode or "self") == "self" and _self_vision_enabled():
        imgs = [_shrink(p, max_side) for p in resolved[:SELF_VISION_MAX_IMAGES]]
        return {
            "__self_vision__": True,
            "images": imgs,
            "names": [os.path.basename(p) for p in resolved[:SELF_VISION_MAX_IMAGES]],
            "dropped": max(0, len(resolved) - SELF_VISION_MAX_IMAGES),
            "note": "图片已作为图像附在下一条消息里，请直接看图回答"
                    + (f"：{question}" if question else "。"),
        }

    q = question or DEFAULT_Q
    try:
        cfg = vsee._load_cfg()
        model = cfg.get("model") or "glm-5.3-flash"
        ans = vsee.ask(resolved, q, model, max_side, verbose=False,
                       max_tokens=max_tokens)
    except Exception as e:
        return {"error": f"看图失败: {type(e).__name__}: {e}"}

    if not ans:
        return {"error": "模型返回空内容（可能是 max_tokens 太小或图过大）"}
    return {
        "images": [os.path.basename(p) for p in resolved],
        "count": len(resolved),
        "model": model,
        "description": ans,
    }
