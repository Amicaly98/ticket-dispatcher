"""进程监督器（Executor）：按 max_slots 并发管理 Driver 子进程。

Worker agent 把一个 Job 交给 Executor：
  handle = executor.start(job, driver)
  然后在 Worker 的主循环里不断调 executor.tick()：
    - 已完成的 handle 被取出，返回 (job, AttemptResult)
    - 正在运行的 handle 被 poll，超时的被 cancel
  executor.cancel(job_id) 主动取消一个在飞任务（前端"停止"时用）。

Executor 内部维护一个字典 slot_map: slot_idx -> handle，用于监控并发。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

from .clock import Clock, DEFAULT_CLOCK
from .driver.base import Driver, RunState
from .models import AttemptResult, Job

log = logging.getLogger("executor")


@dataclass
class _Slot:
    job: Job
    driver: Driver
    handle: object
    started_at: float
    timeout: float = 0.0  # 0 = 不限时
    slot_idx: int = 0     # 该 slot 在 executor 中的序号（用于端口分配等）


class Executor:
    def __init__(self, max_slots: int = 1, clock: Optional[Clock] = None):
        self.max_slots = max_slots
        self.clock = clock or DEFAULT_CLOCK
        self._slots: dict[str, _Slot] = {}   # job_id -> Slot
        self._done: list = []                 # [(job, AttemptResult)] 等待被 worker 取走

    @property
    def busy_slots(self) -> int:
        return len(self._slots)

    @property
    def free_slots(self) -> int:
        return max(0, self.max_slots - self.busy_slots)

    def start(self, job: Job, driver: Driver, timeout: float = 0.0) -> bool:
        """拉起一个 Job。返回 True 表示成功启动，False 表示已满。"""
        if job.job_id in self._slots:
            log.warning("job %s 已在执行，忽略重复 start", job.job_id)
            return True
        if self.free_slots <= 0:
            log.warning("所有 slot 已满(%d/%d)，无法启动 %s",
                        self.busy_slots, self.max_slots, job.job_id)
            return False
        slot_idx = len(self._slots)   # 下一个空闲序号（用于端口分配等）
        try:
            handle = driver.start(job, slot_idx=slot_idx)
        except Exception as e:  # noqa: BLE001
            import traceback
            log.error("启动 job %s 失败: %s", job.job_id, e)
            self._done.append((job, AttemptResult(
                job_id=job.job_id, task_id=job.task_id, worker_id=job.worker_id,
                success=False, reason=f"start_error:{e}",
                detail=traceback.format_exc()[-4096:])))
            return False
        self._slots[job.job_id] = _Slot(
            job=job, driver=driver, handle=handle, started_at=self.clock.now(),
            timeout=timeout, slot_idx=slot_idx)
        log.info("slot 启动 %s (%d/%d)", job.job_id, self.busy_slots, self.max_slots)
        return True

    def tick(self) -> list:
        """轮询所有在飞 slot，返回已完成的 [(job, AttemptResult)]。"""
        done = list(self._done)
        self._done.clear()
        to_remove = []
        for job_id, slot in self._slots.items():
            now = self.clock.now()
            # 超时检查
            if slot.timeout and (now - slot.started_at) > slot.timeout:
                log.warning("job %s 超时(%.0fs)，取消", job_id, slot.timeout)
                slot.driver.cancel(slot.handle, graceful=False)
                done.append((slot.job, AttemptResult(
                    job_id=job_id, task_id=slot.job.task_id, worker_id=slot.job.worker_id,
                    success=False, reason="timeout",
                    detail=f"job 运行超过 {slot.timeout:.0f}s 被强制取消")))
                to_remove.append(job_id)
                continue
            try:
                state, result = slot.driver.poll(slot.handle)
            except Exception as e:  # noqa: BLE001
                import traceback
                log.error("poll job %s 异常: %s", job_id, e)
                done.append((slot.job, AttemptResult(
                    job_id=job_id, task_id=slot.job.task_id, worker_id=slot.job.worker_id,
                    success=False, reason=f"poll_error:{e}",
                    detail=traceback.format_exc()[-4096:])))
                to_remove.append(job_id)
                continue
            if state == RunState.DONE and result is not None:
                done.append((slot.job, result))
                to_remove.append(job_id)
        # 移除已完成 slot 前，最后一次 drain 剩余日志（成功后的尾行不会被下次 drain_output 收到）
        final_lines = []
        for jid in to_remove:
            slot = self._slots.get(jid)
            if slot:
                try:
                    lines = slot.driver.new_output(slot.handle)
                    if lines:
                        final_lines.append((slot.job, lines))
                except Exception:
                    pass
        for jid in to_remove:
            slot = self._slots.pop(jid, None)
            if slot:
                slot.driver.cleanup(slot.handle)
        if done:
            log.info("tick 完成 %d 个 job，剩余 %d/%d", len(done), self.busy_slots, self.max_slots)
        return done, final_lines

    def drain_output(self) -> list:
        """收集所有在飞 slot 的增量 stdout，返回 [(job, [新行...])]（无新行的跳过）。

        供 worker 把黑盒实时输出推成 worker_log 事件。委托各 Driver.new_output（默认空）。"""
        out = []
        for slot in list(self._slots.values()):
            try:
                lines = slot.driver.new_output(slot.handle)
            except Exception as e:  # noqa: BLE001 —— 实时 log 不应影响主流程
                log.debug("new_output 异常 job=%s: %s", slot.job.job_id, e)
                continue
            if lines:
                out.append((slot.job, lines))
        return out

    def cancel(self, job_id: str, graceful: bool = True) -> bool:
        """主动取消一个在飞任务。返回 True 表示找到并取消。

        取消会和「超时」路径一样产出一条 reason="cancelled" 的 AttemptResult 入队 _done，
        让 worker 的 _poll_results 走正常完成流程（push result + 清当前 + 立即心跳），
        否则停止只是默默 pop 掉 slot、前端看不到任何反应。
        """
        slot = self._slots.pop(job_id, None)
        if slot is None:
            return False
        slot.driver.cancel(slot.handle, graceful=graceful)
        slot.driver.cleanup(slot.handle)
        self._done.append((slot.job, AttemptResult(
            job_id=job_id, task_id=slot.job.task_id, worker_id=slot.job.worker_id,
            success=False, reason="cancelled")))
        log.info("主动取消 %s (graceful=%s)", job_id, graceful)
        return True

    def cancel_all(self, graceful: bool = True) -> int:
        """取消所有在飞任务。"""
        count = 0
        for jid in list(self._slots):
            if self.cancel(jid, graceful=graceful):
                count += 1
        return count
