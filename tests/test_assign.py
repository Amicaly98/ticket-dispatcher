"""指派链路测试（降级后的核心契约）。

没有调度器后，唯一的核心流程是：用户手动指派 → Job 入 bus → 指定 worker 消费 →
执行 → result 回流 → worker 回 IDLE。这里用 InMemoryBus + ScriptedDriver + FakeClock
在虚拟时间里把这条链焊死。
"""
from src.bus import InMemoryBus
from src.clock import FakeClock
from src.models import Buyer, ControlSignal, Job
from src.worker_agent import WorkerAgent

from tests.scripted_driver import Behavior, ScriptedDriver


def _job(worker_id="w1", deadline=1000.0) -> Job:
    return Job.build(
        job_id="job-assign-1", task_id="ComicQuest VIP", worker_id=worker_id,
        program="scripted", project_id=42, screen_id=1, sku_id=1, count=1,
        pay_money=100, deadline=deadline,
        account={"uid": 1, "username": "u", "cookie": {}},
        buyers=[Buyer(id=501, name="甲")])


def _agent(bus, clock, behavior):
    driver = ScriptedDriver(default=behavior, clock=clock)
    return WorkerAgent("w1", bus, max_slots=1, clock=clock,
                       driver_factory=lambda program: driver)


def test_assign_runs_and_returns_to_idle():
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(outcome="success", delay=1.0))

    # 模拟 API 的 assign：push 定向 job
    bus.push_job(_job("w1"))

    # worker 消费 → 启动 → RUNNING（未到 ready_at，无结果）
    agent.tick_once()
    assert agent.executor.busy_slots == 1, "job 应在执行中"
    assert bus.pop_result(timeout=0) is None

    # 推进虚拟时间越过 ready_at（deadline 1000 + delay 1）
    clock.advance(2.0)
    agent.tick_once()

    # 结果回流 + worker 抢到后回 IDLE
    result = bus.pop_result(timeout=0)
    assert result is not None and result.success is True
    assert result.job_id == "job-assign-1"
    assert result.buyers_secured == [501]
    assert agent.executor.busy_slots == 0, "抢到后应回到空闲"


def test_stop_cancels_inflight():
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(never_finish=True))

    bus.push_job(_job("w1"))
    agent.tick_once()
    assert agent.executor.busy_slots == 1
    assert agent._current is not None, "运行中应有当前任务富信息"

    # 前端"停止" = cancel 信号 → 立即终止在飞 job
    bus.push_signal(ControlSignal(signal="cancel", worker_id="w1"))
    agent.tick_once()
    assert agent.executor.busy_slots == 0, "stop 应终止在飞 job"

    # 关键回归：停止必须产出一条 cancelled 结果，并清掉「当前」（否则前端无反应/卡旧任务）
    result = bus.pop_result(timeout=0)
    assert result is not None and result.success is False
    assert result.reason == "cancelled"
    assert agent._current is None, "停止后应清空当前任务"
    assert agent._current_job_id is None


def test_current_enriched_during_run():
    """运行中心跳应带「当前在抢什么」的富信息（活动/场次/账号/购票人）。"""
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(never_finish=True))

    bus.push_job(_job("w1"))
    agent.tick_once()

    cur = agent._current
    assert cur is not None
    assert cur["label"] == "ComicQuest VIP"
    assert cur["project_id"] == 42
    assert cur["screen_id"] == 1
    assert cur["account"] == "u"
    assert cur["buyers"] == ["甲"]
    assert "started_at" in cur

    # 富信息应进心跳 → list_workers 能读到
    live = bus.list_workers()["w1"]
    assert live["current"]["label"] == "ComicQuest VIP"


def test_pause_blocks_consumption_resume_restores():
    """暂停 = 停止消费新 Job（worker 不动在飞任务）；恢复 = 重新消费。"""
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(outcome="success", delay=1.0))

    # 先暂停，再派单 → tick 不应消费
    bus.push_signal(ControlSignal(signal="pause", worker_id="w1"))
    bus.push_job(_job("w1"))
    agent.tick_once()
    assert agent._paused is True
    assert agent.executor.busy_slots == 0, "暂停期间不应消费新 Job"
    assert bus.list_workers()["w1"]["status"] == "paused"
    assert bus.list_workers()["w1"]["paused"] is True

    # 恢复 → 下一 tick 消费那张挂着的单
    bus.push_signal(ControlSignal(signal="resume", worker_id="w1"))
    agent.tick_once()
    assert agent._paused is False
    assert agent.executor.busy_slots == 1, "恢复后应消费 Job"


def test_current_has_assigned_at():
    """当前富信息应带 assigned_at（指派时刻），用于显示卡点单已挂多久。"""
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(never_finish=True))

    job = _job("w1")
    bus.push_job(job)
    agent.tick_once()
    assert agent._current["assigned_at"] == job.created_at


def test_worker_log_events_published():
    """worker 把黑盒增量 stdout 推成 worker_log 事件（用 DryRunDriver 合成日志验证）。"""
    from src.driver.dryrun import DryRunDriver
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    driver = DryRunDriver(outcome="success", delay=1.0, clock=clock)
    agent = WorkerAgent("w1", bus, max_slots=1, clock=clock,
                        driver_factory=lambda program: driver)

    bus.push_job(_job("w1"))
    agent.tick_once()   # consume + start + 首次 pump_logs

    logs = [e for e in bus.recent_events() if e.get("type") == "worker_log"]
    assert logs, "应推出 worker_log 事件"
    assert logs[0]["worker_id"] == "w1"
    assert any("dryrun" in line for line in logs[0]["lines"])


def test_ws_cursor_survives_identical_timestamps(monkeypatch):
    """WS 增量游标必须用 seq，不能用墙钟 ts——这是"实时日志推不出去、重启才显示"的根因。

    复现：把 time.time 钉死成常量，模拟同一墙钟 tick 内成批 publish（Windows ts 分辨率 ~15ms）。
    旧逻辑 `ts > last_ts` 在推完第一批后会把同 ts 的后续事件全部永久滤掉；改用 seq 后一条不丢。
    """
    import src.bus as bus_mod
    monkeypatch.setattr(bus_mod.time, "time", lambda: 1000.0)  # 所有事件 ts 完全相同
    bus = InMemoryBus()

    # 模拟 WS 循环：连接时取末尾 seq，之后按 seq > last_seq 增量推
    bus.publish({"type": "worker_log", "worker_id": "w1", "lines": ["batch0"]})
    last_seq = bus.recent_events(n=1)[-1]["seq"]

    # 同一墙钟窗口内再来一批（ts 与上面完全相同）
    for i in range(1, 6):
        bus.publish({"type": "worker_log", "worker_id": "w1", "lines": [f"batch{i}"]})

    delta = [e for e in bus.recent_events(n=100) if e["seq"] > last_seq]
    assert len(delta) == 5, "同 ts 的后续 5 条事件必须全部被增量取到，一条不漏"
    assert {e["lines"][0] for e in delta} == {f"batch{i}" for i in range(1, 6)}
    # 反证：若退回用 ts，全部事件 ts 相同 → `ts > last_ts` 取到 0 条（旧 bug）
    last_ts = 1000.0
    assert len([e for e in bus.recent_events(n=100) if e["ts"] > last_ts]) == 0


def test_assign_uses_worker_heartbeat_program(monkeypatch):
    """自注册 worker（不在 settings.yaml）派单时，Job.program 必须取心跳上报的 program。

    回归守卫：旧逻辑 `body.program or (wc.program if wc else acc.program)` 对自注册 worker
    wc=None → 落到账号 program。给一个心跳上报 other 的自注册 worker 派单会错用 test_driver
    converter（生成 config.yaml 而非 .sba）。修复后优先用心跳 program。
    """
    from src import api as api_mod
    from src.models import Account, Buyer as MBuyer

    pushed = []

    class _FakeBus:
        def list_workers(self):
            # 自注册 other worker 的心跳
            return {"worker_cloud": {"worker_id": "worker_cloud", "status": "idle",
                                     "program": "other"}}
        def push_job(self, job):
            pushed.append(job)
        def publish(self, ev):
            pass

    acc = Account(account_id="test_1", program="test", uid=1, username="u",
                  cookie={"sess_data": "x"}, buyers=[MBuyer(id=501, name="甲")])
    monkeypatch.setattr(api_mod, "_bus", _FakeBus())
    monkeypatch.setattr(api_mod, "_worker_configs", {})  # worker_cloud 不在声明里（自注册）
    monkeypatch.setattr(api_mod, "_store", None)
    monkeypatch.setattr("src.config.load_accounts", lambda *a, **k: {"test_1": acc})

    api_mod.assign_worker("worker_cloud", {
        "account_id": "test_1", "buyer_ids": [501],
        "project_id": 999, "screen_id": 1, "sku_id": 1, "pay_money": 100, "deadline": ""})

    assert len(pushed) == 1
    assert pushed[0].program == "other", "应取 worker 心跳的 program，而非账号的 test_driver"


def test_assign_body_program_overrides_heartbeat(monkeypatch):
    """body 显式 program 优先级最高，覆盖心跳。"""
    from src import api as api_mod
    from src.models import Account, Buyer as MBuyer

    pushed = []

    class _FakeBus:
        def list_workers(self):
            return {"w": {"program": "other"}}
        def push_job(self, job): pushed.append(job)
        def publish(self, ev): pass

    acc = Account(account_id="a1", program="test", uid=1, username="u",
                  cookie={"sess_data": "x"}, buyers=[MBuyer(id=1, name="甲")])
    monkeypatch.setattr(api_mod, "_bus", _FakeBus())
    monkeypatch.setattr(api_mod, "_worker_configs", {})
    monkeypatch.setattr(api_mod, "_store", None)
    monkeypatch.setattr("src.config.load_accounts", lambda *a, **k: {"a1": acc})

    api_mod.assign_worker("w", {
        "account_id": "a1", "buyer_ids": [1], "program": "test_driver",
        "project_id": 1, "screen_id": 1, "sku_id": 1, "pay_money": 1, "deadline": ""})
    assert pushed[0].program == "test_driver"


def test_executor_cancel_emits_result():
    """Executor.cancel 必须产出 cancelled 结果入队（停止可见性的根因修复）。"""
    from src.executor import Executor
    clock = FakeClock(1000.0)
    ex = Executor(max_slots=1, clock=clock)
    driver = ScriptedDriver(default=Behavior(never_finish=True), clock=clock)
    job = _job("w1")
    driver.prepare(job)
    assert ex.start(job, driver) is True

    assert ex.cancel(job.job_id) is True
    done, _final = ex.tick()   # cancelled 结果应已入队 _done，被 tick 取走
    assert len(done) == 1
    _, result = done[0]
    assert result.success is False and result.reason == "cancelled"


def test_executor_drain_output():
    """drain_output 收集在飞 slot 的增量 stdout（DryRunDriver 合成日志）。"""
    from src.executor import Executor
    from src.driver.dryrun import DryRunDriver
    clock = FakeClock(1000.0)
    ex = Executor(max_slots=1, clock=clock)
    driver = DryRunDriver(outcome="success", delay=1.0, clock=clock)
    job = _job("w1")
    driver.prepare(job)
    ex.start(job, driver)

    out = ex.drain_output()
    assert out and out[0][0].job_id == job.job_id
    assert any("dryrun" in line for line in out[0][1])
    # 游标推进：第二次无新行
    assert ex.drain_output() == []


def test_inmemorybus_events_since():
    """InMemoryBus.events_since(seq) 只返回 seq > 给定值的事件。"""
    bus = InMemoryBus()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})
    bus.publish({"type": "c"})
    mid_seq = bus.recent_events(10)[1]["seq"]  # seq of 'b'
    newer = bus.events_since(mid_seq, n=100)
    assert len(newer) == 1 and newer[0]["type"] == "c"
    assert bus.events_since(mid_seq + 999, n=100) == []


def test_failed_assign_also_returns_to_idle():
    bus = InMemoryBus()
    clock = FakeClock(1000.0)
    agent = _agent(bus, clock, Behavior(outcome="sold_out", delay=1.0))

    bus.push_job(_job("w1"))
    agent.tick_once()
    clock.advance(2.0)
    agent.tick_once()

    result = bus.pop_result(timeout=0)
    assert result is not None and result.success is False
    assert result.reason == "sold_out"
    assert agent.executor.busy_slots == 0, "失败后也应回到空闲"
