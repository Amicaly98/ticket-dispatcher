"""传输层抽象：API 节点 ↔ Worker 节点之间的所有通信都走 Bus。

提供两套实现：
- InMemoryBus：线程安全，单进程内把 API/Collector + Worker 循环跑通（无需 Redis，开发/测试用）。
- RedisBus：见 queue.py，跨进程/跨机器的真分布式实现。

降级为手动指派台后 Bus 不再有任何共享可变状态（防重 claim 集合随调度器一起删了）。
只传四类跨机器的东西：
1. Job 队列   （API → Worker，定向到 worker_id）
2. Result 队列（Worker → API/Collector）
3. Control    （API → Worker，停止/取消信号）
4. 状态快照 + 事件发布（Worker 心跳、监控/审计）
"""
from __future__ import annotations
import abc
import os
import queue
import threading
import time
from collections import deque
from typing import Optional

from .models import AttemptResult, ControlSignal, Job


class Bus(abc.ABC):
    # ── Job 队列（定向 worker_id）──
    @abc.abstractmethod
    def push_job(self, job: Job) -> None: ...

    @abc.abstractmethod
    def pop_job(self, worker_id: str, timeout: float = 1.0) -> Optional[Job]: ...

    def ack_job(self) -> None:
        """ack 最近一次 pop_job 拿到的消息（executor.start 成功后调用）。默认 no-op。"""
        pass

    def get_job_context(self, job_id: str, worker_id: str) -> Optional[dict]:
        """从 job stream 中查找已派发 job 的上下文（活动/场次/票档/账号/购票人）。默认 None。"""
        return None

    # ── Result 队列 ──
    @abc.abstractmethod
    def push_result(self, result: AttemptResult) -> None: ...

    @abc.abstractmethod
    def pop_result(self, timeout: float = 1.0) -> Optional[AttemptResult]: ...

    # ── 控制信号（定向 worker_id，非阻塞排空）──
    @abc.abstractmethod
    def push_signal(self, sig: ControlSignal) -> None: ...

    @abc.abstractmethod
    def drain_signals(self, worker_id: str) -> list: ...

    # ── Worker 注册 / 心跳 ──
    @abc.abstractmethod
    def heartbeat(self, worker_id: str, status: dict) -> None: ...

    @abc.abstractmethod
    def list_workers(self) -> dict: ...

    def forget_worker(self, worker_id: str) -> None:
        """从心跳表删除一个 worker（前端删除 worker 时清掉离线僵尸）。默认 no-op。"""
        pass

    # ── 事件（监控/审计）──
    @abc.abstractmethod
    def publish(self, event: dict) -> None: ...

    @abc.abstractmethod
    def recent_events(self, n: int = 100) -> list: ...

    def events_since(self, seq: int, n: int = 1000) -> list:
        """返回 seq 严格大于给定值的事件（WS 增量推送用）。

        默认基于 recent_events 过滤；窗口 n 需覆盖两次拉取间的事件量（worker_log 每 tick 1 条/worker，
        足够）。子类可覆盖优化。"""
        return [ev for ev in self.recent_events(n) if ev.get("seq", 0) > seq]

    # ── 日志按需订阅（控制台打开某 worker 日志面板时才推流，避免 N 个 worker 的 stdout 洪流）──
    def mark_log_interest(self, worker_id: str, ttl: float = 30.0) -> None:
        """登记「有人正在看 worker_id 的实时日志」，ttl 秒内有效（前端面板打开时周期续期）。
        默认 no-op（= 始终推流，向后兼容；真正的 gating 在 RedisBus 分布式场景实现）。"""
        pass

    def log_wanted(self, worker_id: str) -> bool:
        """worker 据此决定要不要 publish worker_log。默认 True（始终推；单机开发/InMemory 不限流）。"""
        return True

    def close(self) -> None:
        pass


class InMemoryBus(Bus):
    """单进程线程安全实现。Job/Result 用 queue.Queue，其余用 dict+锁。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, queue.Queue] = {}          # worker_id -> Queue[Job]
        self._results: queue.Queue = queue.Queue()       # Queue[AttemptResult]
        self._signals: dict[str, list] = {}              # worker_id -> [ControlSignal]
        self._workers: dict[str, dict] = {}              # worker_id -> status
        self._events: deque = deque(maxlen=1000)
        self._event_seq: int = 0                          # 单调自增事件序号（WS 游标用，不靠墙钟 ts）

    def _job_q(self, worker_id: str) -> queue.Queue:
        with self._lock:
            if worker_id not in self._jobs:
                self._jobs[worker_id] = queue.Queue()
            return self._jobs[worker_id]

    # ── Job ──
    def push_job(self, job: Job) -> None:
        self._job_q(job.worker_id).put(job)

    def pop_job(self, worker_id: str, timeout: float = 1.0) -> Optional[Job]:
        try:
            return self._job_q(worker_id).get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Result ──
    def push_result(self, result: AttemptResult) -> None:
        self._results.put(result)

    def pop_result(self, timeout: float = 1.0) -> Optional[AttemptResult]:
        try:
            return self._results.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Control ──
    def push_signal(self, sig: ControlSignal) -> None:
        with self._lock:
            self._signals.setdefault(sig.worker_id, []).append(sig)

    def drain_signals(self, worker_id: str) -> list:
        with self._lock:
            sigs = self._signals.get(worker_id, [])
            self._signals[worker_id] = []
            return sigs

    # ── Worker 注册 / 心跳 ──
    def heartbeat(self, worker_id: str, status: dict) -> None:
        with self._lock:
            self._workers[worker_id] = {**status, "ts": time.time()}

    def list_workers(self) -> dict:
        with self._lock:
            return dict(self._workers)

    def forget_worker(self, worker_id: str) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)

    # ── 事件 ──
    def publish(self, event: dict) -> None:
        with self._lock:
            self._event_seq += 1
            self._events.append({**event, "ts": event.get("ts", time.time()),
                                 "seq": self._event_seq})

    def recent_events(self, n: int = 100) -> list:
        with self._lock:
            return list(self._events)[-n:]


_singleton_bus: Bus = None


def get_bus() -> Bus:
    """工厂：设了 REDIS_HOST 就用 RedisBus（连不上即抛错），否则 InMemoryBus（单例）。

    InMemoryBus 单进程模式下必须是单例，否则 API/Collector 和 WorkerAgent 各持一个实例，
    Job 派发到一个 bus 的队列，Worker 从另一个 bus 的队列读——永远消费不到。

    显式设了 REDIS_HOST 却连不上时**直接抛异常**（不再静默回退 InMemoryBus）：
    回退会让 worker 收不到 job、assign 石沉大海，是最难排查的部署坑。要单进程内存模式就别设 REDIS_HOST。
    """
    global _singleton_bus
    if _singleton_bus is not None:
        return _singleton_bus
    host = os.environ.get("REDIS_HOST")
    if host:
        # 显式要了 Redis 却连不上 → 直接崩，绝不静默回退 InMemoryBus。
        # （旧行为回退后 worker 永远收不到 API 的 job、assign 石沉大海，是部署头号坑。）
        from .queue import RedisBus
        bus = RedisBus(
            host=host,
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
        )
        bus.ping()   # 连不上在此抛 redis.ConnectionError，让进程启动即失败而非假装正常
        _singleton_bus = bus
        return bus
    # 未设 REDIS_HOST → 单进程开发模式，用 InMemoryBus 单例
    _singleton_bus = InMemoryBus()
    return _singleton_bus
