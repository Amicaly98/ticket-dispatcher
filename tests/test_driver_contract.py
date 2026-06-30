"""Driver 契约测试：所有 driver 必须满足同一组生命周期不变量。

把「模拟 driver 行为 == 真实 driver 行为」焊死——DryRun/Scripted 在 CI 跑，
真实 Driver 在有二进制时才跑（此处不强制）。断言：
  - prepare() 返回存在的 workdir
  - poll() 状态单调：RUNNING* → DONE，DONE 后带 result，绝不回到 RUNNING
  - cancel() 幂等，且 cancel 后 poll 给出终态
  - cleanup() 可安全调用
"""
import os

import pytest

from src.clock import FakeClock
from src.driver.base import RunState
from src.driver.dryrun import DryRunDriver
from src.models import Buyer, Job

from tests.scripted_driver import Behavior, ScriptedDriver


def _make_job(deadline: float = 0.0) -> Job:
    return Job.build(
        job_id="job-contract-1", task_id="t1", worker_id="worker_001", program="dryrun",
        project_id=1, screen_id=1, sku_id=1, count=1, pay_money=100, deadline=deadline,
        account={"uid": 1, "username": "u", "cookie": {}},
        buyers=[Buyer(id=5, name="x")])


def _drivers(clock):
    return [
        ("dryrun", DryRunDriver(outcome="success", delay=1.0, clock=clock)),
        ("scripted", ScriptedDriver(default=Behavior(outcome="success", delay=1.0), clock=clock)),
    ]


@pytest.mark.parametrize("name,delay_outcome", [("success", True), ("sold_out", False)])
def test_prepare_returns_existing_workdir(name, delay_outcome):
    clock = FakeClock(1000.0)
    for dname, driver in _drivers(clock):
        job = _make_job()
        wd = driver.prepare(job)
        assert os.path.isdir(wd), f"{dname}: prepare 必须返回存在的目录"


def test_poll_state_is_monotonic_until_done():
    clock = FakeClock(1000.0)
    for dname, driver in _drivers(clock):
        job = _make_job(deadline=1000.0)
        driver.prepare(job)
        h = driver.start(job)
        # 未到 ready_at：必须 RUNNING
        state, result = driver.poll(h)
        assert state == RunState.RUNNING, f"{dname}: 未到点应 RUNNING"
        assert result is None
        # 推进虚拟时间越过 ready_at（delay=1.0）
        clock.advance(2.0)
        state, result = driver.poll(h)
        assert state == RunState.DONE, f"{dname}: 到点应 DONE"
        assert result is not None and result.job_id == job.job_id
        # 再 poll 仍 DONE（不回退）
        state2, _ = driver.poll(h)
        assert state2 == RunState.DONE, f"{dname}: DONE 后不应回到 RUNNING"
        driver.cleanup(h)


def test_cancel_is_idempotent_and_yields_terminal():
    clock = FakeClock(1000.0)
    for dname, driver in _drivers(clock):
        job = _make_job(deadline=1000.0)
        driver.prepare(job)
        h = driver.start(job)
        driver.cancel(h)
        driver.cancel(h)  # 幂等：二次取消不报错
        state, result = driver.poll(h)
        assert state == RunState.DONE, f"{dname}: cancel 后应给终态"
        assert result is not None and result.success is False
        driver.cleanup(h)


def test_fault_injection_start_and_poll_raise():
    """ScriptedDriver 的故障注入：start/poll 抛异常应能被上层捕获（此处验证确实抛出）。"""
    clock = FakeClock(1000.0)
    d_start = ScriptedDriver(default=Behavior(raise_on_start=True), clock=clock)
    job = _make_job()
    d_start.prepare(job)
    with pytest.raises(RuntimeError):
        d_start.start(job)

    d_poll = ScriptedDriver(default=Behavior(raise_on_poll=True), clock=clock)
    d_poll.prepare(job)
    h = d_poll.start(job)
    with pytest.raises(RuntimeError):
        d_poll.poll(h)
