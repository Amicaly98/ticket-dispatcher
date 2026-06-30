"""进程清理回归：停止黑盒进程时必须连根杀掉整棵进程树。

背景：许多打包工具（如 PyInstaller onefile）父 bootloader 会 fork 出真正跑业务的子进程。
旧逻辑只杀父 pid → 子进程变孤儿继续运行。
"""
import os
import subprocess
import sys
import time

import pytest

from src.driver._proctree import _descendants, kill_process_tree

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="进程树枚举依赖 /proc（仅 Linux）")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── 核心：kill_process_tree 连子带孙一起杀 ──

@linux_only
def test_kill_process_tree_reaps_descendants():
    """父进程 fork 一个子进程（模拟 onefile bootloader → 真实 app），
    只把父 pid 交给 kill_process_tree，子进程也必须被杀。"""
    code = (
        "import os,subprocess,sys,time\n"
        "c=subprocess.Popen(['sleep','60'])\n"
        "sys.stdout.write('%d %d\\n' % (os.getpid(), c.pid));sys.stdout.flush()\n"
        "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        line = p.stdout.readline().decode().split()
        ppid, cpid = int(line[0]), int(line[1])
        assert _alive(ppid) and _alive(cpid)
        # 关键断言：只给父 pid，子进程（孙）也被连根拔起
        assert cpid in _descendants([ppid])

        kill_process_tree([ppid], graceful=False)

        deadline = time.time() + 5
        p.wait(timeout=5)
        while time.time() < deadline and _alive(cpid):
            time.sleep(0.1)
        assert not _alive(cpid), "子进程没被杀掉 → 会变孤儿继续运行"
    finally:
        for pid in (locals().get("cpid"), locals().get("ppid")):
            if pid:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        if p.poll() is None:
            p.kill()


def test_kill_process_tree_empty_is_noop():
    kill_process_tree([], graceful=False)
    kill_process_tree([0, None], graceful=True)   # 不抛异常即可
