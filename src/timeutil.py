"""北京时间 (UTC+8) 工具函数。

Bilibili 开票时间始终是北京时间。全球部署时服务器可能在不同时区，
所有与开票时间相关的解析/显示必须用显式 UTC+8，不能依赖服务器 local time。
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

# 北京时间时区常量
CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    """当前北京时间（aware datetime）。"""
    return datetime.now(CST)


def epoch_to_cst(epoch: float) -> datetime:
    """Unix 时间戳 → 北京时间 aware datetime。"""
    return datetime.fromtimestamp(epoch, tz=CST)


def fmt_cst(epoch: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Unix 时间戳 → 北京时间格式化字符串。"""
    return epoch_to_cst(epoch).strftime(fmt)


def parse_cst_str(s: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> float:
    """北京时间格式字符串 → Unix 时间戳。

    Bilibili API 返回的 'YYYY-MM-DD HH:MM:SS' 是北京时间。
    """
    dt = datetime.strptime(s, fmt).replace(tzinfo=CST)
    return dt.timestamp()


def parse_deadline(raw) -> float:
    """统一解析 deadline 输入为 Unix 时间戳（秒）。

    规则：
      - int/float → 直接当 epoch
      - 带 offset 的 ISO 字符串（如 '2026-07-10T18:00:00+08:00'）→ 按 offset 解析
      - 'Z' 后缀（如 '2026-07-10T10:00:00Z'）→ UTC
      - 无 offset 的 ISO 字符串（如 '2026-07-10T18:00:00'）→ 默认北京时间
      - 其他 → 0.0
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return 0.0
    s = raw.strip()
    if not s:
        return 0.0

    # 'Z' 后缀 → 替换为 +00:00（Python 3.10 fromisoformat 不认 Z）
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0

    if dt.tzinfo is not None:
        # 有 offset → 直接转 epoch
        return dt.timestamp()
    else:
        # 无 offset → 默认北京时间（Bilibili 开票时间 = 北京时间）
        return dt.replace(tzinfo=CST).timestamp()
