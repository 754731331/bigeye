# -*- coding: utf-8 -*-
"""
bash/cmd 后端路由判定（v3 定稿，已并入 tools/bash.py，2026-10-14）

调用关系：tools/bash.py 内 `_pick_shell / _build_argv / _why` 从本模块导入；
回滚：copy tools\bash.py.bak_before_gitbash_router tools\bash.py
调试：环境变量 BIGEYE_SHELL_DEBUG=1 可打印每次路由决策。

设计前提（均由实测确立，见 bash后端路由规则（实测）.md）：
  1. 默认后端 = Git Bash，启动时带 MSYS2_ARG_CONV_EXCL=* + MSYS_NO_PATHCONV=1
     → Windows 原生 exe（tasklist/netstat/sc/reg/wmic/powershell/robocopy...）参数透传正常，
       因此这些**不进 cmd 表**，一律交 bash。
  2. 只有三类必须回落 cmd：
     ① bash 里不存在的 cmd 内建（dir/del/copy/ver/cls...）
     ② bash 里存在但**语义不同**的（type 查命令类型 ≠ type 读文件）
     ③ bash 下**静默错误**的（%VAR% 原样输出、set X=1 无操作、>nul 真建文件、单个 & 变后台）

实测依据速查：
  dir /b  → bash rc=2  "dir: cannot access '/b'"
  cd /d   → bash rc=1  "cd: too many arguments"
  type f  → bash rc=1  "type: f: not found"
  del/copy/rd/mklink/chcp/ver/vol/prompt/cls/color/title/pause/assoc/ftype
          → bash rc=127 "command not found"
  findstr → bash 下不可靠：`netstat | findstr LISTEN` 能跑，但 `findstr /i "x" f` 报
            "FINDSTR: 无法打开 x"（参数解析通）→ 一律送 cmd
  echo %PATH% → bash rc=0 但输出字面量 '%PATH%'（静默错误）
  set X=1     → bash rc=0 无任何效果（静默错误）
  echo hi >nul → bash rc=0 且**生成名为 nul 的 0 字节文件**（只能 bash rm 删）
  echo A & echo B → bash 并发，实际输出 B\nA（顺序错乱）
  tree: bash 下 command not found（Git 无 tree）→ 留 cmd 表
  label/doskey: bash 下 rc=0 但行为不对（label 会读 stdin 交互）→ 留 cmd 表
  sort: **已从 cmd 表移除**——bash 下是 MSYS sort（POSIX 语义，正常）；
       若命令同时含 cmd 内建，cmd 分支会靠 PATH 前缀拿到同一个 MSYS sort，结果一致。

【探针25 关键发现：cmd 分支 PATH 前置 Git usr/bin】
  cmd 下原本 `dir /b | head` 报 "'head' 不是内部或外部命令"；
  把 `C:/Program Files/Git/usr/bin` 前置到 PATH 后，head/tail/cat/grep/wc/sed/awk/
  cut/sort/uniq/tr/xargs/tee/basename 在 **cmd 里也能用**，于是「cmd 内建 + posix 管道」
  这类混合命令也能跑通。已实测无回归：dir/findstr/where/attrib/tasklist/sc/reg/
  systeminfo/ping/netstat/cmd /c/`&`/chcp 全部正常。
  唯一语义漂移：`find` `sort` 在 cmd+前缀下变成 MSYS 版（原本是 Windows 版）。
  规避：`sort` 已从 cmd 表移除；`find` 本就交 bash。
"""
import os
import re

# ─────────────────────────── cmd 特征词表 ───────────────────────────
# 词边界：中文/引号/空格/管道/重定向都算边界，字母数字下划线连字符不算
_NB = r'(?<![A-Za-z0-9_\-])'
_NE = r'(?![A-Za-z0-9_\-])'
# 命令起始位置：行首、&& / || / 单| / ; 之后
_PRE = r'(?:^|&&|\|\||[&|;])'

CMD_PATTERNS = [
    # ── ① cmd 内建 / 在 bash 中不存在 ──
    (_NB + r'(?:dir|del|erase|copy|move|ren|rename|rd|rmdir|md|chcp|mklink|'
           r'ver|vol|prompt|cls|title|color|pause|call|goto|shift|setlocal|endlocal|'
           r'doskey|assoc|ftype|subst|openfiles|label|mode|choice|clip|tree|'
           r'typeperf|waitfor|forfiles|findstr|rem)' + _NE,
     'cmd 内建（bash 中 command not found 或行为错误）'),

    # ── ② 语义冲突：bash 的 type 是"查命令类型"，不是"读文件" ──
    #     但 `type -p` / `type -a` 是合法 bash 用法，放行
    (_NB + r'type\s+[^-]', 'type 读文件（与 bash 内建 type 语义冲突）'),

    # ── ③ cmd 开关形态（bash 下必然失败）──
    (_NB + r'dir\s+/[a-z]', 'dir /b /s 等开关'),
    (_NB + r'cd\s+/d' + _NE, 'cd /d'),
    (_NB + r'(?:rd|rmdir)\s+/', 'rd /s'),
    (_NB + r'(?:del|erase)\s+/[fqsa]', 'del /q /f'),
    (_NB + r'copy\s+/[a-z]', 'copy /y'),
    (_NB + r'(?:xcopy|robocopy)\s+/', 'xcopy /e、robocopy /E'),

    # ── ④ set 变量赋值（bash 下静默无操作）──
    (_NB + r'set\s+(?:/a\s+|/[pP]\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=', 'set 变量赋值'),

    # ── ⑤ echo 的 cmd 专属写法 ──
    (_NB + r'echo\s+off' + _NE, 'echo off'),
    (_NB + r'echo\.\s*(?:$|[&|])', 'echo. 输出空行'),
    (_NB + r'@echo' + _NE, '@echo'),

    # ── ⑥ 批处理控制流 ──
    (_PRE + r'\s*start\s', 'start 启动程序'),
    (_PRE + r'\s*for\s+%%?[A-Za-z]\s+in\s*\(', '批处理 for %x in (...)'),
    (_PRE + r'\s*if\s+.*?(?:==|exist\s|not\s+exist|%%?[A-Za-z])', '批处理 if'),
    (_NB + r'goto\s+\S', 'goto'),
    (_NB + r'exit\s+/b' + _NE, 'exit /b'),
    (_PRE + r'\s*call\s+\S', 'call'),

    # ── ⑦ nul 重定向（bash 下真建 nul 文件，且该文件 Windows 下删不掉）──
    (r'>>?\s*[Nn][Uu][Ll]' + _NE, '>nul 重定向'),
    (r'2\s*>\s*[Nn][Uu][Ll]' + _NE, '2>nul 重定向'),
    # 注：不拦 `2>&1`——那是 bash 合法且高频的用法（`cmd 2>&1 | head`）。
    #     曾加过 `>\s*&\s*\d` 规则，实测误伤 8/31 条命令（探针22），已删除。

    # ── ⑧ 单个 &（cmd=顺序执行 / bash=后台并发，输出顺序会乱）──
    #     只在两侧都有空白时判定，避免误伤 URL 里的 ?a=1&b=2
    (r'(?<!&)\s&\s(?!&)', '单个 &（两侧空白）'),
]

_CMD_RE = re.compile('|'.join(f'(?:{p})' for p, _ in CMD_PATTERNS), re.IGNORECASE)
_CMD_LABELS = [(re.compile(p, re.IGNORECASE), lab) for p, lab in CMD_PATTERNS]

# ── ⑨ %VAR% 展开（单独处理：需与 strftime 格式串区分）──
_VAR_RE = re.compile(r'(?<!\+)%[A-Za-z_][A-Za-z0-9_]{1,}%')
# git 的 --pretty=%H / --format=%an 等格式串，误伤豁免
_GIT_FMT_RE = re.compile(r'\bgit\s+(?:log|show|diff|rev-list|for-each-ref|branch|tag)\b')


def _why(command: str) -> str:
    """返回命中原因，调试/日志用。"""
    for r, lab in _CMD_LABELS:
        m = r.search(command)
        if m:
            return f'{lab} → {m.group(0)!r}'
    if _VAR_RE.search(command) and not _GIT_FMT_RE.search(command):
        return f'%VAR% 展开 → {_VAR_RE.search(command).group(0)!r}'
    return '无 cmd 特征'


def _pick_shell(command: str) -> str:
    """返回 'bash' 或 'cmd'。非 Windows 平台恒返回 'bash'。

    行首是第一道闸：危险命令护栏 _check_danger() 在路由**之前**执行，行为不变。
    """
    if os.name != 'nt':
        return 'bash'
    if _CMD_RE.search(command):
        return 'cmd'
    if _VAR_RE.search(command) and not _GIT_FMT_RE.search(command):
        return 'cmd'
    return 'bash'


def _build_argv(command: str, shell: str):
    """构造 Popen 参数。返回 (argv, env_extra, use_shell)。

    bash 分支用 list（shell=False）：引号/管道/重定向全交给 bash 自己解析。
    cmd  分支保持 shell=True（与现状完全一致，零回归）：
        实测（探针22b）cmd 用 list 模式会踩引号地狱——
        `cmd /d /s /c 'dir /b tools | findstr /i "bash"'` rc=1 空输出，
        `cd /d "E:\\... (2)" && cd` rc=1 报路径错；
        而 shell=True 全部正常，因为 Python 传的是未经 list2cmdline 处理的原始串。
    非 Windows 平台：直通系统 shell（不改行为）。
    """
    if os.name != 'nt':
        # POSIX：直接用系统 shell，与改造前行为完全一致（模块在非 Windows 上无意义）
        return command, {}, True
    if shell == 'cmd':
        # 前置 Git usr/bin：让 cmd 分支也能用 head/tail/grep/cat 等 posix 读取工具，
        # 从而兼容「cmd 内建 + posix 管道」的混合命令（探针25 实测）。
        # 注意返回的是**完整 PATH**，避免调用方 `{**os.environ, **extra}` 时把原 PATH 抹掉。
        return 'chcp 65001 >nul 2>&1 && ' + command, _path_env(), True
    gb = find_git_bash()
    if not gb:
        # 找不到 Git Bash 时优雅退化回 cmd（绝不抛异常打断工具调用）
        return 'chcp 65001 >nul 2>&1 && ' + command, _path_env(), True
    return [gb, '--noprofile', '--norc', '-c', command], {
        'MSYS2_ARG_CONV_EXCL': '*',   # 关掉 /xxx → 路径 的自动转换（闸门级）
        'MSYS_NO_PATHCONV': '1',      # 同上，双保险
        'LANG': 'en_US.UTF-8',
        'PYTHONIOENCODING': 'utf-8',
    }, False


# ── Git Bash 定位（注意：绝不能落到 C:\Windows\System32\bash.exe，那是 WSL 启动器）──
_GIT_BASH_CANDIDATES = [
    r'C:\Program Files\Git\bin\bash.exe',
    r'C:\Program Files (x86)\Git\bin\bash.exe',
    r'C:\Program Files\Git\usr\bin\bash.exe',
]
_git_bash_cache = None


def find_git_bash():
    """找到 Git Bash 的 bash.exe，找不到返回 None（调用方退化回 cmd）。"""
    global _git_bash_cache
    if _git_bash_cache is not None:
        return _git_bash_cache or None
    for p in _GIT_BASH_CANDIDATES:
        if os.path.isfile(p):
            # 排除 WSL 启动器：真正的 Git Bash 同目录下会有 ..\usr\bin 或 ..\..\usr\bin
            base = os.path.dirname(os.path.dirname(p))
            if os.path.isdir(os.path.join(base, 'usr', 'bin')):
                _git_bash_cache = p
                return p
    _git_bash_cache = ''
    return None


def find_git_usr_bin():
    """找到 Git 的 usr/bin（posix 工具目录），供 cmd 分支 PATH 前置。找不到返回 None。"""
    gb = find_git_bash()
    if not gb:
        return None
    for cand in (os.path.join(os.path.dirname(os.path.dirname(gb)), 'usr', 'bin'),
                 r'C:\Program Files\Git\usr\bin'):
        if os.path.isfile(os.path.join(cand, 'head.exe')):
            return cand
    return None


_path_env_cache = None


def _path_env():
    """cmd 分支的 env 增量：把 Git usr/bin 前置到 PATH（找不到则返回空 dict，行为不变）。
    返回**完整** PATH 字符串，调用方直接 merge 也不会丢原 PATH。"""
    global _path_env_cache
    if _path_env_cache is None:
        pre = find_git_usr_bin()
        if pre:
            _path_env_cache = {'PATH': pre + os.pathsep + os.environ.get('PATH', '')}
        else:
            _path_env_cache = {}
    return dict(_path_env_cache)


# ─────────────────────────── 自测 ───────────────────────────
if __name__ == '__main__':
    CASES = [
        # (命令, 期望, 说明)
        ('head -20 CHANGELOG.jsonl', 'bash', 'posix'),
        ('ls -la tools/', 'bash', 'posix'),
        ('grep -n "def bash" tools/bash.py', 'bash', 'posix'),
        ('find . -maxdepth 1 -name "*.db"', 'bash', 'MSYS find'),
        ('cat config.py | head -5', 'bash', 'posix 管道'),
        ('netstat -ano | findstr LISTEN', 'cmd', 'findstr 参数解析在 bash 下不可靠，送 cmd'),
        ('tasklist /FI "IMAGENAME eq python.exe" /NH', 'bash', '原生 exe 透传'),
        ('powershell -NoProfile -Command "Get-Date"', 'bash', '原生 exe 透传'),
        ('reg query HKLM\\Software', 'bash', '原生 exe 透传'),
        ('sc query', 'bash', '原生 exe 透传'),
        ('schtasks /query', 'bash', '原生 exe 透传'),
        ('systeminfo', 'bash', '原生 exe'),
        ('date +%Y%m%d', 'bash', 'strftime 不误判'),
        ('git log --pretty=%H | head -3', 'bash', 'git 格式串豁免'),
        ('echo A && echo B', 'bash', '&& 两者都支持'),
        ('mkdir -p a/b/c', 'bash', 'mkdir vs md'),
        ('mv a b', 'bash', 'mv vs move'),
        ('rm -rf x', 'bash', 'rm vs del'),
        ('cp -r a b', 'bash', 'cp vs copy'),
        ('npm start', 'bash', 'npm 的 start 不误判'),
        ('type -p python', 'bash', 'bash type -p 合法'),
        ('awk "BEGIN{print 2^3}"', 'bash', '^ 不误判'),
        ('curl -s "https://x.com/a?b=1&c=2"', 'bash', 'URL 的 & 不误判'),
        ('for f in *.txt; do echo $f; done', 'bash', 'bash for（$var 形态）'),
        ('echo hi > /dev/null', 'bash', '/dev/null 是 posix'),
        # —— cmd ——
        ('dir', 'cmd', '内建'),
        ('dir /b tools', 'cmd', 'dir 开关'),
        ('cd /d D:\\', 'cmd', 'cd /d'),
        ('type tools\\bash.py', 'cmd', 'type 读文件'),
        ('del /q x.tmp', 'cmd', 'del'),
        ('copy a b', 'cmd', 'copy'),
        ('move a b', 'cmd', 'move'),
        ('ren a b', 'cmd', 'ren'),
        ('rd /s /q x', 'cmd', 'rd'),
        ('md newdir', 'cmd', 'md'),
        ('chcp 65001', 'cmd', 'chcp'),
        ('mklink a b', 'cmd', 'mklink'),
        ('ver', 'cmd', '内建'),
        ('cls', 'cmd', '内建'),
        ('title x', 'cmd', '内建'),
        ('color 0a', 'cmd', '内建'),
        ('pause', 'cmd', '内建'),
        ('echo %PATH%', 'cmd', '%VAR% 静默错误'),
        ('echo %CD%', 'cmd', '%VAR% 静默错误'),
        ('set X=1', 'cmd', 'set 静默无操作'),
        ('set /a n=1+2', 'cmd', 'set /a'),
        ('echo off', 'cmd', 'echo off'),
        ('@echo off', 'cmd', '@echo'),
        ('echo.', 'cmd', 'echo. 空行'),
        ('echo hi >nul', 'cmd', '>nul 副作用'),
        ('echo hi 2>nul', 'cmd', '2>nul 副作用'),
        ('echo A & echo B', 'cmd', '单个 & 顺序语义'),
        ('sort -u', 'bash', 'sort 交 bash（MSYS 版）'),
        ('start "" notepad', 'cmd', 'start'),
        ('tree /f', 'cmd', 'bash 无 tree'),
        ('rem 注释', 'cmd', 'rem 内建'),
        ('if exist x.txt echo yes', 'cmd', '批处理 if'),
        ('for %f in (*.txt) do echo %f', 'cmd', '批处理 for'),
        ('goto :eof', 'cmd', 'goto'),
        ('call build.bat', 'cmd', 'call'),
        ('exit /b 0', 'cmd', 'exit /b'),
        ('xcopy /e a b', 'cmd', 'xcopy 开关'),
        ('assoc .txt', 'cmd', '内建'),
        ('label', 'cmd', 'bash 下会读 stdin'),
        ('doskey', 'cmd', 'bash 下无效果'),
    ]
    bad = []
    for c, exp, desc in CASES:
        got = _pick_shell(c)
        if got != exp:
            bad.append((c, exp, got, desc, _why(c)))
    print(f'自测 {len(CASES)} 条，错 {len(bad)} 条')
    for c, e, g, d, w in bad:
        print(f'  ❌ 期望 {e} 得到 {g}: {c!r}  ({d})  [{w}]')
    if not bad:
        print('  ✅ 全通过')
    print(f'\nGit Bash: {find_git_bash()}')
    print(f'Git usr/bin: {find_git_usr_bin()}')
    for cmd, shell in [('ls -la | head -3', 'bash'), ('dir /b tools', 'cmd')]:
        argv, extra, use_shell = _build_argv(cmd, shell)
        shown = argv[:3] if isinstance(argv, list) else argv[:60]
        print(f'  [{shell}] shell={use_shell} {shown} ... +env={list(extra)}')
