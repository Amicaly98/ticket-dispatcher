"""Driver 工厂。

内置只有 DryRunDriver（端到端验证用）。第三方 Driver 通过 register_driver() 注册：
  from src.driver import register_driver
  from .my_driver import MyDriver
  register_driver("my_program", MyDriver)

详见 docs/driver_development.md。
"""
from __future__ import annotations
import logging
from typing import Type

from .base import Driver
from .dryrun import DryRunDriver

_DRIVERS: dict[str, Type[Driver]] = {
    "dryrun": DryRunDriver,
}


def register_driver(program: str, cls: Type[Driver]) -> None:
    """注册自定义 Driver。program 名称全局唯一，后注册覆盖先注册。"""
    _DRIVERS[program.lower()] = cls


def get_driver(program: str, force_dryrun: bool = False) -> Driver:
    """获取驱动实例。DRYRUN=1 环境变量或 force_dryrun=True 时强制用 DryRunDriver。"""
    import os
    if force_dryrun or os.environ.get("DRYRUN") == "1":
        return DryRunDriver()
    cls = _DRIVERS.get((program or "").lower())
    if cls is None:
        logging.getLogger("driver").warning(
            "未知 program=%s，回退 DryRunDriver（请通过 register_driver() 注册自定义 Driver）",
            program)
        return DryRunDriver()
    return cls()
