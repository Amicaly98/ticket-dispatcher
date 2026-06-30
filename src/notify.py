"""任务完成后统一推送（Server酱 / ft07）。

挂在 Collector：所有 worker 的结果都流经 collector，这里一处发推送 = 统一、与黑盒无关。
同步发 + 短超时 + 吞异常：推送故障**绝不**影响 collector 主链路。
Collector 已是后台线程，同步调用阻塞几百 ms 可接受。

配置在 config/settings.yaml 的 push 段：
  push:
    enabled: true
    on_success: true
    on_failure: false
    channels:
      - type: sct                    # Server酱 Turbo（GET 请求，URL 自带 key）
        url: "https://xxx.push.ft07.com/send/sctpxxx.send"
      - type: serverchan             # Server酱 经典（POST 请求，需 sendkey）
        sendkey: "SCTxxxxx"
"""
from __future__ import annotations
import logging
from urllib.parse import quote

import httpx

from .config import load_settings

log = logging.getLogger("notify")

_SCT_URL = "https://sctapi.ftqq.com/{key}.send"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")


class Notifier:
    """任务完成后推送器。Collector 在每个结果回流时调用 notify_result。"""

    def __init__(self, cfg: dict | None = None):
        if cfg is None:
            cfg = load_settings().get("push") or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.on_success = bool(cfg.get("on_success", True))
        self.on_failure = bool(cfg.get("on_failure", False))
        self.channels = cfg.get("channels") or []

    def wants(self, success: bool) -> bool:
        """该结果要不要推（供调用方先判断，省掉无谓的 DB 查询）。"""
        if not self.enabled or not self.channels:
            return False
        return self.on_success if success else self.on_failure

    def notify_result(self, result, ctx: dict | None = None) -> None:
        """推送。同步执行（Collector 已是后台线程，阻塞几百 ms 可接受）。"""
        if not self.wants(result.success):
            return
        log.info("准备推送: job=%s success=%s order=%s", result.job_id, result.success, result.order_id)
        self._run(result, dict(ctx or {}))

    def _run(self, result, ctx: dict) -> None:
        try:
            title, desp = self._format(result, ctx)
            log.info("推送消息: title=%s (channels=%d)", title[:60], len(self.channels))
            for ch in self.channels:
                try:
                    self._send_one(ch, title, desp)
                except Exception as e:  # noqa: BLE001
                    log.warning("推送失败 (%s): %s", (ch or {}).get("type"), e, exc_info=True)
        except Exception as e:  # noqa: BLE001
            log.warning("推送处理异常: %s", e, exc_info=True)

    def _format(self, result, ctx: dict) -> tuple[str, str]:
        from .timeutil import fmt_cst
        buyers = ctx.get("buyer_names") or []
        if isinstance(buyers, list):
            buyers = "、".join(str(b) for b in buyers)
        activity = ctx.get("task_id") or result.task_id or "-"
        acct = ctx.get("account_label") or "-"
        acct_id = ctx.get("account_id") or ""
        if result.success:
            title = f"🎉 任务成功！{acct}｜{activity}"
        else:
            title = f"❌ 任务失败 {result.reason}｜{activity}"
        lines = [
            f"**活动**：{activity}",
            f"**账号**：{acct}" + (f"（{acct_id}）" if acct_id else ""),
            f"**购票人**：{buyers or '-'}",
            f"**Worker**：{result.worker_id}",
            f"**订单号**：`{result.order_id or '-'}`",
            f"**结果**：{'成功' if result.success else result.reason}",
        ]
        deadline = ctx.get("deadline")
        if deadline and deadline > 0:
            lines.append(f"**开票时间**：{fmt_cst(deadline)}")
        return title, "\n\n".join(lines)

    def _send_one(self, ch: dict, title: str, desp: str) -> None:
        ctype = (ch or {}).get("type")
        if ctype == "sct":
            base_url = (ch.get("url") or "").strip()
            if not base_url:
                log.warning("sct 渠道缺 url，跳过")
                return
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}title={quote(title[:100])}&desp={quote(desp)}"
            r = httpx.get(url, timeout=8)
            ok = False
            try:
                body = r.json()
                ok = body.get("code", -1) == 0 or body.get("errno", -1) == 0
                if not ok:
                    log.warning("sct 返回非0: code=%s errno=%s msg=%s",
                                body.get("code"), body.get("errno"), body.get("message"))
            except Exception:  # noqa: BLE001
                ok = r.status_code == 200
                if not ok:
                    log.warning("sct 响应非JSON: status=%d body=%s", r.status_code, r.text[:200])
            log.info("sct 推送 -> HTTP %s ok=%s", r.status_code, ok)
        elif ctype == "serverchan":
            key = (ch.get("sendkey") or "").strip()
            if not key:
                log.warning("Server酱 渠道缺 sendkey，跳过")
                return
            r = httpx.post(_SCT_URL.format(key=key),
                           data={"title": title[:100], "desp": desp}, timeout=8)
            ok = False
            try:
                body = r.json()
                ok = body.get("code", -1) == 0
                if not ok:
                    log.warning("Server酱 返回非0: code=%s errno=%s msg=%s",
                                body.get("code"), body.get("errno"), body.get("message"))
            except Exception:  # noqa: BLE001
                log.warning("Server酱 响应非JSON: status=%d body=%s", r.status_code, r.text[:200])
            log.info("Server酱 推送 -> HTTP %s ok=%s", r.status_code, ok)
        else:
            log.warning("未知推送渠道 type=%r，跳过", ctype)
