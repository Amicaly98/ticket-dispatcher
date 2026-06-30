"""Driver 抽象：把「黑盒进程」的生命周期收敛成一个契约。

调度系统与具体执行脚本解耦的边界就在这里。
Driver 只负责：把 Job 翻译成目标程序原生配置、拉起进程、轮询状态、必要时取消。
精确时序（倒计时 + 突发下单）、token 签名、风控全在被拉起的进程里，不在这里。

每个 Driver 实例无状态地服务多个 Job；每次 start 返回一个不透明 handle，
后续 poll/cancel/cleanup 都针对该 handle。executor 负责并发管理多个 handle。

实现指南见 docs/driver_development.md。
"""
from __future__ import annotations
import abc
import os
from enum import Enum
from typing import Optional

from ..models import AttemptResult, Job

# 配置工作根目录：config/runtime/<worker_id>/<job_id>/
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME_ROOT = os.path.join(ROOT, "config", "runtime")


class RunState(str, Enum):
    RUNNING = "running"
    DONE = "done"


class Driver(abc.ABC):
    """Driver 抽象基类。子类必须设置 class 属性 `program` 并实现所有抽象方法。"""
    program: str = ""

    def workdir(self, job: Job) -> str:
        d = os.path.join(RUNTIME_ROOT, job.worker_id, job.job_id)
        os.makedirs(d, exist_ok=True)
        return d

    @abc.abstractmethod
    def prepare(self, job: Job) -> str:
        """把 Job 翻译成目标程序原生配置，写入 per-job 工作目录，返回该目录。"""

    @abc.abstractmethod
    def start(self, job: Job, slot_idx: int = 0) -> object:
        """拉起黑盒进程（或其模拟），返回不透明 handle。

        slot_idx：该 job 在 executor 中的 slot 序号（0-based），用于端口分配等。"""

    @abc.abstractmethod
    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        """轮询 handle：RUNNING 时 result=None；DONE 时返回结构化 AttemptResult。"""

    @abc.abstractmethod
    def cancel(self, handle: object, graceful: bool = True) -> None:
        """取消：graceful=True 时不打断在飞请求、尽快收尾；False 时强杀。"""

    def cleanup(self, handle: object) -> None:
        """回收 handle 资源（可选）。"""

    def new_output(self, handle: object) -> list:
        """返回自上次调用以来该 handle 新产生的 stdout 行（增量，供实时 log 推送）。

        默认返回空列表——不支持实时输出的 Driver 无需实现。实现方应维护一个游标，
        只返回新行，避免重复推送。"""
        return []
