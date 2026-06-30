"""RedisBus 契约测试（用 fakeredis，纯内存模拟 redis streams + 消费者组，无需真 redis）。

降级前 RedisBus 是多机部署的传输层，但「从未在真实 redis 上跑过」（见 CLAUDE.md 瓶颈#1），
导致 `pop_job` 的 `xreadgroup` 用错了关键字参数（`group=`/`consumer=`）——redis-py 的签名是
`xreadgroup(groupname, consumername, streams, ...)`，只能用位置参数。错误调用抛 TypeError，
又被 `except Exception: return None` 吞掉，于是 **Redis 模式下 worker 永远拿不到任何 Job 且毫无报错**。

这一套契约把 RedisBus 焊死，覆盖：
  - push_job → pop_job → ack → push_result → pop_result 全链路（修复点的回归守卫）
  - 定向投递（job 只到目标 worker）
  - PEL 崩溃恢复（pop 不 ack，重启后重新拿到同一条）
  - ack 后从 PEL 移除（不再重复消费）
  - 控制信号 FIFO + 排空
  - 心跳 / list_workers / 事件
fakeredis 缺失时整文件跳过（生产部署不装它）。
"""
from __future__ import annotations
import pytest

fakeredis = pytest.importorskip("fakeredis")

from src.models import AttemptResult, Buyer, ControlSignal, Job
from src.queue import RedisBus


@pytest.fixture
def make_bus(monkeypatch):
    """返回一个工厂：每次调用都新建一个 RedisBus，但全部共享同一个 FakeServer，
    以模拟「多个进程/多次重启连同一个 redis 服务端」。"""
    server = fakeredis.FakeServer()

    def _factory(*_args, **kwargs):
        # 忽略 host/port/db，统一连到共享的内存 server；保留 decode_responses 等其它 kwargs
        for k in ("host", "port", "db"):
            kwargs.pop(k, None)
        return fakeredis.FakeStrictRedis(server=server, **kwargs)

    monkeypatch.setattr("redis.Redis", _factory)

    def _make() -> RedisBus:
        return RedisBus(host="ignored")

    return _make


def _job(job_id="job-1", worker_id="w1", deadline=1000.0) -> Job:
    return Job.build(
        job_id=job_id, task_id="ComicQuest VIP", worker_id=worker_id,
        program="scripted", project_id=42, screen_id=1, sku_id=1, count=1,
        pay_money=100, deadline=deadline,
        account={"uid": 1, "username": "u", "cookie": {}},
        buyers=[Buyer(id=501, name="甲")])


# ── 回归守卫：就是被修掉的那个 bug ──

def test_pop_job_returns_job_not_none(make_bus):
    """push 一个 job 后 pop 必须拿到 Job 本身。

    修复前 pop_job 的 xreadgroup 用了 group=/consumer= 关键字 → TypeError → 被吞 → 永远 None。
    """
    bus = make_bus()
    bus.push_job(_job("job-1", "w1"))
    got = bus.pop_job("w1", timeout=0.1)
    assert got is not None, "pop_job 必须拿到 Job（修复前永远返回 None）"
    assert got.job_id == "job-1"
    assert got.buyer_ids == [501]


# ── 全链路：指派 → 消费 → ack → 结果回流 ──

def test_job_result_roundtrip(make_bus):
    api_bus = make_bus()      # API 节点侧
    worker_bus = make_bus()   # Worker 节点侧（不同实例，同一 server）

    api_bus.push_job(_job("job-1", "w1"))

    job = worker_bus.pop_job("w1", timeout=0.1)
    assert job is not None and job.job_id == "job-1"
    worker_bus.ack_job()      # executor.start 成功后 ack

    worker_bus.push_result(AttemptResult(
        job_id="job-1", task_id=job.task_id, worker_id="w1",
        success=True, order_id="ORDER-9", buyers_secured=[501]))

    result = api_bus.pop_result(timeout=0.1)
    assert result is not None
    assert result.job_id == "job-1" and result.success is True
    assert result.order_id == "ORDER-9" and result.buyers_secured == [501]


# ── 定向投递：job 只到目标 worker ──

def test_job_targeted_to_worker(make_bus):
    bus = make_bus()
    bus.push_job(_job("job-w1", "w1"))

    assert bus.pop_job("w2", timeout=0.1) is None, "w2 不该看到投给 w1 的 job"
    got = bus.pop_job("w1", timeout=0.1)
    assert got is not None and got.job_id == "job-w1"


# ── PEL 崩溃恢复：pop 不 ack，重启后重新拿到同一条 ──

def test_pel_crash_recovery(make_bus):
    worker_bus = make_bus()
    worker_bus.push_job(_job("job-crash", "w1"))

    # 第一次消费但「崩溃」——没来得及 ack
    first = worker_bus.pop_job("w1", timeout=0.1)
    assert first is not None and first.job_id == "job-crash"
    # 不调用 ack_job()，模拟进程崩溃

    # worker 重启（新 RedisBus 实例，同一 server）→ 应从 PEL 恢复同一条 job
    restarted = make_bus()
    recovered = restarted.pop_job("w1", timeout=0.1)
    assert recovered is not None, "未 ack 的 job 必须能从 PEL 恢复"
    assert recovered.job_id == "job-crash"


# ── ack 后从 PEL 移除：不再重复消费 ──

def test_ack_removes_from_pel(make_bus):
    bus = make_bus()
    bus.push_job(_job("job-1", "w1"))

    got = bus.pop_job("w1", timeout=0.1)
    assert got is not None
    bus.ack_job()

    # ack 之后再 pop（含 PEL 恢复路径）应为空——这一条已确认消费完成
    assert bus.pop_job("w1", timeout=0.1) is None, "ack 后不该再被消费"


# ── 控制信号：FIFO + 一次性排空 ──

def test_signals_fifo_and_clear(make_bus):
    api_bus = make_bus()
    worker_bus = make_bus()

    api_bus.push_signal(ControlSignal(signal="cancel", worker_id="w1", job_id="a"))
    api_bus.push_signal(ControlSignal(signal="cancel", worker_id="w1", job_id="b"))

    sigs = worker_bus.drain_signals("w1")
    assert [s.job_id for s in sigs] == ["a", "b"], "信号应按 FIFO 排空"
    assert worker_bus.drain_signals("w1") == [], "排空后应为空"


# ── 心跳 / list_workers ──

def test_heartbeat_and_list_workers(make_bus):
    worker_bus = make_bus()
    api_bus = make_bus()

    worker_bus.heartbeat("w1", {"worker_id": "w1", "status": "busy", "busy_slots": 1})
    live = api_bus.list_workers()
    assert "w1" in live
    assert live["w1"]["status"] == "busy" and live["w1"]["busy_slots"] == 1
    assert "ts" in live["w1"], "心跳应带时间戳"


# ── 事件：publish + recent_events ──

def test_publish_recent_events(make_bus):
    bus = make_bus()
    bus.publish({"type": "job_dispatched", "job_id": "job-1", "worker_id": "w1"})
    bus.publish({"type": "job_result", "job_id": "job-1", "success": True})

    events = bus.recent_events(n=10)
    assert len(events) >= 2
    assert events[-1]["type"] == "job_result"
    assert events[-2]["type"] == "job_dispatched"


def test_event_seq_monotonic_for_ws_cursor(make_bus):
    """每条事件带严格单调自增 seq——WS 游标靠它而非墙钟 ts。

    回归守卫（实时日志推不出去的根因）：WS 用 `ts > last_ts` 过滤增量时，
    同一墙钟 tick 内成批 publish 的事件 ts 相同 → 推完一批后剩余同 ts 事件被永久滤掉。
    改用 seq 后，即使所有事件 ts 完全相同，seq 仍严格递增、不丢任何一条。
    """
    bus = make_bus()
    for i in range(50):
        bus.publish({"type": "worker_log", "worker_id": "w1", "lines": [f"line {i}"]})

    events = bus.recent_events(n=100)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), "seq 必须单调"
    assert len(set(seqs)) == len(seqs), "seq 必须唯一（不重复）"

    # 模拟 WS 游标：从中途某条之后增量取，必须恰好取到剩余全部，一条不漏
    last_seq = seqs[len(seqs) // 2]
    delta = [e for e in events if e["seq"] > last_seq]
    assert len(delta) == len(events) - (len(seqs) // 2 + 1)


def test_events_since_returns_only_newer(make_bus):
    """events_since(seq) 只返回 seq 严格大于给定值的事件（WS 增量推送 API）。"""
    bus = make_bus()
    bus.publish({"type": "a", "i": 1})
    bus.publish({"type": "b", "i": 2})
    bus.publish({"type": "c", "i": 3})

    all_events = bus.recent_events(10)
    mid_seq = all_events[1]["seq"]  # seq of event 'b'

    newer = bus.events_since(mid_seq, n=100)
    assert len(newer) == 1, "events_since 应只返回 c（b 的 seq 不满足 > 条件）"
    assert newer[0]["type"] == "c"
    assert newer[0]["seq"] > mid_seq


def test_events_since_empty_if_none_newer(make_bus):
    bus = make_bus()
    bus.publish({"type": "a"})
    last = bus.recent_events(1)[-1]["seq"]
    assert bus.events_since(last, n=100) == [], "末尾 seq 之后应为空"
