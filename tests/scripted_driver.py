"""ScriptedDriver：按 job 编排行为的测试替身（取代用全局环境变量配置的 DryRun）。

每个 program/job 的行为由一段「脚本」决定，支持：
  - outcome：success / sold_out / error
  - delay：在虚拟时间里跑多久才 DONE（配合 FakeClock）
  - fail_attempts：前 N 次 attempt 先售罄、之后成功（重试/故障注入演示）
  - 故障注入：start 抛异常 / poll 抛异常 / 永不返回（崩溃）

这是「环节1 + 故障注入」的可控替身，也用于 Driver 契约测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.clock import Clock, DEFAULT_CLOCK
from src.driver.base import Driver, RunState
from src.models import AttemptResult, Job


@dataclass
class Behavior:
    """单个 job 的预设行为。"""
    outcome: str = "success"          # success / sold_out / error
    delay: float = 1.0                # 虚拟秒：deadline 之后多久 DONE
    fail_attempts: int = 0            # 前 N 次 attempt 先售罄
    raise_on_start: bool = False      # start() 抛异常（测 executor 的 start_error 兜底）
    raise_on_poll: bool = False       # 第一次 poll 抛异常（测 poll_error 兜底）
    never_finish: bool = False        # 永远 RUNNING（测超时 / inflight 回收）


@dataclass
class _Handle:
    job: Job
    ready_at: float
    success: bool
    reason: str
    behavior: Behavior
    cancelled: bool = False
    polls: int = field(default=0)


class ScriptedDriver(Driver):
    """按 (program 或 job_id) 查表决定行为；查不到用 default。"""

    program = "scripted"

    def __init__(self, behaviors: Optional[dict] = None,
                 default: Optional[Behavior] = None, clock: Optional[Clock] = None):
        # behaviors: {job_id 或 program: Behavior}
        self.behaviors: dict = behaviors or {}
        self.default = default or Behavior()
        self.clock = clock or DEFAULT_CLOCK
        self.prepared: list = []          # 记录 prepare 过的 job_id（断言用）
        self.cleaned: list = []           # 记录 cleanup 过的 job_id

    def _behavior(self, job: Job) -> Behavior:
        return self.behaviors.get(job.job_id) or self.behaviors.get(job.program) or self.default

    def prepare(self, job: Job) -> str:
        wd = self.workdir(job)
        self.prepared.append(job.job_id)
        return wd

    def start(self, job: Job, slot_idx: int = 0) -> object:
        b = self._behavior(job)
        if b.raise_on_start:
            raise RuntimeError("scripted start failure")
        now = self.clock.now()
        ready_at = max(now, job.deadline) + b.delay
        success, reason = True, "ok"
        if b.outcome == "sold_out":
            success, reason = False, "sold_out"
        elif b.outcome == "error":
            success, reason = False, "error"
        if b.fail_attempts and job.attempt_no < b.fail_attempts:
            success, reason = False, "sold_out"
        return _Handle(job=job, ready_at=ready_at, success=success, reason=reason, behavior=b)

    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        h: _Handle = handle  # type: ignore[assignment]
        h.polls += 1
        if h.behavior.raise_on_poll and h.polls == 1:
            raise RuntimeError("scripted poll failure")
        if h.cancelled:
            return RunState.DONE, AttemptResult(
                job_id=h.job.job_id, task_id=h.job.task_id, worker_id=h.job.worker_id,
                success=False, reason="cancelled")
        if h.behavior.never_finish:
            return RunState.RUNNING, None
        if self.clock.now() < h.ready_at:
            return RunState.RUNNING, None
        secured = h.job.buyer_ids if h.success else []
        return RunState.DONE, AttemptResult(
            job_id=h.job.job_id, task_id=h.job.task_id, worker_id=h.job.worker_id,
            success=h.success, order_id=(f"SCRIPTED-{h.job.job_id}" if h.success else ""),
            reason=h.reason, buyers_secured=secured, buyers_attempted=h.job.buyer_ids)

    def cancel(self, handle: object, graceful: bool = True) -> None:
        handle.cancelled = True  # type: ignore[attr-defined]

    def cleanup(self, handle: object) -> None:
        h: _Handle = handle  # type: ignore[assignment]
        self.cleaned.append(h.job.job_id)
