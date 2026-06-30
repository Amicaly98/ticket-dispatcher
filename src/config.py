"""配置加载。"""
from __future__ import annotations
import glob
import os
import time
from typing import Optional

import yaml

from .models import Account, Buyer, WorkerConfig
from .timeutil import parse_deadline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse_time(t) -> float:
    """解析时间为 Unix 时间戳。无 offset 的字符串默认北京时间（Bilibili 开票时间 = CST）。"""
    return parse_deadline(t)


def load_settings(path: Optional[str] = None) -> dict:
    """加载全局设置。"""
    path = path or os.path.join(ROOT, "config", "settings.yaml")
    if not os.path.exists(path):
        return _default_settings()
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or _default_settings()


def _default_settings() -> dict:
    # 降级后只剩 Worker 静态配置 + 可选代理池（无调度参数）+ 购票后推送。
    return {
        "workers": [],
        "proxy_pools": {},
        # 购票后统一推送（Collector 读取，见 src/notify.py）。默认关闭，填 sendkey 后开 enabled。
        "push": {
            "enabled": False,
            "on_success": True,    # 抢到推送
            "on_failure": False,   # 失败也推（cookie 失效等即时知道）
            "fetch_pay_url": True,  # 成功时取真实付款码链接
            "channels": [],        # [{type: serverchan, sendkey: "SCTxxxx"}]
        },
    }


def load_accounts(path: Optional[str] = None) -> dict[str, Account]:
    """加载账号池。"""
    path = path or os.path.join(ROOT, "config", "accounts.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    accounts = {}
    for acc_data in data.get("accounts", []):
        buyers = []
        for b in acc_data.get("buyers", []):
            buyers.append(Buyer(
                id=b["id"],
                name=b.get("name", ""),
                tel=b.get("tel", ""),
                personal_id=b.get("personal_id", ""),
                id_type=b.get("id_type", 0),
            ))
        account = Account(
            account_id=acc_data["account_id"],
            program=acc_data["program"],
            uid=acc_data["uid"],
            username=acc_data.get("username", ""),
            cookie=acc_data.get("cookie", {}),
            buyers=buyers,
            proxy=acc_data.get("proxy", ""),
            vip_status=acc_data.get("vip_status", 0),
            vip_type=acc_data.get("vip_type", 0),
            vip_due_date=acc_data.get("vip_due_date", 0),
        )
        accounts[account.account_id] = account
    return accounts


def load_workers(settings: dict) -> list[WorkerConfig]:
    """加载 Worker 静态声明列表（降级后无 Worker 状态机；worker 自注册，本列表只给默认值）。"""
    workers = []
    for w_data in settings.get("workers", []):
        workers.append(WorkerConfig(
            worker_id=w_data["worker_id"],
            public_ip=w_data.get("public_ip", ""),
            max_slots=w_data.get("max_slots", 3),
            program=w_data.get("program", ""),
            proxy=w_data.get("proxy", ""),
        ))
    return workers


def save_worker_decl(decl: dict, path: Optional[str] = None) -> None:
    """新增/更新一条 worker 声明到 settings.yaml（前端「创建 worker」用）。

    声明只是「离线占位 + program/max_slots 默认值」——worker 能否被指派最终看 bus 心跳。
    decl: {worker_id, program?, max_slots?, public_ip?}
    """
    path = path or os.path.join(ROOT, "config", "settings.yaml")
    settings = load_settings(path)
    workers = settings.get("workers") or []
    wid = decl["worker_id"]
    for i, w in enumerate(workers):
        if w.get("worker_id") == wid:
            workers[i] = {**w, **decl}
            break
    else:
        workers.append(decl)
    settings["workers"] = workers
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def delete_worker_decl(worker_id: str, path: Optional[str] = None) -> bool:
    """从 settings.yaml 移除一条 worker 声明。返回是否真的删到了（不存在返回 False）。"""
    path = path or os.path.join(ROOT, "config", "settings.yaml")
    settings = load_settings(path)
    workers = settings.get("workers") or []
    kept = [w for w in workers if w.get("worker_id") != worker_id]
    if len(kept) == len(workers):
        return False
    settings["workers"] = kept
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(settings, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return True
