"""推送（src/notify.py）离线测试：渠道发送 / 消息富化 / 开关门控。

不打真网络——monkeypatch notify 模块里的 httpx。直接调 _run（同步），
绕过 notify_result 的子线程，避免测试里的时序竞争。
"""
from types import SimpleNamespace

import pytest

from src.notify import Notifier


def _result(success=True, order_id="ORD123", reason="ok", worker_id="w1", task_id="演唱会X"):
    return SimpleNamespace(success=success, order_id=order_id, reason=reason,
                           worker_id=worker_id, task_id=task_id)


def _ctx():
    return {"task_id": "演唱会X", "screen_name": "07-01 19:30", "sku_desc": "VIP ¥880",
            "account_label": "用户A", "account_id": "test_123", "buyer_names": ["张三", "李四"]}


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def _serverchan_notifier():
    return Notifier({"enabled": True, "on_success": True, "on_failure": False,
                     "channels": [{"type": "serverchan", "sendkey": "SCTKEY"}]})


def test_wants_gating():
    n = _serverchan_notifier()
    assert n.wants(True) is True
    assert n.wants(False) is False          # on_failure=False
    off = Notifier({"enabled": False, "channels": [{"type": "serverchan", "sendkey": "K"}]})
    assert off.wants(True) is False         # 全局关
    nochan = Notifier({"enabled": True, "channels": []})
    assert nochan.wants(True) is False      # 没渠道


def test_format_success_has_account_order():
    n = _serverchan_notifier()
    title, desp = n._format(_result(), _ctx())
    assert "🎉" in title and "用户A" in title
    assert "ORD123" in desp                 # 订单号
    assert "用户A" in desp and "test_123" in desp   # 账号 + id
    assert "张三、李四" in desp              # 购票人


def test_format_failure():
    n = _serverchan_notifier()
    title, desp = n._format(_result(success=False, reason="sold_out", order_id=""), _ctx())
    assert "❌" in title and "sold_out" in title


def test_send_serverchan_posts(monkeypatch):
    sent = {}

    def fake_post(url, data=None, timeout=None):
        sent["url"] = url
        sent["data"] = data
        return _Resp({"code": 0})

    monkeypatch.setattr("src.notify.httpx.post", fake_post)
    n = _serverchan_notifier()
    n._run(_result(), _ctx())
    assert "sctapi.ftqq.com/SCTKEY.send" in sent["url"]
    assert "🎉" in sent["data"]["title"]
    assert "ORD123" in sent["data"]["desp"]


def test_disabled_notifier_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr("src.notify.httpx.post", lambda *a, **k: calls.append(1) or _Resp({"code": 0}))
    n = Notifier({"enabled": False, "channels": [{"type": "serverchan", "sendkey": "K"}]})
    n.notify_result(_result(), _ctx())
    assert calls == []
