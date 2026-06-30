"""DryRunDriver：不碰任何外部进程的默认 Driver，用于端到端跑通主干。

它做两件事：
1. prepare() 写出调试 JSON（Job 快照），验证配置链路。
2. start()/poll() 模拟一个「黑盒进程」：在 deadline（开售时刻）后经过一小段
   sim_delay 完成，返回可配置的结构化结果（成功/售罄/失败）。

输出策略（构造参数，或环境变量）：
  DRYRUN_OUTCOME = success | sold_out | error   （默认 success）
  DRYRUN_DELAY   = 模拟下单耗时秒                 （默认 1.0）
  DRYRUN_FAIL_ATTEMPTS = 前 N 次尝试先失败(售罄)再成功，用于故障注入/重试演示（默认 0）
"""
from __future__ import annotations
import json
import logging
import os
from typing import Optional

from ..clock import Clock, DEFAULT_CLOCK
from ..models import AttemptResult, Job
from .base import Driver, RunState

log = logging.getLogger("driver.dryrun")


class _Handle:
    def __init__(self, job: Job, ready_at: float, success: bool, reason: str):
        self.job = job
        self.ready_at = ready_at
        self.success = success
        self.reason = reason
        self.cancelled = False
        # 合成的「黑盒输出」，供实时 log / 失败 detail 在 DRYRUN 下也能跑通
        self.stdout_lines: list = [
            f"[dryrun] 启动模拟黑盒 job={job.job_id} project={job.project_id}",
            f"[dryrun] 倒计时至 deadline={job.deadline:.0f} 后开火",
        ]
        self.out_cursor = 0


class DryRunDriver(Driver):
    program = "dryrun"

    def __init__(self, outcome: Optional[str] = None, delay: Optional[float] = None,
                 fail_attempts: Optional[int] = None, clock: Optional[Clock] = None):
        self.outcome = outcome or os.environ.get("DRYRUN_OUTCOME", "success")
        self.delay = delay if delay is not None else float(os.environ.get("DRYRUN_DELAY", "1.0"))
        self.fail_attempts = (fail_attempts if fail_attempts is not None
                              else int(os.environ.get("DRYRUN_FAIL_ATTEMPTS", "0")))
        self.clock = clock or DEFAULT_CLOCK

    # ── prepare：写出调试 JSON ──
    def prepare(self, job: Job) -> str:
        wd = self.workdir(job)
        try:
            # 尝试调用已注册的 Converter
            from ..converters import get_converter
            converter = get_converter(job.program)
            if converter:
                converter.convert(job, wd)
                log.info("[%s] 已通过 Converter(%s) 写出配置 -> %s", job.job_id, job.program, wd)
                return wd
        except Exception:
            pass
        # 兜底：写出 Job 快照 JSON
        debug_path = os.path.join(wd, "job.debug.json")
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("[%s] 写出调试 JSON -> %s", job.job_id, debug_path)
        return wd

    # ── start：模拟拉起进程 ──
    def start(self, job: Job, slot_idx: int = 0) -> object:
        now = self.clock.now()
        # 模拟目标程序自己的倒计时：deadline 之后才「开火」
        ready_at = max(now, job.deadline) + self.delay
        success = True
        reason = "ok"
        if self.outcome == "sold_out":
            success, reason = False, "sold_out"
        elif self.outcome == "error":
            success, reason = False, "error"
        # 故障注入：前 N 次尝试先售罄，之后成功
        if self.fail_attempts and job.attempt_no < self.fail_attempts:
            success, reason = False, "sold_out"
        return _Handle(job, ready_at, success, reason)

    # ── poll ──
    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        h: _Handle = handle  # type: ignore[assignment]
        if h.cancelled:
            return RunState.DONE, AttemptResult(
                job_id=h.job.job_id, task_id=h.job.task_id, worker_id=h.job.worker_id,
                success=False, reason="cancelled")
        if self.clock.now() < h.ready_at:
            return RunState.RUNNING, None
        secured = h.job.buyer_ids if h.success else []
        order_id = f"DRYRUN-{h.job.job_id}" if h.success else ""
        if h.success:
            h.stdout_lines.append(f"[dryrun] 下单成功 orderId={order_id}")
        else:
            h.stdout_lines.append(f"[dryrun] 失败：{h.reason}")
        detail = "" if h.success else "\n".join(h.stdout_lines[-30:]) + "\n(exit=1, dryrun)"
        return RunState.DONE, AttemptResult(
            job_id=h.job.job_id, task_id=h.job.task_id, worker_id=h.job.worker_id,
            success=h.success, order_id=order_id, reason=h.reason,
            buyers_secured=secured, detail=detail)

    def cancel(self, handle: object, graceful: bool = True) -> None:
        handle.cancelled = True  # type: ignore[attr-defined]

    def new_output(self, handle: object) -> list:
        h: _Handle = handle  # type: ignore[assignment]
        new = h.stdout_lines[h.out_cursor:]
        h.out_cursor = len(h.stdout_lines)
        return new
