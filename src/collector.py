"""结果收集器（替代 engine）。

降级后没有调度循环——这只是一个被动后台线程：
  1. drain bus 的 AttemptResult → 落 SQLite 历史 + 广播 job_result 事件
  2. drain worker 生命周期事件 → 落 SQLite

无 tick、无模式切换、无 worker 分配、无防重。worker 的实时状态由 bus 心跳
（list_workers）直接表达，API 直接读，不经这里。
"""
from __future__ import annotations
import logging
import time

from .bus import Bus, get_bus
from .notify import Notifier
from .store import Store

log = logging.getLogger("collector")


class Collector:
    def __init__(self, bus: Bus = None, store: Store = None, notifier: Notifier = None):
        self.bus = bus or get_bus()
        self.store = store or Store()
        # 购票后统一推送：成败结果都经这里，一处发推送 = 全 worker 生效（见 src/notify.py）
        self.notifier = notifier if notifier is not None else Notifier()
        self._running = False
        self._last_event_idx = 0
        self._last_event_seq = 0   # Lua 增量过滤用的 seq 游标

    def start(self):
        self._running = True
        log.info("Collector 启动：drain results + worker events → store")
        while self._running:
            try:
                self._drain_results()
                self._drain_worker_events()
                time.sleep(0.2)
            except KeyboardInterrupt:
                break
            except Exception as e:  # noqa: BLE001
                log.error("Collector 异常: %s", e, exc_info=True)
                time.sleep(1)
        self._shutdown()

    def stop(self):
        self._running = False

    def _drain_results(self):
        for _ in range(50):
            result = self.bus.pop_result(timeout=0)
            if result is None:
                break
            log.info("收到结果: job=%s success=%s order=%s reason=%s",
                     result.job_id, result.success, result.order_id, result.reason)
            self.store.record_result(result)
            self.bus.publish({
                "type": "job_result",
                "job_id": result.job_id, "task_id": result.task_id,
                "worker_id": result.worker_id, "success": result.success,
                "order_id": result.order_id, "reason": result.reason,
                "detail": result.detail,
            })
            # 购票后推送（成功必推 / 失败可选）：先 wants() 过滤，避免无谓 DB 查询；
            # 富化消息要的账号·票档·购票人从 attempts 表按 job_id 取（result 本身不带）。
            # attempts 无记录时（job 由另一台 API 指派），从 Redis job stream 取上下文兜底。
            if self.notifier.wants(result.success):
                ctx = self.store.get_attempt(result.job_id) or {}
                if not ctx:
                    ctx = self.bus.get_job_context(result.job_id, result.worker_id) or {}
                    if ctx:
                        log.info("推送上下文从 Redis job stream 补全: job=%s screen=%s sku=%s",
                                 result.job_id, ctx.get("screen_name"), ctx.get("sku_desc"))
                    else:
                        log.warning("推送上下文为空（attempts + job stream 均无 job_id=%s），消息将缺活动/票档/账号信息",
                                    result.job_id)
                self.notifier.notify_result(result, ctx)
            else:
                log.debug("跳过推送: success=%s (enabled=%s on_success=%s on_failure=%s)",
                          result.success, self.notifier.enabled,
                          self.notifier.on_success, self.notifier.on_failure)

    def _drain_worker_events(self):
        # 用 events_since（Lua 服务端过滤）替代 recent_events(200)（全量 LRANGE）。
        # 旧方式每 0.2s 拉 200 条全量 JSON（133B×200=27KB×5/s=133KB/s≈1Mbps 网络流量）。
        # 新方式只拉 seq > last 的新事件，无新事件时传输量接近 0。
        worker_types = {"worker_registered", "worker_offline", "worker_error"}
        events = self.bus.events_since(self._last_event_seq, n=200)
        for ev in events:
            seq = ev.get("seq", 0)
            if seq > self._last_event_seq:
                self._last_event_seq = seq
            etype = ev.get("type", "")
            if etype == "worker_error":
                # 失败诊断单独落库：detail 直接进 detail 列，便于审计翻历史失败原文
                self.store.record_worker_event(
                    ev.get("worker_id", ""), f"error:{ev.get('reason', '')}",
                    ev.get("detail", "") or str(ev.get("task_id", "")))
            elif etype in worker_types:
                self.store.record_worker_event(
                    ev.get("worker_id", ""), etype,
                    str({k: v for k, v in ev.items() if k not in ("type", "ts")}))

    def _shutdown(self):
        log.info("Collector 关闭")
        self.store.close()
