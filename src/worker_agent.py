"""Worker agent：真正的 Worker 节点循环。

与 API 节点跑在不同进程/机器上，两者唯一的通信通道是 Bus。

生命周期：
  1. 向 bus 注册 + 周期心跳（状态变化时立即心跳，让控制台即时反映）
  2. 从 bus 消费定向到本 Worker 的 Job
  3. 交 Executor 拉起 Driver 子进程（黑盒 bot）
  4. tick() 轮询结果，上报 AttemptResult，完成后自动回 IDLE
  5. 处理 ControlSignal（cancel = 前端"停止"）：终止在飞 Job

用法：
  from worker_agent import WorkerAgent
  agent = WorkerAgent("worker_001", bus, max_slots=3)
  agent.run()  # 阻塞主循环
"""
from __future__ import annotations
import logging
from typing import Optional

from .bus import Bus
from .clock import Clock, DEFAULT_CLOCK
from .driver import get_driver
from .executor import Executor
from .models import AttemptResult, ControlSignal, Job

log = logging.getLogger("worker")


class WorkerAgent:
    def __init__(self, worker_id: str, bus: Bus, max_slots: int = 3,
                 poll_interval: float = 0.3, heartbeat_interval: float = 1.5,
                 clock: Optional[Clock] = None, driver_factory=None,
                 program: str = "", public_ip: str = ""):
        self.worker_id = worker_id
        self.bus = bus
        self.program = program   # 本节点跑什么 bot，随心跳上报，让控制台认得自注册的活 worker
        self.public_ip = public_ip  # 随心跳上报，前端「出口」列显示
        self.clock = clock or DEFAULT_CLOCK
        # driver_factory(program) -> Driver；默认走全局工厂，测试可注入 ScriptedDriver
        self._driver_factory = driver_factory or get_driver
        self.executor = Executor(max_slots=max_slots, clock=self.clock)
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._running = False
        self._paused = False                         # 前端「暂停」：停止消费新 Job（在飞的继续）
        self._current_task_id: Optional[str] = None
        self._current_job_id: Optional[str] = None
        self._current: Optional[dict] = None        # 当前在抢什么的富信息（控制台「当前」展示）
        self._last_result: Optional[dict] = None   # 供控制台展示上次抢票结果
        self._last_hb = 0.0

    # ── 主循环 ──

    def run(self):
        self._running = True
        self._register()
        log.info("Worker agent 启动: %s (max_slots=%d)", self.worker_id, self.executor.max_slots)
        while self._running:
            try:
                self.tick_once()
                self.clock.sleep(self.poll_interval)
            except KeyboardInterrupt:
                log.info("收到中断，关闭 Worker agent")
                break
            except Exception as e:  # noqa: BLE001
                log.error("主循环异常: %s", e, exc_info=True)
                self.clock.sleep(1)
        self._shutdown()

    def tick_once(self):
        """单轮：心跳 + 消费 Job + 轮询结果 + 处理信号。测试可直接调用（配合 FakeClock）。"""
        now = self.clock.now()
        # 心跳
        if now - self._last_hb > self.heartbeat_interval:
            self._heartbeat()
            self._last_hb = now
        # 处理控制信号（先于 poll：cancel 产出的 cancelled 结果同 tick 被 _poll_results 收走）
        self._process_signals()
        # 消费 Job
        self._try_consume()
        # 推送黑盒实时 log（增量）
        self._pump_logs()
        # 轮询结果（含 cancel/超时 产出的结果 → 清当前 + 立即心跳）
        self._poll_results()

    def stop(self):
        self._running = False

    # ── 注册 / 心跳 ──

    def _register(self):
        self.bus.heartbeat(self.worker_id, {
            "worker_id": self.worker_id,
            "status": "idle",
            "program": self.program,
            "max_slots": self.executor.max_slots,
            "public_ip": self.public_ip,
        })
        self.bus.publish({
            "type": "worker_registered",
            "worker_id": self.worker_id, "program": self.program,
            "max_slots": self.executor.max_slots,
        })

    def _beat_now(self):
        """状态变化时立即心跳（覆盖 heartbeat_interval 节流），让控制台即时反映。"""
        self._heartbeat()
        self._last_hb = self.clock.now()

    def _heartbeat(self):
        # busy 优先（在飞 Job 还在跑）；否则 paused（已摘下不接新单）；否则 idle。
        # 另带 paused 布尔，应对 busy+paused（跑着但已暂停接新单）让前端正确显示开关态。
        if self.executor.busy_slots > 0:
            status = "busy"
        elif self._paused:
            status = "paused"
        else:
            status = "idle"
        self.bus.heartbeat(self.worker_id, {
            "worker_id": self.worker_id,
            "status": status,
            "paused": self._paused,
            "program": self.program,
            "public_ip": self.public_ip,
            "busy_slots": self.executor.busy_slots,
            "max_slots": self.executor.max_slots,
            "current_task_id": self._current_task_id,
            "current_job_id": self._current_job_id,
            "current": self._current,
            "last_result": self._last_result,
        })

    # ── 当前任务富信息 ──

    @staticmethod
    def _summarize_job(job: Job) -> dict:
        """把 Job 摘成控制台「当前」要展示的富信息（在抢什么票/谁的号/哪些购票人）。"""
        acc = job.account or {}
        return {
            "label": job.task_id,
            "job_id": job.job_id,
            "program": job.program,
            "project_id": job.project_id,
            "screen_id": job.screen_id,
            "sku_id": job.sku_id,
            "screen_name": job.screen_name,
            "sku_desc": job.sku_desc,
            "account": acc.get("username") or acc.get("account_id", ""),
            "uid": acc.get("uid", 0),
            "buyers": [b.get("name", "") for b in job.buyers],
            "deadline": job.deadline,
            "assigned_at": job.created_at,   # 指派时刻（卡点单派下去挂着等开售，用于显示已挂多久）
        }

    # ── 消费 Job ──

    def _try_consume(self):
        if self._paused:
            return   # 已暂停：不消费新 Job（在飞 Job 由 _poll_results 照常收尾）
        if self.executor.free_slots <= 0:
            return
        job = self.bus.pop_job(self.worker_id, timeout=0)
        if job is None:
            return
        log.info("[%s] 收到 Job: task=%s buyers=%s attempt=%d",
                 self.worker_id, job.task_id, job.buyer_ids, job.attempt_no)
        driver = self._driver_factory(job.program)
        try:
            driver.prepare(job)
        except Exception as e:  # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            log.error("[%s] prepare 失败: %s，上报错误结果", self.worker_id, e)
            result = AttemptResult(
                job_id=job.job_id, task_id=job.task_id, worker_id=self.worker_id,
                success=False, reason=f"prepare_error:{e}", detail=tb[-4096:])
            self.bus.push_result(result)
            self._report_error(result)
            return
        started = self.executor.start(job, driver)
        if started:
            self._current_task_id = job.task_id
            self._current_job_id = job.job_id
            self._current = {**self._summarize_job(job), "started_at": self.clock.now()}
            # executor.start 成功，ack Job 消息（RedisBus 路径：从 PEL 移除）
            self.bus.ack_job()
            self._beat_now()   # 立即心跳：让控制台即时看到 RUNNING

    # ── 轮询结果 ──

    def _poll_results(self):
        done, final_lines = self.executor.tick()
        # 完成 slot 的最后一波日志（在 slot 移除前 drain 的），推成 worker_log
        for job, lines in final_lines:
            if len(lines) > 40:
                lines = lines[-40:] + [f"…(省略 {len(lines) - 40} 行)"]
            self.bus.publish({
                "type": "worker_log",
                "worker_id": self.worker_id,
                "job_id": job.job_id,
                "task_id": job.task_id,
                "lines": lines,
            })
        for job, result in done:
            log.info("[%s] Job %s 完成: success=%s order=%s reason=%s",
                     self.worker_id, job.job_id, result.success,
                     result.order_id, result.reason)
            self._last_result = {
                "job_id": result.job_id, "task_id": result.task_id,
                "success": result.success, "order_id": result.order_id,
                "reason": result.reason, "detail": result.detail, "ts": result.ts,
            }
            self.bus.push_result(result)
            if not result.success:
                self._report_error(result)
            # 该 Job 完成 → 抢到/失败/被停都回到空闲
            if job.job_id == self._current_job_id:
                self._current_task_id = None
                self._current_job_id = None
                self._current = None
            self._beat_now()   # 立即心跳：让控制台即时看到 IDLE + last_result

    def _report_error(self, result: AttemptResult):
        """失败（非用户主动停止）→ 发 worker_error 事件，让前端实时高亮 + 落审计。"""
        if result.reason == "cancelled":
            return
        self.bus.publish({
            "type": "worker_error",
            "worker_id": self.worker_id,
            "job_id": result.job_id,
            "task_id": result.task_id,
            "reason": result.reason,
            "detail": result.detail,
        })

    # ── 实时 log 推送 ──

    def _pump_logs(self):
        """把各在飞 slot 的黑盒增量 stdout 推成 worker_log 事件（节流：每行级别，限量）。

        始终 publish（写入 ts:events 列表），让 WS 连接时能回放历史日志——之前只在
        log_wanted() 时才推，导致没人看时日志不进列表、打开面板后只能看到后续新推的。
        带宽已由 Lua 增量过滤保障（WS 只拉新增事件，不全量 LRANGE），始终 publish
        不会产生显著额外流量。失败诊断走 worker_error（_report_error），同理不受限。"""
        pending = list(self.executor.drain_output())
        if not pending:
            return
        for job, lines in pending:
            # 限量：单次最多推 40 行，避免刷爆事件总线（deque maxlen=1000）
            if len(lines) > 40:
                lines = lines[-40:] + [f"…(省略 {len(lines) - 40} 行)"]
            self.bus.publish({
                "type": "worker_log",
                "worker_id": self.worker_id,
                "job_id": job.job_id,
                "task_id": job.task_id,
                "lines": lines,
            })

    # ── 处理控制信号 ──

    def _process_signals(self):
        for sig in self.bus.drain_signals(self.worker_id):
            sig = ControlSignal.from_dict(sig) if isinstance(sig, dict) else sig
            if sig.signal == "cancel":
                # 前端的"停止"：立即终止指定/全部在飞 Job，worker 随即回 IDLE
                if sig.job_id:
                    self.executor.cancel(sig.job_id)
                else:
                    self.executor.cancel_all()
            elif sig.signal == "pause":
                # 前端「暂停」：停止消费新 Job（在飞 Job 继续），立即心跳反映 paused
                self._paused = True
                self._beat_now()
            elif sig.signal == "resume":
                self._paused = False
                self._beat_now()

    # ── 关闭 ──

    def _shutdown(self):
        log.info("[%s] 关闭中...", self.worker_id)
        self.executor.cancel_all(graceful=False)
        self.bus.heartbeat(self.worker_id, {"worker_id": self.worker_id, "status": "offline"})
        self.bus.publish({"type": "worker_offline", "worker_id": self.worker_id})
        log.info("[%s] 已关闭", self.worker_id)
