"""配置转换器：把 Job 翻译成目标程序的原生配置文件。

本模块提供 Converter 注册机制。内置不包含任何具体 Converter，
第三方通过 register_converter() 注册：

  from src.converters import register_converter
  from .my_converter import MyConverter
  register_converter("my_program", MyConverter())

详见 docs/converter_development.md。
"""
from __future__ import annotations
from typing import Optional, Protocol


class Converter(Protocol):
    """Converter 协议。实现方只需提供 convert(job, output_dir) 方法。"""
    def convert(self, job, output_dir: str) -> None: ...


_CONVERTERS: dict[str, Converter] = {}


def register_converter(program: str, converter: Converter) -> None:
    """注册自定义 Converter。program 名称全局唯一。"""
    _CONVERTERS[program.lower()] = converter


def get_converter(program: str) -> Optional[Converter]:
    """获取已注册的 Converter，未注册返回 None。"""
    return _CONVERTERS.get((program or "").lower())
