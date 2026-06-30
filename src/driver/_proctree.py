"""连根拔起一个进程及其全部后代 —— 防黑盒进程留下孤儿子进程。

背景：许多打包工具（如 PyInstaller onefile）启动时父 bootloader 会 fork 出真正跑业务的
子进程。普通的 terminate()/kill() 只杀父进程，子进程被 reparent 到 init 继续运行，
可能导致意料之外的行为（如重复下单触发风控）。

修复：停止时按进程树枚举出全部后代，整棵一起杀。Linux 走 /proc 枚举 ppid 链；其它平台
退化为只对给定 root pid 发信号。
"""
from __future__ import annotations
import os
import signal
import sys
import time


def _read_ppid(pid: int):
    """读 /proc/<pid>/stat 拿父 pid。comm 字段被括号包裹且可能含空格/右括号，
    故从最后一个 ')' 之后再切字段（state, ppid, ...）。失败返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 2:].split()
    try:
        return int(fields[1])   # 右括号后：fields[0]=state, fields[1]=ppid
    except (IndexError, ValueError):
        return None


def _descendants(root_pids) -> set:
    """返回 root_pids 及其全部后代（含自身）。仅 Linux（依赖 /proc）。"""
    parent_of = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {int(p) for p in root_pids}
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        ppid = _read_ppid(pid)
        if ppid is not None:
            parent_of[pid] = ppid
    children: dict[int, list] = {}
    for pid, ppid in parent_of.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [int(p) for p in root_pids if p and int(p) > 0]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    return seen


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_process_tree(root_pids, graceful: bool = True, grace: float = 1.5) -> None:
    """杀掉 root_pids 及其全部后代。

    graceful=True：先 SIGTERM 整棵树，最多等 `grace` 秒，对仍存活者补 SIGKILL；
    graceful=False：直接 SIGKILL。
    Linux 用 /proc 枚举后代；其它平台只对给定 root_pids 发信号（无法枚举孙子）。
    杀 SIGKILL 前会重新枚举一次，捕获 graceful 等待期间新 fork 出来的子进程。
    """
    roots = [int(p) for p in root_pids if p and int(p) > 0]
    if not roots:
        return
    on_linux = sys.platform.startswith("linux")
    targets = _descendants(roots) if on_linux else set(roots)

    if graceful:
        for p in targets:
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(_alive(p) for p in targets):
                return
            time.sleep(0.1)
        if on_linux:   # 等待期间可能又 fork 了新子，重扫一遍
            targets = _descendants(roots) | targets

    for p in targets:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
