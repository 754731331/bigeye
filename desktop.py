"""大眼桌面壳：原生 Win32 窗口 + WebView2 内核，直接加载现有 Web 前端。

双击 大眼.bat 启动：无终端黑窗、不打开浏览器，前端只维护一套。
- 服务未运行时自动后台拉起 server.py（无控制台窗口）
- 窗口关闭时，仅终止本壳拉起的 server（用户手动起的不动）
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

PORT = 9890
# frozen 时 BASE = exe 所在目录（onedir），_MEIPASS 是打包临时目录，数据须持久化到 exe 旁
BASE = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(BASE, "图标.ico")
HEALTH = f"http://127.0.0.1:{PORT}/api/health"


_hwnd = None   # 主窗口句柄，供 JS API 拖拽/最大化用
_form = None   # 主窗口的 WinForms Form 实例（MaximizedBounds 必须走托管属性）
# 最大化校正的状态：hooked=事件已挂 / busy=正在处理(防重入) / dead=熔断 /
# fixes+last=滑动窗口计数（8 秒内纠超过 4 次说明在打架，直接收手，别把窗口晃晕）
_fit_state = {"hooked": False, "busy": False, "dead": False, "fixes": 0, "last": 0.0}
_window = None  # pywebview 窗口对象
_pip_window = None  # 画中画独立小窗（原生置顶窗口）
_splash = {"status": "正在启动", "done": False}  # 启动提示窗共享状态
_splash_closed = threading.Event()  # 卡片实际销毁信号：主窗口等它再显示，杜绝同屏跳动


def _is_web_url(u):
    """只放行 http/https，别让前端把本地路径/自定义协议塞进来。"""
    s = str(u or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _clear_pip_window(*_a):
    """小窗被用户关掉后清引用，下次调用才能重新建。"""
    global _pip_window
    _pip_window = None



def _splash_thread():
    """独立线程跑启动提示窗：双击后立刻有反馈，主窗口加载完立即消失。

    tkinter 全部调用只在本线程内；主线程通过 _splash 字典传状态（GIL 安全）。
    视觉：圆角深色卡片 + 单调递增进度条 + 状态小字；位置固定、淡入出现，
    主窗口加载完成立即销毁（不淡出，避免被主窗口弹出顶跳）。
    """
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        root = tk.Tk()
    except Exception:
        _splash_closed.set()  # 无 tkinter 环境静默跳过，放行主窗口
        return
    try:
        W, H, R = 420, 120, 14
        BG, EDGE, ACCENT, DIM = "#161616", "#2a2a2a", "#2ee6c0", "#8a8f98"
        MAGIC = "#010101"  # 透明键色，卡片圆角外区域透明
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=MAGIC)
        try:
            root.attributes("-transparentcolor", MAGIC)
        except Exception:
            pass
        root.update_idletasks()
        x0 = (root.winfo_screenwidth() - W) // 2
        y0 = (root.winfo_screenheight() - H) // 2
        root.geometry(f"{W}x{H}+{x0}+{y0}")  # 位置固定，动画只改透明度，避免跳动
        root.attributes("-alpha", 0.0)

        cv = tk.Canvas(root, width=W, height=H, bg=MAGIC, highlightthickness=0)
        cv.pack()
        # 圆角卡片：两矩形 + 四角扇形填充，边缘用弧线和直线描边
        cv.create_rectangle(R, 0, W - R, H, fill=BG, outline="")
        cv.create_rectangle(0, R, W, H - R, fill=BG, outline="")
        corners = ((R, R, 90), (W - R, R, 0), (W - R, H - R, 270), (R, H - R, 180))
        for cx, cy, a0 in corners:
            cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=a0, extent=90, fill=BG, outline="")
            cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=a0, extent=90, style="arc", outline=EDGE)
        cv.create_line(R, .5, W - R, .5, fill=EDGE)
        cv.create_line(R, H - .5, W - R, H - .5, fill=EDGE)
        cv.create_line(.5, R, .5, H - R, fill=EDGE)
        cv.create_line(W - .5, R, W - .5, H - R, fill=EDGE)
        f_s = tkfont.Font(family="Microsoft YaHei", size=9)
        # 进度条：2px 轨道 + 单调递增填充（渐近 92%，完成即满格关闭）
        TX1, TX2, TY = 110, 310, 46
        cv.create_rectangle(TX1, TY, TX2, TY + 3, fill="#262626", outline="")
        seg = cv.create_rectangle(TX1, TY, TX1, TY + 3, fill=ACCENT, outline="")
        st = cv.create_text(W / 2, 80, text="", fill=DIM, font=f_s)

        A = {"v": 0.0, "p": 0.0}
        t0 = time.time()
        pos0 = None

        def close(reason="done"):
            try:
                # withdraw 同步调系统 ShowWindow(SW_HIDE)，卡片立刻不可见；
                # destroy 的实际摘窗可能滞后 1~2 帧，不能等它再放行主窗口
                root.withdraw()
                root.destroy()
            finally:
                _splash_log(f"card close reason={reason}")
                _splash_closed.set()

        def tick(n=0):
            nonlocal pos0
            if _splash["done"] or time.time() - t0 > 140:
                close("done" if _splash["done"] else "timeout")  # 完成立即关
                return
            if pos0 is None:
                pos0 = (root.winfo_x(), root.winfo_y())
            elif (root.winfo_x(), root.winfo_y()) != pos0:
                close("moved")  # 位置被系统/主窗口顶动 → 立即关，绝不让它跳
                return
            if A["v"] < 1.0:
                A["v"] = min(1.0, A["v"] + 0.08)
                root.attributes("-alpha", A["v"])
            A["p"] = min(0.92, A["p"] + (0.92 - A["p"]) * 0.015 + 0.0012)
            cv.coords(seg, TX1, TY, TX1 + (TX2 - TX1) * A["p"], TY + 3)
            cv.itemconfig(st, text=_splash["status"] + "." * (n // 15 % 3 + 1))
            root.after(30, tick, n + 1)

        root.after(30, tick)
        root.mainloop()
    except Exception:
        pass
    finally:
        _splash_closed.set()  # 任何退出路径都放行主窗口


def _splash_log(msg):
    """启动交接时序日志，复发"卡片跳"时可查证。"""
    try:
        with open(os.path.join(BASE, "data", "splash_debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}\n")
    except Exception:
        pass


def start_splash():
    threading.Thread(target=_splash_thread, daemon=True).start()


def _set_window_icon():
    """窗口加载后按进程 ID 找到 Win32 窗口：设置图标 + 去掉标题栏（保留缩放边框）。"""
    global _hwnd
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 任务栏独立分组，不挂到 python.exe 下
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bigeye.desktop")
        pid = os.getpid()
        cands = []  # (面积, hwnd)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _):
            # 认主窗口：hidden 创建不可见，不能按可见性过滤；标题会被前端
            # 改成任务名，不能按标题过滤；进程内还有 WinForms/WebView2 的
            # 隐藏辅助顶层窗口，不能全收。取面积最大的顶层窗口=主窗口。
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                cands.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            return True

        user32.EnumWindows(enum_cb, 0)
        cands.sort(reverse=True)
        hwnds = [h for _, h in cands[:1]]
        LR_LOADFROMFILE = 0x0010
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000   # 标题栏（含系统按钮）
        WS_THICKFRAME = 0x00040000  # 边缘拖拽缩放
        for hwnd in hwnds:
            _hwnd = hwnd
            small = user32.LoadImageW(None, ICON, 1, 16, 16, LR_LOADFROMFILE)
            big = user32.LoadImageW(None, ICON, 1, 32, 32, LR_LOADFROMFILE)
            if small:
                user32.SendMessageW(hwnd, 0x80, 0, small)  # WM_SETICON ICON_SMALL
            if big:
                user32.SendMessageW(hwnd, 0x80, 1, big)   # WM_SETICON ICON_BIG
            # 去标题栏、保留缩放边框
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            style = (style & ~WS_CAPTION) | WS_THICKFRAME
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
            SWP = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)
    except Exception:
        pass


def _window_frame_insets():
    """当前窗口样式的非客户区厚度 (左, 上, 右, 下)，按窗口 DPI 算。

    最大化时 Windows 会给窗口保留一圈"缩放边框"，厚度正好等于这个值 ——
    实测（2026-09-24，96dpi + 去标题栏样式）：AdjustWindowRectExForDpi 算出的
    7px 与最大化后客户区的实际内缩 7px 完全一致。DPI 变了值跟着变（120dpi=8、
    144dpi=10、192dpi=12），所以只能现算，不能写死。
    拿不到就退回 SM_CXSIZEFRAME + SM_CXPADDEDBORDER。
    """
    try:
        import ctypes
        from ctypes import wintypes
        if os.name != "nt" or not _hwnd:
            return (0, 0, 0, 0)
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForWindow(_hwnd) or 96

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        r = RECT(0, 0, 100, 100)
        style = user32.GetWindowLongPtrW(_hwnd, -16)   # GWL_STYLE
        ex = user32.GetWindowLongPtrW(_hwnd, -20)      # GWL_EXSTYLE
        if user32.AdjustWindowRectExForDpi(ctypes.byref(r), style, 0, ex, dpi):
            return (-r.left, -r.top, r.right - 100, r.bottom - 100)
        fx = user32.GetSystemMetricsForDpi(32, dpi) + user32.GetSystemMetricsForDpi(92, dpi)
        fy = user32.GetSystemMetricsForDpi(33, dpi) + user32.GetSystemMetricsForDpi(92, dpi)
        return (fx, fy, fx, fy)
    except Exception as e:
        _splash_log(f"frame_insets err: {e!r}")
        return (0, 0, 0, 0)


def _ui_call(fn):
    """把窗口操作丢回 UI 线程执行；返回是否走了托管 Invoke。

    MaximizedBounds 是**托管属性**，跨线程写不安全（WinForms 属性非线程安全，
    挂了调试器还会直接抛 Cross-thread operation not valid）。js_api 的 toggle_max
    跑在子线程上，所以必须先 marshal 回 UI 线程 —— 与 start_drag 同一套路。
    InvokeRequired=False（已经在 UI 线程，如 _on_loaded / Form 事件）时直接调，
    免得自己把自己 Invoke 死锁。拿不到 Control 就原样直调：宁可沿用旧行为，
    也不能让"最大化"整条链路因为 marshal 失败而失效。
    """
    try:
        import clr  # noqa: F401  pywebview 已加载 pythonnet
        from System import IntPtr, Action
        from System.Windows.Forms import Control
        ctl = Control.FromHandle(IntPtr(_hwnd))
        if ctl is not None:
            if ctl.InvokeRequired:
                ctl.Invoke(Action(fn))
                return True
            fn()
            return True
    except Exception as e:
        _splash_log(f"ui_call err: {e!r}")
    fn()
    return False


def _monitor_work_area():
    """窗口当前所在显示器的**工作区** (左, 上, 宽, 高)，物理像素；失败返回 None。

    用 ctypes 的 MonitorFromWindow(MONITOR_DEFAULTTONEAREST)+GetMonitorInfoW，
    不走 WinForms 的 Screen.FromHandle —— 后者要 pythonnet、还要能拿到 Form 实例，
    而"这块屏幕上可达区域有多大"用纯 Win32 问最直接，也顺便给出显示器原点
    （多屏平移判定要用到它）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        if os.name != "nt" or not _hwnd:
            return None
        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        mon = user32.MonitorFromWindow(_hwnd, MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception as e:
        _splash_log(f"work_area err: {e!r}")
        return None


def _set_maximized_bounds():
    """把 WinForms 窗体的 MaximizedBounds 设为「工作区 + 非客户区厚度」。

    为什么必须外扩（2026-09-24 实测）：去掉标题栏后窗口仍留着 WS_THICKFRAME，
    最大化时 Windows 会把这圈 7px 的非客户区算在窗口矩形"里面"。把
    MaximizedBounds 直接设成工作区，客户区就被挤掉 7px —— 实测窗口
    2560x1552 / 客户区只有 2546x1538 @ (7,7)，那圈边框落在屏幕可见范围内
    被系统按深色填充：黑色/灰色主题看不出来，**白色主题就是左右两条黑边**。
    按厚度外扩后窗口矩形 = (-7,-7,2574,1566)（工作区 2560x1552 每边外扩 7px）、
    客户区正好铺满工作区 (2560x1552 @ (0,0))，多余边框全部悬到屏幕外，四边不留缝。
    顺带：客户区下边 = 工作区下边，所以照样不盖任务栏。

    行业通用解法（WinForms 社区多源验证）：Form.MaximizedBounds 会让
    窗体自己的 WndProc 在 WM_GETMINMAXINFO 里把最大化范围约束到这个矩形。
    按钮/双击/拖顶/Win+↑ 所有最大化路径都经过它，且保留全部原生行为
    （下拉还原、吸附、动画）。ctypes 子类化钩子改的值会被托管层覆盖，
    所以必须走托管属性。仅 UI 线程调用（且须在 _set_window_icon 改样式之后）。

    多屏坑（2026-09-28 实测，就是"全屏出界"的元凶）：MaximizedBounds.Location
    会被 WinForms 原样填进 MINMAXINFO.ptMaxPosition，而 Windows 把这个值
    **当作相对"当前正在最大化的那块显示器工作区原点"**来解释。主屏原点 (0,0)
    时两者等价、看不出问题；窗口一旦在副屏上最大化就整体平移。实测：
    MaximizedBounds=(-7,-7,2574,1566)（按主屏算出来的）+ 在副屏（工作区原点
    -1920,0）最大化 ⇒ 窗口矩形变成 (-1927,-7,647,1559)：左边 1920px 压到副屏、
    右边只剩 647px 留在主屏，客户区明明还是 2560x1552 —— 用户看到的就是
    "全屏以后跑到屏幕外面去了"。
    ⇒ Location 必须写成相对值 (-fl, -ft)，让 Windows 自己去加显示器原点；
    尺寸仍取当前所在显示器的工作区（两块屏分辨率不同，尺寸不能共用）。
    """
    try:
        from System.Drawing import Rectangle
        from System.Windows.Forms import Application
        global _form
        form = _form
        if form is None or form.IsDisposed:
            # 遍历 OpenForms 按句柄匹配拿 Form 实例（pythonnet 可直接操作托管对象）
            form = None
            for f in Application.OpenForms:
                if f.Handle.ToInt64() == _hwnd:
                    form = f
                    break
            _form = form
        wa = _monitor_work_area()
        if wa is None:                      # 极端兜底：退回 WinForms 的屏信息
            from System import IntPtr
            from System.Windows.Forms import Screen
            w = Screen.FromHandle(IntPtr(_hwnd)).WorkingArea
            wa = (w.X, w.Y, w.Width, w.Height)
        fl, ft, fr, fb = _window_frame_insets()
        if form is None:
            _splash_log("maximized_bounds: form not found (wa=%d,%d %dx%d)"
                        % (wa[0], wa[1], wa[2], wa[3]))
            return
        # 关键：Location 给相对值，别自己加 wa[0]/wa[1]（Windows 会按显示器原点再加一次）
        form.MaximizedBounds = Rectangle(
            -fl, -ft, wa[2] + fl + fr, wa[3] + ft + fb)
        _splash_log("maximized_bounds wa=%d,%d %dx%d insets=%s size=%dx%d"
                    % (wa[0], wa[1], wa[2], wa[3], (fl, ft, fr, fb),
                       wa[2] + fl + fr, wa[3] + ft + fb))
        _hook_form_events(form)   # 第一次拿到 Form 就把 Resize/Move 收口挂上
    except Exception as e:
        _splash_log(f"maximized_bounds err: {e!r}")


def _hook_form_events(form):
    """监听窗口尺寸/位置变化，把"最大化"这件事收口到一处。

    为什么要挂：用户还能用 Win+↑、拖到屏幕顶部吸附、任务栏右键最大化 —— 这些路径
    不经过本壳的 toggle_max()，MaximizedBounds 若还是上一块屏算出来的尺寸就会溢出。
    所以：未最大化时随手刷新 MaximizedBounds（给下一次最大化备好正确尺寸），
    已最大化时核对客户区。带重入锁 + 熔断，防止自己触发的 Resize 打成死循环。
    """
    if _fit_state["hooked"]:
        return
    _fit_state["hooked"] = True

    def on_move_resize(sender, args):
        if _fit_state["busy"] or _fit_state["dead"]:
            return
        _fit_state["busy"] = True
        try:
            import ctypes
            if ctypes.windll.user32.IsZoomed(_hwnd):
                now = time.time()
                if now - _fit_state["last"] > 8:
                    _fit_state["fixes"] = 0
                if _fit_state["fixes"] >= 4:          # 熔断：反复纠不回来就收手
                    _fit_state["dead"] = True
                    _splash_log("max_fit hook disabled (thrashing)")
                    return
                _fit_state["fixes"] += 1
                _fit_state["last"] = now
                _verify_max_fit()
            else:
                _set_maximized_bounds()
        except Exception as e:
            _splash_log(f"on_move_resize err: {e!r}")
        finally:
            _fit_state["busy"] = False

    try:
        form.Resize += on_move_resize
        form.Move += on_move_resize
        _splash_log("form hook: Resize/Move attached")
    except Exception as e:
        _splash_log(f"form hook err: {e!r}")


def _verify_max_fit():
    """最大化后核对：客户区必须正好铺满"窗口所在显示器"的工作区，否则纠正。

    为什么还要这一步：MaximizedBounds 只在"最大化那一刻"生效，且一旦窗口是在
    另一块显示器上被最大化的（或者它压根没吃到新值），窗口就会带着旧尺寸/旧偏移
    停在错的位置 —— 现象就是"全屏出界、右栏跑到屏幕外"。
    纠正手法选「还原 → 按当前显示器重算 MaximizedBounds → 再最大化」，
    而不是 SetWindowPos 硬改矩形：后者会把原生的 zoomed 状态和吸附/还原动画弄丢。
    只在检测到不一致时才动作，最多两次，正常路径零额外开销。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not _hwnd or not user32.IsZoomed(_hwnd):
            _splash_log("max_fit skip: not maximized")
            return
        for attempt in (1, 2):
            wa = _monitor_work_area()
            if wa is None:
                _splash_log("max_fit: no work area")
                return
            c = wintypes.RECT()
            user32.GetClientRect(_hwnd, ctypes.byref(c))
            if (c.right, c.bottom) == (wa[2], wa[3]):
                _splash_log("max_fit ok client=%dx%d wa=%d,%d %dx%d"
                            % (c.right, c.bottom, wa[0], wa[1], wa[2], wa[3]))
                return
            _splash_log("max_fit fix#%d client=%dx%d wa=%d,%d %dx%d"
                        % (attempt, c.right, c.bottom, wa[0], wa[1], wa[2], wa[3]))
            _set_maximized_bounds()
            user32.ShowWindow(_hwnd, 9)      # SW_RESTORE
            time.sleep(0.05)
            user32.ShowWindow(_hwnd, 3)      # SW_MAXIMIZE
            time.sleep(0.12)
        c = wintypes.RECT()
        user32.GetClientRect(_hwnd, ctypes.byref(c))
        if (c.right, c.bottom) == (wa[2], wa[3]):
            _splash_log("max_fit GIVEUP but client ok %dx%d" % (c.right, c.bottom))
            return
        # 最后兜底：MaximizedBounds 被吞掉时，按绝对坐标把外框直接摆到位
        fl, ft, fr, fb = _window_frame_insets()
        SWP = 0x0004 | 0x0010          # SWP_NOZORDER | SWP_NOACTIVATE
        user32.SetWindowPos(_hwnd, 0, wa[0] - fl, wa[1] - ft,
                            wa[2] + fl + fr, wa[3] + ft + fb, SWP)
        time.sleep(0.12)
        c = wintypes.RECT()
        user32.GetClientRect(_hwnd, ctypes.byref(c))
        _splash_log("max_fit GIVEUP client=%dx%d zoomed=%d after SetWindowPos"
                    % (c.right, c.bottom, bool(user32.IsZoomed(_hwnd))))
    except Exception as e:
        _splash_log(f"max_fit err: {e!r}")


def _log_viewport(delay=1.5):
    """延迟到独立线程里量一次「窗口矩形 / 客户区 / 页面视口」，落盘备查。

    这条日志是"全屏出界"的判据：client 和 viewport 必须吻合，
    否则前端就是按错的宽高在排版（右栏被挤出去/一屏装不下都是它的下游症状）。
    必须独立线程：evaluate_js 会等页面回结果，卡在 UI 线程或 js_api 线程上
    可能死等（js_api 里再调 evaluate_js 是 pywebview 已知的死锁姿势）。
    """
    def run():
        time.sleep(delay)
        if not _window or not _hwnd:
            return
        vp = "?"
        try:
            vp = _window.evaluate_js("window.innerWidth + 'x' + window.innerHeight")
        except Exception as e:
            vp = f"err:{e.__class__.__name__}"
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            c = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(_hwnd, ctypes.byref(r))
            ctypes.windll.user32.GetClientRect(_hwnd, ctypes.byref(c))
            wa = _monitor_work_area() or (0, 0, 0, 0)
            _splash_log("geometry rect=%d,%d %dx%d client=%dx%d viewport=%s wa=%d,%d %dx%d"
                        % (r.left, r.top, r.right - r.left, r.bottom - r.top,
                           c.right, c.bottom, vp, wa[0], wa[1], wa[2], wa[3]))
        except Exception as e:
            _splash_log(f"geometry err: {e!r} viewport={vp}")
    threading.Thread(target=run, daemon=True).start()


# DWM 窗口属性。去掉 WS_CAPTION 后，窗口顶部那条非客户区会被 DWM 当成"标题栏"
# 整块刷上系统标题栏颜色——强调色一开（ColorPrevalence=1）就是一抹亮色，
# 于是顶边成了一条特别宽的彩色边框，而左右下只有 1px。这两个属性正是它的开关：
DWMWA_BORDER_COLOR = 34     # 四周那 1px 细线
DWMWA_CAPTION_COLOR = 35    # 顶部 6px 条（高 = 非客户区上边）
_frame_color = None         # 前端同步过来的顶栏底色；None = 尚未同步


def _set_dwm_frame(hexcolor=None):
    """把顶部非客户区条 + 四周 1px 细线刷成 App 顶栏底色，让四条边视觉一致。

    hexcolor: '#rrggbb' / 'rrggbb'（也接受 3 位缩写）；None 表示沿用已记住的颜色，
    没记住过就用默认深色。返回是否全部设置成功。
    """
    global _frame_color
    if hexcolor:
        s = str(hexcolor).strip().lstrip('#')
        if len(s) == 3:
            s = ''.join(ch * 2 for ch in s)
        try:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except (ValueError, IndexError):
            return False            # 非法颜色直接拒绝，别污染已记住的值
        if len(s) != 6 or r > 255 or g > 255 or b > 255:
            return False
        _frame_color = '#' + s
    if os.name != 'nt' or not _hwnd:
        return False
    try:
        s = (_frame_color or '#171717').lstrip('#')
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        val = ctypes.c_uint((b << 16) | (g << 8) | r)  # COLORREF = 0x00BBGGRR
        dwm = ctypes.windll.dwmapi
        ok = True
        for attr in (DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR):
            if dwm.DwmSetWindowAttribute(_hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) != 0:
                ok = False
        return ok
    except Exception:
        return False


def _on_loaded():
    """页面加载完：先改样式再显示，标题栏不闪现。卡片早已在 webview.start() 前销毁。"""
    _set_window_icon()
    _set_maximized_bounds()
    _set_dwm_frame()  # 顶部那条"特别宽"的边框：刷成顶栏底色（前端随后会按主题校正）
    _verify_max_fit()  # 若启动时就是最大化态，顺手核对客户区是否铺满工作区
    if _window:
        _window.show()
    _log_viewport()  # 页面稳定后记一条几何快照（独立线程，不阻塞）


def _do_native_drag():
    """在 UI 线程上释放捕获并发起系统标题栏拖拽。"""
    import ctypes
    user32 = ctypes.windll.user32
    user32.ReleaseCapture()
    user32.SendMessageW(_hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION



class WinApi:
    """暴露给前端 window.pywebview.api 的窗口控制。"""

    def minimize(self):
        if _window:
            _window.minimize()

    def toggle_max(self):
        """系统真最大化/还原（尺寸已被 WM_GETMINMAXINFO 约束到工作区，不盖任务栏）。

        ShowWindow 同步生效，IsZoomed 立即反映新状态，直接读即可。
        最大化前先按"窗口当前所在显示器"重算 MaximizedBounds：窗口可能已经被拖到
        另一块屏上，沿用旧屏的尺寸就会溢出/留缝；再叠加 Windows 按显示器原点解释
        ptMaxPosition 的行为，就会整体平移成"全屏出界"。算完还要核对一次。
        ⚠️ 整段必须 marshal 回 UI 线程（MaximizedBounds 是托管属性，js_api 在子线程）。
        """
        if not _hwnd:
            return False
        import ctypes
        user32 = ctypes.windll.user32

        def _do():
            if user32.IsZoomed(_hwnd):
                user32.ShowWindow(_hwnd, 9)      # SW_RESTORE
            else:
                _set_maximized_bounds()          # 先按当前显示器重算，再最大化
                user32.ShowWindow(_hwnd, 3)      # SW_MAXIMIZE
                _verify_max_fit()

        _ui_call(_do)
        return bool(user32.IsZoomed(_hwnd))

    def is_maximized(self):
        import ctypes
        return bool(_hwnd and ctypes.windll.user32.IsZoomed(_hwnd))

    def close(self):
        if _window:
            _window.destroy()

    def set_frame_color(self, hexcolor):
        """前端在应用主题时把当前顶栏底色传进来，让顶部非客户区条同步。

        三种主题顶栏底色不同（黑 #0a0a0a / 灰 #161616 / 白 #ffffff），
        写死颜色会在换主题后露馅，所以由前端算好实际值传过来。
        """
        return _set_dwm_frame(hexcolor)

    def open_external(self, url):
        """用系统默认浏览器打开链接（画中画面板"在浏览器打开"的兜底出口）。"""
        if not _is_web_url(url):
            return False
        try:
            if os.name == "nt":
                os.startfile(url)  # noqa: S606 打开的是网址，不是可执行文件
            else:
                import webbrowser
                webbrowser.open(url)
            return True
        except Exception:
            return False

    def open_pip(self, url):
        """独立小窗（原生画中画）：置顶、可拖动缩放，给"禁止被 iframe 内嵌"的站点用。

        为什么能在这里建窗：pywebview 的 js_api 调用本身跑在子线程里，而 create_window
        只在"非主线程 + GUI 已启动"时才会立即建窗（源码 __init__.py 里就是这个判断），
        所以 js_api 里直接调是官方允许的路径，不需要绕 route。
        已存在小窗就复用它，避免连点叠出一堆窗口。
        """
        global _pip_window
        if not _is_web_url(url):
            return False
        try:
            import webview
            w = _pip_window
            if w is not None:
                try:
                    w.load_url(url)
                    w.show()
                    w.restore()
                    return True
                except Exception:
                    _pip_window = None  # 已被关掉/失效，重开一个
            try:
                scr = webview.screens[0]
                sw, sh = int(scr.width), int(scr.height)
            except Exception:
                sw, sh = 1280, 860
            ww, hh = 520, 620
            x = max(20, sw - ww - 48)
            y = max(20, sh - hh - 120)
            _pip_window = webview.create_window(
                "画中画", url, width=ww, height=hh, x=x, y=y,
                on_top=True, resizable=True, min_size=(300, 220),
                text_select=True,
            )
            if _pip_window is None:
                return False
            try:
                _pip_window.events.closed += _clear_pip_window
            except Exception:
                pass
            return True
        except Exception as e:
            _splash_log(f"open_pip err: {e!r}")
            return False

    def start_drag(self):
        """原生标题栏拖拽：系统接管移动，自带 Win 手势（左右边缘分屏/拖顶最大化）。

        js_api 在后台线程执行，而 ReleaseCapture 只释放"调用线程"的捕获——
        鼠标捕获在 UI 线程的 WebView2 子窗口上，必须 marshal 回 UI 线程再发，
        否则系统拖拽循环收不到鼠标移动，窗口不动。
        """
        if not _hwnd:
            return
        try:
            import clr  # noqa: F401  pywebview 已加载 pythonnet
            from System import IntPtr, Action
            from System.Windows.Forms import Control
            ctl = Control.FromHandle(IntPtr(_hwnd))
            if ctl is not None:
                ctl.Invoke(Action(_do_native_drag))
                return
        except Exception:
            pass
        _do_native_drag()  # 兜底：直接调（理论上仅 UI 线程调用时有效）


def server_alive():
    try:
        with urllib.request.urlopen(HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _server_cmd():
    """返回拉起后端的命令。
    frozen 后 sys.executable 是 desktop.exe 而非解释器，不能跑 server.py；
    需改为启动同目录（onedir）的 server.exe。"""
    if getattr(sys, 'frozen', False):
        server_exe = os.path.join(BASE, 'server.exe')
        if not os.path.isfile(server_exe):
            raise FileNotFoundError(f"找不到 server.exe: {server_exe}")
        return [server_exe, '--port', str(PORT)]
    return [sys.executable, '-u', 'server.py', '--port', str(PORT)]


def main():
    start_splash()  # 先起提示窗，再 import webview（导入本身也要几秒）
    import webview

    spawned = None
    if not server_alive():
        _splash["status"] = "正在启动服务"
        # 日志落盘，崩溃可查（与重启脚本同款带时间戳文件）
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(BASE, "data")
        os.makedirs(log_dir, exist_ok=True)
        out = open(os.path.join(log_dir, f"server_stdout_{ts}.log"), "a", encoding="utf-8")
        err = open(os.path.join(log_dir, f"server_stderr_{ts}.log"), "a", encoding="utf-8")
        spawned = subprocess.Popen(
            _server_cmd(),
            cwd=BASE,
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(120):  # 首次启动要加载模型，最多等 120 秒
            if server_alive():
                break
            if spawned.poll() is not None:
                print("server 启动失败，见 data/server_stderr_*.log")
                _splash["status"] = "服务启动失败，见 data/server_stderr_*.log"
                time.sleep(3)  # 让用户看清提示再退出
                sys.exit(1)
            time.sleep(1)
        else:
            print("server 启动超时")
            spawned.terminate()
            _splash["status"] = "服务启动超时"
            time.sleep(3)
            sys.exit(1)

    _splash["status"] = "正在加载界面"
    global _window
    _window = webview.create_window(
        "大眼",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=860,
        min_size=(720, 480),
        js_api=WinApi(),
        hidden=True,  # 先隐藏创建，样式就绪后再显示，避免标题栏闪现
        text_select=True,  # pywebview 默认注入 body{user-select:none} 禁全页选择，关掉才能复制对话文字
    )
    _window.events.loaded += _on_loaded
    # 卡片必须在 webview.start() 前彻底销毁：webview 初始化 UI 框架会扰动
    # 同进程还活着的 tkinter 卡片（表现为卡片跳变）。代价是卡片关闭后有一
    # 小段页面加载空白期，本地页面加载很快，可接受。
    _splash["done"] = True
    _splash_closed.wait(timeout=1)
    _splash_log("main show")
    # private_mode 默认 True 会丢弃 localStorage/cookies，主题等设置重启即失；
    # 关闭后持久化到 %APPDATA%/pywebview。
    webview.start(private_mode=False)  # 阻塞直到窗口关闭（窗口 hidden 创建，loaded 后才显示）

    if spawned is not None:
        spawned.terminate()
        # 大眼退出 → 一并停掉公网链路（守门员 + frpc），不留后台进程。
        # 保活现在由大眼本体负责（见 server._tunnel_watchdog_loop），所以关闭时收摊；
        # 若被强杀来不及走这里也没关系：下次启动时按配置自动收敛并补齐。
        try:
            import tunnel_manager
            tunnel_manager.stop_link()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw 无控制台，崩溃写盘便于排查
        import traceback
        try:
            log_dir = os.path.join(BASE, "data")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "desktop_crash.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                traceback.print_exc(file=f)
                f.write("\n")
        except Exception:
            pass
        raise
