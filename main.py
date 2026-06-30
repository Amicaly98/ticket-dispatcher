#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ticket-dispatcher — 通用票务调度框架（手动指派台 + 分布式 Worker fleet）。

无自动调度：用户手动把配好的任务指派给指定 worker，worker 实时可见、可开关。

角色：
  python main.py api              # API 节点（FastAPI + Collector，默认 0.0.0.0:8000）
                                  #   InMemoryBus 单机模式下自动起 WorkerAgent
  python main.py worker           # Worker agent 节点（真正的远端执行节点）
  python main.py status           # 查看号池 + worker 心跳（无副作用）

环境变量：
  WORKER_ID     — Worker agent 的 ID（默认 worker_local）
  WORKER_PROGRAM— Worker 跑哪种 Driver（默认查 settings.yaml 声明）
  MAX_SLOTS     — Worker agent 最大并发 slot 数（默认 1）
  REDIS_HOST    — Redis 地址（未设置则用 InMemoryBus，单进程跑通；设了却连不上会直接报错退出）
  REDIS_PORT    — Redis 端口（默认 6379）
  REDIS_PASSWORD— Redis 密码（默认无；设了即用 AUTH，省掉 SSH 隧道）
  API_HOST / API_PORT — API 绑定地址/端口
  DRYRUN        — 设为 1 强制所有 Driver 用 DryRunDriver
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 系统代理会导致 httpx 请求卡死（事件循环阻塞 → API 无响应）。
# 强制禁用代理，让 httpx 直连目标服务器。
os.environ.setdefault('NO_PROXY', '*')
os.environ.setdefault('no_proxy', '*')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def _find_redis_server() -> str:
    """按优先级找 redis-server：PATH > 常见 Windows 安装目录。找不到返回空。"""
    import shutil
    exe = shutil.which("redis-server")
    if exe:
        return exe
    # Windows 常见路径
    for p in (r"C:\Program Files\Redis\redis-server.exe",
              r"C:\Redis\redis-server.exe"):
        if os.path.isfile(p):
            return p
    return ""


def _auto_redis() -> tuple:
    """尝试自动拉起 redis-server，返回 (redis_proc_or_None, password)。

    逻辑：
      1. 如果 REDIS_HOST 已手动设置 → 不干预，返回 (None, password)。
      2. 尝试连 localhost:6379 → 已在跑 → 设 REDIS_HOST=localhost。
      3. 不在跑 → 找 redis-server → 启动（带密码）→ 等就绪 → 设 REDIS_HOST。
      4. 都不行 → 返回 (None, password)，调用方走 InMemoryBus。
    """
    import subprocess, time
    password = os.environ.get("REDIS_PASSWORD", "")
    port = int(os.environ.get("REDIS_PORT", "6379"))

    if os.environ.get("REDIS_HOST"):
        log.info("REDIS_HOST 已设置 (%s)，跳过自动 Redis", os.environ["REDIS_HOST"])
        return None, password

    import socket
    def _try_connect(pwd: str):
        try:
            import redis as redis_lib
            r = redis_lib.Redis(host="127.0.0.1", port=port, password=pwd or None, protocol=2)
            r.ping()
            return r
        except Exception:
            return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", port))
        sock.close()
        r = _try_connect(password) or _try_connect("")
        if r is None:
            log.warning("Redis 在 %d 端口但认证失败，回退 InMemoryBus", port)
            return None, password
        ver = r.info("server").get("redis_version", "0")
        major = int(ver.split(".")[0])
        matched_pwd = password if _try_connect(password) else ""
        r.close()
        if major < 5:
            log.warning("检测到 Redis %s 在 localhost:%d（Streams 需 ≥5.0），回退 InMemoryBus。"
                        "请安装 Redis 7：https://github.com/tporadowski/redis/releases", ver, port)
            return None, matched_pwd
        log.info("检测到 Redis %s 在 localhost:%d 运行，直接用", ver, port)
        os.environ["REDIS_HOST"] = "127.0.0.1"
        if matched_pwd:
            os.environ["REDIS_PASSWORD"] = matched_pwd
        else:
            os.environ.pop("REDIS_PASSWORD", None)
        return None, matched_pwd
    except (ConnectionRefusedError, OSError):
        sock.close()

    redis_exe = _find_redis_server()
    if not redis_exe:
        log.warning("未找到 redis-server，回退 InMemoryBus（单进程模式，不支持跨机器 worker）")
        return None, password

    import tempfile
    conf_lines = [
        f"port {port}", "bind 127.0.0.1",
        "save \"\"", "loglevel warning",
    ]
    if password:
        conf_lines.insert(2, f"requirepass {password}")
    tmp_conf = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
    tmp_conf.write("\n".join(conf_lines) + "\n")
    tmp_conf.close()
    log.info("自动启动 Redis: %s (port=%d)", redis_exe, port)
    proc = subprocess.Popen(
        [redis_exe, tmp_conf.name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(25):
        time.sleep(0.2)
        if proc.poll() is not None:
            log.error("redis-server 启动后立即退出 (code=%d)，回退 InMemoryBus", proc.returncode)
            return None, password
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.settimeout(0.3)
        try:
            s2.connect(("127.0.0.1", port))
            s2.close()
            try:
                import redis as redis_lib
                r = redis_lib.Redis(host="127.0.0.1", port=port, password=password, protocol=2)
                ver = r.info("server").get("redis_version", "0")
                major = int(ver.split(".")[0])
                r.close()
                if major < 5:
                    log.warning("Redis %s 太旧（Streams 需 ≥5.0），回退 InMemoryBus。"
                                "请安装 Redis 7：https://github.com/tporadowski/redis/releases", ver)
                    proc.terminate()
                    return None, password
                log.info("Redis %s 已就绪 (pid=%d)，API 将用 RedisBus", ver, proc.pid)
            except Exception as e:
                log.warning("Redis 版本检查失败 (%s)，假设可用继续", e)
            os.environ["REDIS_HOST"] = "127.0.0.1"
            os.environ["REDIS_PASSWORD"] = password
            return proc, password
        except (ConnectionRefusedError, OSError):
            s2.close()
    log.error("等待 Redis 超时，回退 InMemoryBus")
    proc.terminate()
    return None, password


def cmd_api():
    """启动 API 节点（FastAPI + Collector）。"""
    # 加载平台 API 扩展（注册到 app 的路由会在启动时自动挂载）
    import src.platform.bilibili  # noqa: F401
    from src.api import start_api
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8000"))
    log.info("API 服务启动: http://%s:%s", host, port)
    redis_proc, _ = _auto_redis()
    try:
        start_api(host, port)
    finally:
        if redis_proc and redis_proc.poll() is None:
            log.info("关闭自动启动的 Redis (pid=%d)", redis_proc.pid)
            redis_proc.terminate()
            try:
                redis_proc.wait(timeout=3)
            except Exception:
                redis_proc.kill()


def cmd_worker():
    """启动 Worker agent 节点。"""
    from src.worker_agent import WorkerAgent
    from src.bus import get_bus
    worker_id = os.environ.get("WORKER_ID", "worker_local")
    max_slots = int(os.environ.get("MAX_SLOTS", "1"))
    # program：env 优先；否则查 settings.yaml 里该 worker 的声明；再默认空串
    program = os.environ.get("WORKER_PROGRAM", "")
    public_ip = os.environ.get("PUBLIC_IP", "")
    if not program:
        from src.config import load_settings, load_workers
        decl = {w.worker_id: w for w in load_workers(load_settings())}
        if worker_id in decl:
            program = decl[worker_id].program
            if not public_ip:
                public_ip = decl[worker_id].public_ip
    bus = get_bus()
    agent = WorkerAgent(worker_id, bus, max_slots=max_slots, program=program, public_ip=public_ip)
    log.info("Worker agent 启动: %s (max_slots=%d, program=%s, public_ip=%s)",
             worker_id, max_slots, program or "(未设)", public_ip or "(未设)")
    try:
        agent.run()
    finally:
        bus.close()


def cmd_status():
    """查看号池 + worker 心跳（无副作用）。"""
    from src.config import load_settings, load_accounts, load_workers
    settings = load_settings()
    accounts = load_accounts()
    workers = load_workers(settings)

    print("=" * 50)
    print("  ticket-dispatcher 状态")
    print("=" * 50)
    print(f"  号池: {len(accounts)}")
    for aid, acc in accounts.items():
        buyers = ", ".join(b.name for b in acc.buyers) or "无"
        print(f"    {aid}: {acc.username} (uid={acc.uid}) 购票人=[{buyers}]")
    print(f"  Worker（配置）: {len(workers)}")
    for w in workers:
        print(f"    {w.worker_id}: {w.public_ip} (max={w.max_slots}, {w.program or '(未设)'})")

    try:
        from src.bus import get_bus
        bus = get_bus()
        live = bus.list_workers()
        if live:
            print("\n  Worker 实时心跳:")
            for wid, ws in live.items():
                print(f"    {wid}: {ws.get('status')} (busy {ws.get('busy_slots', 0)}/{ws.get('max_slots', '?')})")
        bus.close()
    except Exception:
        pass
    print("=" * 50)


def main():
    argv = sys.argv[1:]
    cmd = argv[0].lower() if argv else "api"

    if cmd == "status":
        cmd_status()
    elif cmd == "worker":
        cmd_worker()
    elif cmd in ("api", ""):
        cmd_api()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
