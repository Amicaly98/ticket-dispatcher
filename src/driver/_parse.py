"""共享的目标程序 stdout 解析器。

各 Driver 的 _parse_result 有近乎相同的解析逻辑：
  1. JSON 中找 orderId
  2. JSON 中找 errno=0
  3. 成功关键字匹配
  4. 退出码兜底

这里抽成共享函数，多个 Driver 复用，也便于离线测试。
自定义 Driver 可以直接使用 parse_target_output()，也可以在此基础上扩展。
"""
from __future__ import annotations
import json
import re
from typing import Optional

from ..models import AttemptResult, Job


def strip_ansi(text: str) -> str:
    """去除 ANSI 转义序列（颜色、光标控制等）。"""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


# 成功关键字单一真相源：解析器与各 Driver 的 poll DONE 检测共用，避免漂移。
# 自定义 Driver 可通过 add_success_keywords() 追加项目特定关键字。
SUCCESS_KEYWORDS = [
    "order_create_success",
    "下单成功",
    "订单创建成功",
]


def add_success_keywords(*keywords: str) -> None:
    """追加自定义成功关键字（在 Driver 初始化时调用）。"""
    for kw in keywords:
        if kw not in SUCCESS_KEYWORDS:
            SUCCESS_KEYWORDS.append(kw)


# detail 尾巴上限：行数 + 字节，防止把整段日志塞进 Bus / DB
_DETAIL_TAIL_LINES = 30
_DETAIL_MAX_CHARS = 4096

# 已知错误关键字 → 友好 reason 短码（命中第一个为准，顺序敏感）
_REASON_KEYWORDS = (
    ("未登录", "not_logged_in"),
    ("登录失效", "not_logged_in"),
    ("登录已失效", "not_logged_in"),
    ("库存不足", "sold_out"),
    ("已售罄", "sold_out"),
    ("无票", "sold_out"),
    ("已售完", "sold_out"),
    ("票已售完", "sold_out"),
    ("风控", "risk_control"),
    ("验证码", "captcha"),
    ("频繁", "rate_limited"),
    ("项目不可售", "not_on_sale"),
    ("未开售", "not_on_sale"),
)


def _classify(text: str) -> str:
    """从 stdout 全文里识别已知错误关键字，命中则返回友好短码，否则空串。"""
    for kw, code in _REASON_KEYWORDS:
        if kw in text:
            return code
    return ""


def _build_detail(text: str, retcode: int) -> str:
    """失败诊断详情：stdout 最后 N 行 + 退出码，限长。"""
    lines = text.splitlines()
    tail = "\n".join(lines[-_DETAIL_TAIL_LINES:])
    if len(tail) > _DETAIL_MAX_CHARS:
        tail = "…" + tail[-_DETAIL_MAX_CHARS:]
    suffix = f"\n(exit={retcode})"
    return (tail + suffix).strip()


def parse_target_output(
    text: str,
    retcode: int,
    job: Job,
    extra_reason: str = "",
) -> AttemptResult:
    """从目标程序的 stdout 文本 + 退出码解析结构化 AttemptResult。

    Args:
        text: 完整 stdout 文本
        retcode: 进程退出码
        job: 对应的 Job（用于填充 buyer_ids 等）
        extra_reason: 额外原因标记
    """
    lines = text.splitlines()

    # ── 模式 1：JSON 中找 orderId ──
    order_id = ""
    for line in lines:
        if "orderId" in line:
            try:
                for m in re.finditer(r'\{[^{}]*"orderId"[^{}]*\}', line):
                    obj = json.loads(m.group())
                    oid = obj.get("orderId") or obj.get("data", {}).get("orderId", "")
                    if oid:
                        order_id = str(oid)
                        break
            except (json.JSONDecodeError, AttributeError):
                pass
        if order_id:
            break

    # ── 模式 1b：纯文本找订单号 ──
    if not order_id:
        for line in lines:
            m = re.search(r'订单号[：:\s]*(\d{10,})', line)
            if m:
                order_id = m.group(1)
                break

    # ── 模式 2：JSON 中找 errno=0 ──
    success = bool(order_id)
    reason = "ok" if success else ""

    if not success:
        for line in lines:
            if "errno" in line:
                try:
                    for m in re.finditer(r'\{[^{}]*"errno"[^{}]*\}', line):
                        obj = json.loads(m.group())
                        if obj.get("errno") == 0:
                            success = True
                            reason = "ok"
                            break
                except (json.JSONDecodeError, AttributeError):
                    pass
            if success:
                break

    # ── 模式 3：成功关键字 ──
    if not success:
        for kw in SUCCESS_KEYWORDS:
            if kw in text:
                success = True
                reason = "ok"
                break

    # ── 模式 4：已知错误关键字 → 友好短码 ──
    if not success and (not reason or reason == "no_result"):
        kw_reason = _classify(text)
        if kw_reason:
            reason = kw_reason

    # ── 模式 5：退出码兜底 ──
    if not success and not reason:
        if extra_reason:
            reason = extra_reason
        elif retcode == 0:
            reason = "no_result"
        else:
            reason = f"exit_{retcode}"

    if not success and not reason:
        reason = "unknown"

    buyers_secured = job.buyer_ids if success else []
    # 失败才带 detail（成功无需诊断，避免无谓占用 Bus/DB）
    detail = "" if success else _build_detail(text, retcode)

    return AttemptResult(
        job_id=job.job_id,
        task_id=job.task_id,
        worker_id=job.worker_id,
        success=success,
        order_id=order_id,
        reason=reason,
        detail=detail,
        buyers_secured=buyers_secured,
        buyers_attempted=job.buyer_ids,
    )
