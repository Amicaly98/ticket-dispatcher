"""时钟抽象：把"现在几点"和"睡一会儿"收敛成一个可注入的接口。

WorkerAgent / Executor / Driver 都是时间驱动的（心跳间隔、轮询间隔、bot 卡点 deadline）。
如果测试用真实 `time.time()`，一个"deadline 到点才完成"的用例就要真等。把时钟抽出来后：
  - 生产用 RealClock（包 time.time/time.sleep）
  - 测试用 FakeClock（虚拟时间，advance(dt) 推进，sleep 只记账不真睡）

注入到 WorkerAgent/Executor/DryRunDriver，让指派链路测试和 driver 契约测试在虚拟时间里跑。
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时钟接口。now() 返回 Unix 时间戳；sleep() 推进/等待。"""

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class RealClock:
    """真实时钟：直接代理 time 模块。生产默认。"""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """虚拟时钟：测试用。时间只在 advance()/sleep() 时前进，绝不真睡。

    用法：
        clk = FakeClock(start=1_000_000.0)
        clk.advance(300)          # 直接跳 300 秒（模拟到了 rush 窗口外）
        clk.now()                 # -> 1_000_300.0
        clk.sleep(0.1)            # 等价于 advance(0.1)，不阻塞
    """

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        # 测试里 sleep 等价于推进虚拟时间，不真正阻塞
        if seconds > 0:
            self._t += seconds

    def advance(self, seconds: float) -> None:
        """显式推进虚拟时间。"""
        self._t += float(seconds)

    def set(self, t: float) -> None:
        """跳到某个绝对时间戳。"""
        self._t = float(t)


# 进程级默认时钟（生产代码不显式传 clock 时回退到它）
DEFAULT_CLOCK: Clock = RealClock()
