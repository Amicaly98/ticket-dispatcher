"""FastAPI 管理 API（降级版：手动指派台）。

无自动调度——API 是无状态转发 + 号池 CRUD：
  - 号池：从 accounts.yaml 强读（权威源），写端点同步落盘
  - 指派：POST /workers/{id}/assign 把配好的抢票单组成 Job 推给指定 worker
  - 控制：POST /workers/{id}/stop 终止 worker 当前在飞 Job
  - 状态：GET /workers 读 bus 心跳（含 last_result），实时
后台只有一个 Collector 线程 drain result → SQLite。WS 推 bus 事件 delta。
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .bus import Bus, get_bus
from .store import Store

log = logging.getLogger("api")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 全局状态（由 lifespan 初始化）
_bus: Optional[Bus] = None
_store: Optional[Store] = None
_collector = None
_worker_configs: dict = {}   # worker_id -> WorkerConfig

# ── 鉴权 ──
API_KEY = os.environ.get("API_KEY", "")


def _read_accounts_public() -> dict:
    """从 YAML 强读账号并剥掉 cookie（账号读的权威源=文件，消除快照滞后窗口）。

    cookie 绝不下发前端——只用 has_cookie 标记登录态。
    """
    from .config import load_accounts
    out = {}
    for aid, acc in load_accounts().items():
        out[aid] = {
            "uid": acc.uid,
            "username": acc.username,
            "program": acc.program,
            "buyers": [{"id": b.id, "name": b.name} for b in acc.buyers],
            "status": acc.status,
            "has_cookie": bool(acc.cookie),
            "vip_status": acc.vip_status,
            "vip_type": acc.vip_type,
            "vip_due_date": acc.vip_due_date,
        }
    return out


def _update_account_fields(account_id: str, fields: dict) -> bool:
    """把 fields merge 进 accounts.yaml 里指定账号。返回是否找到该号。"""
    import yaml
    path = os.path.join(ROOT, "config", "accounts.yaml")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    accounts = data.get("accounts", [])
    found = False
    for i, a in enumerate(accounts):
        if a.get("account_id") == account_id:
            accounts[i] = {**a, **fields}
            found = True
            break
    if not found:
        return False
    data["accounts"] = accounts
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return True


def _check_auth(request: Request):
    """X-API-Key 鉴权：所有写操作（POST/DELETE/PUT/PATCH）必须带 key。"""
    if not API_KEY:
        return  # 未配置 key → 开发模式，不鉴权
    if request.method not in ("POST", "DELETE", "PUT", "PATCH"):
        return
    key = request.headers.get("X-API-Key", "")
    if key != API_KEY:
        raise HTTPException(401, "未授权：缺少或错误的 X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bus, _store, _collector, _worker_configs
    _bus = get_bus()
    _store = Store()

    from .config import load_settings, load_workers
    settings = load_settings()
    _worker_configs = {w.worker_id: w for w in load_workers(settings)}

    import threading
    from .collector import Collector
    _collector = Collector(bus=_bus)
    threading.Thread(target=_collector.start, daemon=True, name="collector").start()

    # InMemoryBus（单进程模式）下为每个配置的 worker 起一个 WorkerAgent，否则派发的 Job 无人消费
    from .bus import InMemoryBus
    if isinstance(_bus, InMemoryBus):
        from .worker_agent import WorkerAgent
        for w in _worker_configs.values():
            agent = WorkerAgent(w.worker_id, _bus, max_slots=w.max_slots,
                                program=w.program, public_ip=w.public_ip)
            threading.Thread(target=agent.run, daemon=True, name=f"worker-{w.worker_id}").start()
            log.info("InMemoryBus 模式：WorkerAgent 已启动 (id=%s, slots=%d)", w.worker_id, w.max_slots)

    yield

    if _collector:
        _collector.stop()
    _store.close()
    _bus.close()


app = FastAPI(title="ticket-dispatcher", lifespan=lifespan)

# ── 平台 API 扩展点 ──
# Driver 作者可以通过 register_api_router() 注册平台特定的端点（如扫码登录、活动查询等）。
# 注册的路由会在 lifespan 启动时自动挂载到 app 上。
# 用法：
#   from src.api import register_api_router
#   from fastapi import APIRouter
#   router = APIRouter(prefix="/bilibili", tags=["bilibili"])
#   @router.post("/qr/gen")
#   async def qr_gen(): ...
#   register_api_router(router)
_pending_routers: list = []


def register_api_router(router) -> None:
    """注册平台特定的 API 路由。在 Driver 模块初始化时调用，路由会在 app 启动时自动挂载。"""
    _pending_routers.append(router)


# 在 lifespan 中挂载已注册的路由
_original_lifespan = lifespan


@asynccontextmanager
async def _lifespan_with_routers(application: FastAPI):
    for r in _pending_routers:
        application.include_router(r)
        log.info("已挂载扩展路由: prefix=%s", getattr(r, 'prefix', '?'))
    async with _original_lifespan(application) as ctx:
        yield ctx


# 替换 lifespan
app.router.lifespan_context = _lifespan_with_routers

# CORS（前端 dashboard 需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# 鉴权中间件
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    _check_auth(request)
    return await call_next(request)


# ═══════════════════════════════════════════
#  Worker 控制台 API（指派 + 停止 + 实时状态）
# ═══════════════════════════════════════════

@app.get("/workers")
def list_workers():
    """配置声明的 worker ∪ bus 心跳里的活 worker。

    worker 自注册：起 `main.py worker` 即往 bus 发心跳，无需在 settings.yaml 登记就能被指派。
    settings.yaml 仅提供 program/max_slots/public_ip 默认值与「离线占位」展示；活 worker 以心跳为准。
    """
    live = _bus.list_workers() if _bus else {}
    out = {}
    # 1) 配置声明的 worker：默认 offline，有心跳则用心跳覆盖
    for wid, wc in _worker_configs.items():
        out[wid] = {"worker_id": wid, "status": "offline", "busy_slots": 0,
                    "max_slots": wc.max_slots, "program": wc.program, "public_ip": wc.public_ip}
        if wid in live:
            out[wid].update(live[wid])
    # 2) 没在配置里但有心跳的 worker（纯自注册）：直接采信心跳
    for wid, w in live.items():
        out.setdefault(wid, {"public_ip": "", **w})
    return out


@app.get("/workers/{worker_id}")
def get_worker(worker_id: str):
    workers = list_workers()
    if worker_id in workers:
        return workers[worker_id]
    raise HTTPException(404, "Worker 不存在")


@app.post("/workers/{worker_id}/assign")
def assign_worker(worker_id: str, body: dict):
    """把一张配好的抢票单指派给指定 worker。

    号-worker 多对多自由指派，系统不做任何防重。body：
      {account_id, buyer_ids[], project_id, screen_id, sku_id, pay_money,
       deadline(ISO 或 epoch), label?, program?, retry_interval?, check_interval?}
    deadline 透传给黑盒 bot，由 bot 自己 NTP 倒计时卡点。
    """
    from .config import load_accounts, _parse_time
    from .models import Job
    if not _bus:
        raise HTTPException(503, "服务未就绪")
    account_id = body.get("account_id", "")
    acc = load_accounts().get(account_id)
    if not acc:
        raise HTTPException(404, f"账号 {account_id} 不存在")
    buyer_ids = body.get("buyer_ids", [])
    buyers = [b for b in acc.buyers if b.id in buyer_ids] if buyer_ids else list(acc.buyers)
    if not buyers:
        raise HTTPException(400, "未指定有效购票人")
    if not body.get("project_id"):
        raise HTTPException(400, "缺少 project_id")
    wc = _worker_configs.get(worker_id)
    # program 优先级：① body 显式 → ② worker 心跳实际上报（自注册 worker 的唯一真相）
    # → ③ settings.yaml 声明 → ④ 账号默认。
    live_program = (_bus.list_workers().get(worker_id, {}) or {}).get("program") if _bus else None
    program = (body.get("program") or live_program
               or (wc.program if wc else None) or acc.program)
    label = body.get("label") or f"项目{body.get('project_id')}"
    job = Job.build(
        job_id=f"job-{int(time.time() * 1000)}",
        worker_id=worker_id,
        program=program,
        task_id=label,
        project_id=int(body["project_id"]),
        screen_id=int(body.get("screen_id", 0)),
        sku_id=int(body.get("sku_id", 0)),
        screen_name=body.get("screen_name", ""),
        sku_desc=body.get("sku_desc", ""),
        count=len(buyers),
        pay_money=int(body.get("pay_money", 0)),
        deadline=_parse_time(body.get("deadline", 0)),
        account=acc.snapshot(),
        buyers=buyers,
        retry_interval=float(body.get("retry_interval", 0.3)),
        check_interval=float(body.get("check_interval", 3.0)),
        enable_check_stock=bool(body.get("enable_check_stock", False)),
    )
    _bus.push_job(job)
    if _store:
        _store.record_attempt(job)
    _bus.publish({"type": "job_dispatched", "job_id": job.job_id,
                  "task_id": job.task_id, "worker_id": worker_id})
    log.info("指派 job=%s -> worker=%s account=%s buyers=%d",
             job.job_id, worker_id, account_id, len(buyers))
    return {"status": "ok", "job_id": job.job_id, "worker_id": worker_id, "buyers": len(buyers)}


@app.post("/workers/{worker_id}/stop")
def stop_worker(worker_id: str):
    """停止 worker 当前在飞 Job（立即终止，随即回 IDLE）。"""
    if not _bus:
        raise HTTPException(503, "服务未就绪")
    from .models import ControlSignal
    _bus.push_signal(ControlSignal(signal="cancel", worker_id=worker_id))
    _bus.publish({"type": "worker_stop_requested", "worker_id": worker_id})
    return {"status": "ok", "message": f"Worker {worker_id} 停止指令已发送"}


@app.post("/workers/{worker_id}/pause")
def pause_worker(worker_id: str):
    """暂停 worker：停止消费新 Job（在飞 Job 继续跑完，进程不退）。用于异常处理临时摘下。"""
    if not _bus:
        raise HTTPException(503, "服务未就绪")
    from .models import ControlSignal
    _bus.push_signal(ControlSignal(signal="pause", worker_id=worker_id))
    _bus.publish({"type": "worker_pause_requested", "worker_id": worker_id})
    return {"status": "ok", "message": f"Worker {worker_id} 暂停指令已发送"}


@app.post("/workers/{worker_id}/resume")
def resume_worker(worker_id: str):
    """恢复 worker：重新开始消费新 Job。"""
    if not _bus:
        raise HTTPException(503, "服务未就绪")
    from .models import ControlSignal
    _bus.push_signal(ControlSignal(signal="resume", worker_id=worker_id))
    _bus.publish({"type": "worker_resume_requested", "worker_id": worker_id})
    return {"status": "ok", "message": f"Worker {worker_id} 恢复指令已发送"}


@app.post("/workers/{worker_id}/logs/watch")
def watch_worker_logs(worker_id: str, ttl: float = 30.0):
    """登记「正在看 worker_id 的实时日志」（前端打开日志面板时调，周期续期）。

    按需订阅：worker 仅在被 watch 时才推 worker_log 流，把 N 个 worker 同时刷屏的日志洪流
    降到只推正在被查看的那台。RedisBus 真生效；InMemory 单机恒推（no-op）。续期周期 < ttl 即可。"""
    if not _bus:
        raise HTTPException(503, "服务未就绪")
    _bus.mark_log_interest(worker_id, ttl=ttl)
    return {"status": "ok", "worker_id": worker_id, "ttl": ttl}


def _reload_worker_configs():
    """重读 settings.yaml 刷新内存里的 worker 声明（创建/删除声明后调用，让 /workers 立即反映）。"""
    global _worker_configs
    from .config import load_settings, load_workers
    _worker_configs = {w.worker_id: w for w in load_workers(load_settings())}


@app.post("/workers")
def create_worker(body: dict):
    """创建（声明）一个 worker：写入 settings.yaml，控制台立即显示离线占位。

    声明式——不远程拉起进程。worker 真正上线仍需在那台机器起 `main.py worker`（自注册）；
    本端点只是把它登记好（设 program/max_slots/public_ip 默认值 + 占个离线位）。
    body: {worker_id, program?, max_slots?, public_ip?}
    """
    from .config import save_worker_decl
    worker_id = (body.get("worker_id") or "").strip()
    if not worker_id:
        raise HTTPException(400, "缺少 worker_id")
    if worker_id in _worker_configs:
        raise HTTPException(409, f"Worker {worker_id} 已存在")
    decl = {"worker_id": worker_id,
            "program": body.get("program") or "",
            "max_slots": int(body.get("max_slots", 1)),
            "public_ip": body.get("public_ip", "")}
    save_worker_decl(decl)
    _reload_worker_configs()
    if _bus:
        _bus.publish({"type": "worker_declared", "worker_id": worker_id})
    log.info("声明 worker=%s program=%s slots=%d", worker_id, decl["program"], decl["max_slots"])
    return {"status": "ok", "worker_id": worker_id,
            "hint": f"去 worker 机器上运行：WORKER_ID={worker_id} MAX_SLOTS={decl['max_slots']} "
                    f"WORKER_PROGRAM={decl['program']} python main.py worker"}


@app.delete("/workers/{worker_id}")
def delete_worker(worker_id: str):
    """删除 worker：从 settings.yaml 移除声明 + 从 bus 心跳清掉（抹掉控制台上的离线僵尸）。

    注意：不会杀掉远端真在跑的 worker 进程——它若仍活着会在下次心跳重新出现（符合预期：
    要彻底移除得先去那台机器停掉进程）。
    """
    from .config import delete_worker_decl
    removed_decl = delete_worker_decl(worker_id)
    _reload_worker_configs()
    if _bus:
        _bus.forget_worker(worker_id)
        _bus.publish({"type": "worker_deleted", "worker_id": worker_id})
    if not removed_decl:
        # 声明里没有，但可能是个纯自注册的僵尸——forget_worker 已尝试清心跳
        log.info("删除 worker=%s（无 settings 声明，已清心跳）", worker_id)
    return {"status": "ok", "worker_id": worker_id}


# ═══════════════════════════════════════════
#  账号管理 API（号池）
# ═══════════════════════════════════════════

@app.get("/accounts")
def list_accounts():
    # 直接从 YAML 强读（权威源），写端点同步落盘后立即可见——无快照滞后窗口。
    return _read_accounts_public()


@app.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    """删除账号（从 accounts.yaml 移除）。"""
    import yaml
    path = os.path.join(ROOT, "config", "accounts.yaml")
    if not os.path.exists(path):
        raise HTTPException(404, "accounts.yaml 不存在")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    accounts = data.get("accounts", [])
    before = len(accounts)
    data["accounts"] = [a for a in accounts if a.get("account_id") != account_id]
    if len(data["accounts"]) == before:
        raise HTTPException(404, f"账号 {account_id} 不存在")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    if _bus:
        _bus.publish({"type": "account_deleted", "account_id": account_id})
    return {"status": "ok"}


@app.post("/accounts/save")
def save_account(body: dict):
    """创建或更新账号（写入 accounts.yaml）。"""
    import yaml
    account_id = body.get("account_id", "")
    if not account_id:
        raise HTTPException(400, "缺少 account_id")
    path = os.path.join(ROOT, "config", "accounts.yaml")
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    accounts = data.get("accounts", [])
    found = False
    for i, a in enumerate(accounts):
        if a.get("account_id") == account_id:
            accounts[i] = {**a, **body}
            found = True
            break
    if not found:
        accounts.append(body)
    data["accounts"] = accounts
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    if _bus:
        _bus.publish({"type": "account_saved", "account_id": account_id})
    return {"status": "ok", "account_id": account_id}


# ═══════════════════════════════════════════
#  状态 / 聚合 / 历史
# ═══════════════════════════════════════════

@app.get("/status")
def get_status():
    """聚合状态：workers（实时心跳）+ accounts（号池强读）。"""
    return {"workers": list_workers(), "accounts": _read_accounts_public()}


@app.get("/metrics")
def get_metrics():
    """全局聚合指标：成功率、reason 分布、worker 状态。

    结果指标走 SQL 聚合（Store.metrics()），不再每次拉上万行——前端每 2s 轮询，
    旧实现随库增长 + 持 GIL 会拖垮事件循环导致 WS/HTTP 断连。
    """
    if not _store:
        return {}
    m = _store.metrics()
    worker_stats = {}
    for ws in list_workers().values():
        status = ws.get("status", "unknown")
        worker_stats[status] = worker_stats.get(status, 0) + 1
    m["worker_status_counts"] = worker_stats
    return m


@app.get("/history/results")
def history_results(worker_id: Optional[str] = None, limit: int = 50):
    if not _store:
        return []
    return _store.get_recent_results(limit=limit)


@app.get("/history/workers")
def history_workers(worker_id: Optional[str] = None, limit: int = 50):
    if not _store:
        return []
    return _store.get_worker_events(worker_id, limit=limit)


# ═══════════════════════════════════════════
#  事件流 + WebSocket
# ═══════════════════════════════════════════

@app.get("/events")
def recent_events(n: int = 100, since_seq: int = 0):
    """获取事件。since_seq > 0 时增量返回（只返回 seq > since_seq 的事件，Lua 服务端过滤）。"""
    if not _bus:
        return []
    if since_seq > 0:
        return _bus.events_since(since_seq, n)
    return _bus.recent_events(n)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """事件驱动 WS：订阅 bus 事件流，实时推 delta。

    设计要点（前端「一刷新就断开、运行越久越久」的根因修复）：
    ① **并发检测断开**：旧实现从不读 socket，浏览器刷新丢弃旧连接后服务端协程不自知，会一直空转，
       每次刷新泄漏一个僵尸协程；积累后每 0.2s 各自拉一遍事件 → 拖垮事件循环、新连接被饿死。
       现起一个 receive 任务，客户端一断开立即结束本协程，杜绝僵尸累积。
    ② **阻塞读移出事件循环**：`recent_events`/`events_since` 在 RedisBus 下是阻塞调用（LRANGE+json），
       直接在协程里跑会卡住整个 loop；用 run_in_threadpool 丢到线程池。
    ③ **连接时回放最近 worker_log/worker_error**：刷新后日志面板不再空白（修「日志刷新即丢」）。
       仍只回放日志类事件、且有上限，不会把全部历史一次性灌给前端。
    游标用 bus 的单调 seq（InMemory 计数器 / Redis INCR），与墙钟无关，避免同 tick 同 ts 漏推/乱序。
    """
    from fastapi.concurrency import run_in_threadpool
    await ws.accept()
    if not _bus:
        await ws.close()
        return

    # 连接时回放最近日志事件 + 记录最新 seq。
    # 先取最新 seq（只拉最后 1 条），再用 Lua 过滤只取日志类事件（不拉全量 400 条到 Python）。
    try:
        tail = await run_in_threadpool(_bus.recent_events, 1)
        last_seq = tail[-1].get("seq", 0) if tail else 0
    except Exception:
        last_seq = 0
    log.info("WS 客户端已连接 (last_seq=%d)", last_seq)
    # 回放：拉最近 400 条但只发日志类（Lua 在 Redis 内过滤，只返回匹配项，不传全量）
    try:
        if hasattr(_bus, '_LUA_EVENTS_SINCE'):
            # Lua 过滤：只返回 worker_log / worker_error 类型的事件
            backlog = await run_in_threadpool(
                _bus._recent_events_filtered, 400,
                ("worker_log", "worker_error"))
        else:
            backlog = await run_in_threadpool(_bus.recent_events, 400)
            backlog = [ev for ev in backlog
                       if ev.get("type") in ("worker_log", "worker_error")]
        for ev in backlog:
            await ws.send_json(ev)
    except WebSocketDisconnect:
        return
    except Exception:
        return

    # 并发任务：持续读 socket，仅用于侦测断开（收到任何帧/断开都触发 stop）
    disconnected = asyncio.Event()

    async def _watch():
        try:
            while True:
                await ws.receive()
        except Exception:
            disconnected.set()

    watcher = asyncio.create_task(_watch())
    try:
        while not disconnected.is_set():
            # Lua 脚本服务端过滤：无新事件时只传空列表（不传 1000 条 JSON），带宽近零
            new_events = await run_in_threadpool(_bus.events_since, last_seq, 1000)
            for ev in new_events:
                await ws.send_json(ev)
                last_seq = ev.get("seq", last_seq)
            # 无新事件时 0.5s 一轮（减少 Redis 调用频率），有新事件时立即下一轮（已由 for 跳过 sleep）
            if not new_events:
                await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        log.info("WS 客户端断开")
    except Exception as e:
        log.error("WS 异常: %s", e, exc_info=True)
    finally:
        watcher.cancel()


# ── 启动函数 ──

def start_api(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
