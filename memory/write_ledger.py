#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_ledger.py — 写入尝试账本（append-only）  v3

设计来源：dsh-memory 的 writepipe 三态记账。
核心原则：
  1. 三态留痕：committed / short_circuit / error —— "没写成"不等于"没发生"
  2. committed 只认验证：写后验证通过（verify 字段）才算落盘
  3. error 必带原因
  4. 只追加，永不改写
  5. 只记事实，不记理解（那是 memory / 草稿纸的职责）
  6. 写失败不静默：降级到 stderr 告警
  7. 【v2】账本自身故障绝不反噬主写入：record() 保证不抛异常；
     调用侧另有 led_* 安全包装（账本对象整个坏掉也只降级告警）
  8. 【v3】多进程安全：跨进程文件锁 + O_APPEND 追加。
     实测 Windows 上 O_APPEND 的 os.write 并非原子（4 进程 x 200 条
     实测丢 146~173 条、并出现 4 条撕裂行），故必须串行化 [rotate + append]。
  9. 【v3】轮转文件名带 pid+序号，避免同秒重命名冲突（v1 会静默轮转失败）
  10.【v3.2】每条账目带 run_id（本次运行的唯一标识 = ms时间戳-pid）。
     seq 是【进程内】计数器，重启后从 1 重数 —— 单看 seq 必然撞车。
     唯一键 = (run_id, seq)，见 rec_key()。字段只增不改，旧记录仍可读。
"""
import os
import sys
import json
import time
import threading

# ── 三态枚举 ──
COMMITTED = "committed"        # 落盘且验证过
SHORT_CIRCUIT = "short_circuit"  # 被闸门/去重逻辑拦下（也是一次事件）
ERROR = "error"                # 失败，必须带原因

_LOCK = threading.Lock()
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 8MB 轮转
_ROTATE_SEQ = 0                 # 进程内轮转序号，防同秒重名

if os.name == "nt":
    import msvcrt
else:
    import fcntl


# 降级日志：stderr 之外的第二出口（见下方 _degrade 说明）
_DEGRADE_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "write_ledger_degraded.log")


def _degrade(msg):
    """账本自身出问题时的唯一出口：绝不静默、绝不抛出。

    v3.1：大眼由 `大眼.bat` 以 pythonw.exe 启动（无控制台），stderr 会被丢弃 ——
    只写 stderr 等于静默。故必须再落一份降级日志文件，否则原则 6 形同虚设。
    """
    line = "[write_ledger][DEGRADED] %s" % msg
    try:
        if sys.stderr is not None:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    except Exception:
        pass
    try:
        d = os.path.dirname(_DEGRADE_LOG)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(_DEGRADE_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line))
    except Exception:
        pass  # 第二出口也失败：认输，但绝不抛出、绝不递归


class _ProcLock:
    """
    跨进程互斥锁（长持 fd，只做 lock/unlock，避免每次 open/close 开销）。
    Windows: msvcrt.locking；POSIX: fcntl.flock。
    锁文件与账本文件分离，永不 rename，故轮转不影响锁语义。
    """

    def __init__(self, lock_path):
        self.lock_path = lock_path
        self._fd = None

    def _ensure(self):
        if self._fd is None:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        return self._fd

    def acquire(self, timeout=5.0):
        fd = self._ensure()
        deadline = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() > deadline:
                    _degrade("获取账本跨进程锁超时(%.1fs)" % timeout)
                    return False
                time.sleep(0.0005)

    def release(self):
        fd = self._fd
        if fd is None:
            return
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception as e:
            _degrade("释放账本锁失败: %s" % e)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None


def _atomic_append(path, line_bytes, do_fsync=True):
    """O_APPEND 打开 + 单次 os.write。调用方需已持有跨进程锁。"""
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, line_bytes)
        if do_fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


class WriteLedger:
    """只追加的写入账本。线程安全 + 多进程安全。"""

    def __init__(self, path=None, max_bytes=_DEFAULT_MAX_BYTES, enabled=True,
                 fsync=True, batch_size=0):
        if path is None:
            # 账本是数据不是代码：与 chat.db 同放根 data/（见 fragment_store._get_db_path）
            memory_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(os.path.dirname(memory_dir), "data", "write_ledger.jsonl")
        self.path = path
        self.max_bytes = max_bytes
        self.enabled = enabled
        self.fsync = fsync
        self.batch_size = batch_size   # >0 = 攒够 N 条再 fsync
        self._pending = 0
        self._dir = os.path.dirname(self.path)
        self._seq = 0
        # v3.2：run_id 标识"本进程本次运行"。seq 跨重启一定重号，
        # 故对账/去重必须用 (run_id, seq) 而不是 seq 单键。
        self.run_id = "%d-%d" % (int(time.time() * 1000), os.getpid())
        self._plock = _ProcLock(self.path + ".lock")
        if self.enabled and self._dir:
            try:
                os.makedirs(self._dir, exist_ok=True)
            except Exception as e:
                _degrade("无法创建账本目录 %s: %s" % (self._dir, e))

    def __del__(self):
        try:
            self._plock.close()
        except Exception:
            pass

    # ── 内部：轮转（保留历史，不删除；文件名带 pid+序号防重名）──
    def _rotate_if_needed(self):
        global _ROTATE_SEQ
        try:
            if not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < self.max_bytes:
                return
            for _ in range(5):   # 重名就换名重试，绝不因重名放弃轮转
                _ROTATE_SEQ += 1
                ts = time.strftime("%Y%m%d_%H%M%S")
                dst = "%s.%s_%d_%d" % (self.path, ts, os.getpid(), _ROTATE_SEQ)
                if os.path.exists(dst):
                    continue
                try:
                    os.rename(self.path, dst)
                    self._pending = 0
                    return
                except FileExistsError:
                    continue
                except Exception as e:
                    _degrade("轮转失败: %s" % e)
                    return
        except Exception as e:
            _degrade("轮转检查失败: %s" % e)

    # ── 核心 API（保证不抛异常）──
    def record(self, actor, target, outcome, reason="", verify="", extra=None):
        """
        追加一条账目。返回 True=已落盘，False=降级。
        本方法【保证不抛异常】——账本是旁路观测，绝不允许反噬主写入。
        """
        try:
            if not self.enabled:
                return True
            if outcome not in (COMMITTED, SHORT_CIRCUIT, ERROR):
                _degrade("非法 outcome: %r" % outcome)
                outcome = ERROR
                reason = reason or "illegal_outcome"
            if outcome == ERROR and not reason:
                reason = "unspecified"  # 不阻断，但强制有值

            with _LOCK:                    # 进程内线程串行
                self._seq += 1
                rec = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S") + ".%03d" % int((time.time() % 1) * 1000),
                    "seq": self._seq,
                    "run_id": self.run_id,
                    "actor": actor,
                    "target": target,
                    "outcome": outcome,
                    "reason": reason,
                    "verify": verify,
                    "pid": os.getpid(),
                }
                if extra:
                    rec["extra"] = extra
                try:
                    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
                except Exception as e:
                    _degrade("账目序列化失败: %s | 原记录: %r" % (e, rec))
                    return False
                locked = self._plock.acquire()   # 跨进程串行 [rotate + append]
                try:
                    self._rotate_if_needed()
                    self._pending += 1
                    # v3.1：批量模式必须【覆盖】fsync，否则 fsync=True 的 OR 短路会让
                    # batch_size 形同虚设（实测批量(20) 仍每条 fsync，100 条 100 次）
                    if self.batch_size > 0:
                        do_fsync = self._pending >= self.batch_size
                    else:
                        do_fsync = self.fsync
                    _atomic_append(self.path, line, do_fsync=do_fsync)
                    if do_fsync:
                        self._pending = 0
                    return True
                except Exception as e:
                    _degrade("写入账本失败: %s | 原记录: %s" % (e, line.decode("utf-8", "replace").strip()))
                    return False
                finally:
                    if locked:
                        self._plock.release()
        except Exception as e:
            # 兜底：连上面都没兜住的也不能影响主流程
            _degrade("账本内部异常: %s" % e)
            return False

    def flush(self):
        """
        v3.1：批量模式下，最后不足一批的账目还没 fsync。
        进程正常退出/交接前调一次，保证不丢尾巴。
        即使失败也只降级告警，不抛。
        """
        try:
            with _LOCK:
                if self._pending <= 0 or not self.enabled:
                    return True
                locked = self._plock.acquire()
                try:
                    if os.path.exists(self.path):
                        fd = os.open(self.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    self._pending = 0
                    return True
                finally:
                    if locked:
                        self._plock.release()
        except Exception as e:
            _degrade("flush 失败: %s" % e)
            return False

    # ── 便捷方法 ──
    def committed(self, actor, target, verify="", extra=None):
        return self.record(actor, target, COMMITTED, reason="", verify=verify, extra=extra)

    def short_circuit(self, actor, target, reason, extra=None):
        return self.record(actor, target, SHORT_CIRCUIT, reason=reason, extra=extra)

    def error(self, actor, target, reason, extra=None):
        return self.record(actor, target, ERROR, reason=reason, extra=extra)

    # ── 查询（对账用）──
    def read_all(self, include_rotated=False):
        """读回全部账目。用于对账：宣称的 committed 在账里查得到吗。"""
        out = []
        paths = []
        if include_rotated and self._dir and os.path.isdir(self._dir):
            base = os.path.basename(self.path)
            for f in sorted(os.listdir(self._dir)):
                if f.startswith(base) and not f.endswith(".lock"):
                    paths.append(os.path.join(self._dir, f))
        else:
            paths = [self.path]
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    for ln in fh:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            out.append(json.loads(ln))
                        except Exception:
                            out.append({"__malformed__": ln})
            except Exception as e:
                _degrade("读取账本失败 %s: %s" % (p, e))
        return out

    def stats(self):
        """按出口态统计。"""
        s = {COMMITTED: 0, SHORT_CIRCUIT: 0, ERROR: 0, "__malformed__": 0}
        for r in self.read_all():
            if "__malformed__" in r:
                s["__malformed__"] += 1
            else:
                s[r.get("outcome", "?")] = s.get(r.get("outcome", "?"), 0) + 1
        return s


def rec_key(rec):
    """账目唯一键：seq 只在单次进程运行内唯一，必须与 run_id 组合。

    v3.2 之前写入的记录没有 run_id 字段，回退用 pid 当运行标识
    （同一 pid 重启重号概率远低于裸 seq；真撞了还有 ts 可人工区分）。
    """
    rid = rec.get("run_id")
    if not rid:
        pid = rec.get("pid")
        rid = "pid:%s" % pid if pid is not None else "unknown"
    return (rid, rec.get("seq"))


# ── 单例 ──
_LEDGER = None


def get_ledger(path=None, enabled=True):
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = WriteLedger(path=path, enabled=enabled)
    return _LEDGER


def set_enabled(flag):
    """消融实验用：运行时开关账本。"""
    led = get_ledger()
    if flag:
        led.enabled = True
    else:
        led.flush()          # 关闭前先把尾巴落盘，避免丢最后一批
        led.enabled = False
    return led.enabled


# ── 安全包装：调用侧唯一入口，账本对象整个坏掉也只降级告警 ──
def _safe(method, *a, **k):
    try:
        return getattr(get_ledger(), method)(*a, **k)
    except Exception as e:
        _degrade("账本调用失败(%s): %s" % (method, e))
        return False


def led_committed(actor, target, verify="", extra=None):
    return _safe("committed", actor, target, verify=verify, extra=extra)


def led_short_circuit(actor, target, reason, extra=None):
    return _safe("short_circuit", actor, target, reason=reason, extra=extra)


def led_error(actor, target, reason, extra=None):
    return _safe("error", actor, target, reason=reason, extra=extra)


if __name__ == "__main__":
    import tempfile
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "t.jsonl")
    led = WriteLedger(path=p, enabled=True)
    led.committed("add", "frag:1", verify="read_back:ok(len=9)")
    led.short_circuit("add", "frag:2", reason="text_too_short")
    led.short_circuit("add", "frag:3", reason="exact_dup")
    led.error("add", "frag:4", reason="db_locked")
    print("stats:", led.stats())
    rows = led.read_all()
    print("rows:", len(rows))
    for r in rows:
        print("  ", r["seq"], r["outcome"], r["target"], r.get("reason"))
    assert len(rows) == 4
    assert led.stats()[COMMITTED] == 1
    assert led.stats()[SHORT_CIRCUIT] == 2
    assert led.stats()[ERROR] == 1
    # 安全包装：账本整体崩掉也不抛
    class Bomb:
        def committed(self, *a, **k): raise RuntimeError("BOOM")
        short_circuit = error = committed
    globals()["_LEDGER"] = Bomb()
    assert led_committed("x", "y") is False
    assert led_error("x", "y", reason="z") is False
    print("SELFTEST OK (no-raise guarantee verified)")
