"""数据模型。

降级后系统是「手动指派台 + Worker fleet」，不再有自动调度，故无 Task/调度状态机。
两类对象：
1. 号池活对象（Account/Buyer）+ Worker 静态配置（WorkerConfig）——进程内。
2. 跨进程契约（Job/AttemptResult/ControlSignal）——纯数据、可 JSON 序列化，
   是「API 节点 ↔ Worker 节点」之间的唯一通信单位（经 bus 派发/回流）。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import time

from pydantic import BaseModel, Field


# ── 购票人 ──

@dataclass
class Buyer:
    id: int
    name: str
    tel: str = ""
    personal_id: str = ""
    id_type: int = 0              # 0=身份证, 1=护照, ...

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Buyer":
        return cls(
            id=d["id"], name=d.get("name", ""), tel=d.get("tel", ""),
            personal_id=d.get("personal_id", ""), id_type=d.get("id_type", 0),
        )


# ── 账号（号池） ──

@dataclass
class Account:
    account_id: str
    program: str                  # Driver 名称（注册时的 program 值）
    uid: int
    username: str
    cookie: dict                  # 平台凭证（由 Converter 解释，格式因平台而异）
    buyers: list = field(default_factory=list)  # list[Buyer]
    proxy: str = ""               # 代理地址
    status: str = "idle"          # 静态字段（号池无运行态）
    # 平台扩展字段（可选，由平台 API 扩展写入）
    vip_status: int = 0           # 0=无效 1=有效
    vip_type: int = 0             # 平台特定的会员类型
    vip_due_date: int = 0         # 到期时间（毫秒时间戳）

    def snapshot(self) -> dict:
        """跨进程传输用的账号快照（含 cookie，Worker 端 Converter 需要）。"""
        return {
            "account_id": self.account_id,
            "program": self.program,
            "uid": self.uid,
            "username": self.username,
            "cookie": self.cookie,
            "proxy": self.proxy,
        }


# ══════════════════════════════════════════════════════════════════
#  跨进程契约（纯数据，可 JSON 序列化）—— API 节点 ↔ Worker 节点
# ══════════════════════════════════════════════════════════════════

class Job(BaseModel):
    """一次「在某 Worker 上、用某账号、对某批 buyer 发起一次抢票」的可序列化指令。

    这是用户手动指派给某个 Worker 的唯一单位，完全自包含（号-worker 多对多自由指派，
    系统不维护任何全局状态）。`deadline` 透传给黑盒 bot，由 bot 自己 NTP 倒计时卡点。
    pydantic 模型：远端反序列化时校验字段+类型+版本，缺字段/类型错显式报错而非静默。
    """
    schema_version: int = 2
    job_id: str
    task_id: str = ""             # 活动标签/关联键（人类可读，前端显示"worker 在抢什么"）
    program: str                  # Driver 名称（注册时的 program 值）
    worker_id: str
    project_id: int
    screen_id: int
    sku_id: int
    screen_name: str = ""         # 场次文案（如 "2026-07-01 19:30"），前端「当前」显示用
    sku_desc: str = ""            # 票档文案（如 "VIP内场 ¥880"），前端「当前」显示用
    count: int
    pay_money: int
    deadline: float               # 开售时间戳（精确时序由黑盒 bot 自己做）
    account: dict                 # Account.snapshot()
    buyers: list                  # list[buyer dict]
    retry_interval: float = 0.3   # 透传给 bot 的下单重试间隔
    check_interval: float = 3.0   # 透传给 bot 的检查间隔
    enable_check_stock: bool = False  # 蹲票模式：True=先查库存再下单（等退票/补票）
    attempt_no: int = 0           # DryRun/Scripted 故障注入用
    created_at: float = Field(default_factory=time.time)

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls.model_validate(d)

    @property
    def buyer_ids(self) -> list:
        return [b["id"] for b in self.buyers]

    @classmethod
    def build(cls, *, job_id: str, worker_id: str, program: str, project_id: int,
              screen_id: int, sku_id: int, count: int, pay_money: int, deadline: float,
              account, buyers: list, task_id: str = "",
              screen_name: str = "", sku_desc: str = "",
              retry_interval: float = 0.3, check_interval: float = 3.0,
              enable_check_stock: bool = False,
              attempt_no: int = 0) -> "Job":
        """从原语构造 Job。account 可为 dict（已 snapshot）或 Account 对象；buyers 同理。"""
        return cls(
            job_id=job_id, task_id=task_id, program=program, worker_id=worker_id,
            project_id=project_id, screen_id=screen_id, sku_id=sku_id,
            screen_name=screen_name, sku_desc=sku_desc,
            count=count, pay_money=pay_money, deadline=deadline,
            account=account if isinstance(account, dict) else account.snapshot(),
            buyers=[b.to_dict() if isinstance(b, Buyer) else dict(b) for b in buyers],
            retry_interval=retry_interval, check_interval=check_interval,
            enable_check_stock=enable_check_stock,
            attempt_no=attempt_no,
        )


class AttemptResult(BaseModel):
    """Worker 节点执行完一个 Job 后回流给 API 节点的结构化结果。"""
    schema_version: int = 2
    job_id: str
    task_id: str = ""             # 关联键（= Job.task_id 活动标签）
    worker_id: str
    success: bool
    order_id: str = ""
    reason: str = ""              # sold_out / not_logged_in / cloud_fail / timeout / error / ok
    detail: str = ""              # 失败时的诊断详情（stdout 尾巴 + 退出码 / traceback），限长 ~4KB
    buyers_secured: list = Field(default_factory=list)   # list[buyer id]
    buyers_attempted: list = Field(default_factory=list)
    ts: float = Field(default_factory=time.time)

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> "AttemptResult":
        return cls.model_validate(d)


class ControlSignal(BaseModel):
    """API 节点 → Worker 节点的控制信号。

    - cancel：前端「停止」，终止在飞 Job，随即回 IDLE。
    - pause / resume：前端「暂停/恢复」开关。pause 只挡**新** Job 消费（在飞 Job 继续跑完，
      进程不退），便于异常处理时把某 worker 临时摘下；resume 恢复消费。
    """
    schema_version: int = 2
    signal: str                   # cancel / pause / resume
    worker_id: str
    task_id: str = ""
    job_id: str = ""
    ts: float = Field(default_factory=time.time)

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict) -> "ControlSignal":
        return cls.model_validate(d)


# ══════════════════════════════════════════════════════════════════
#  Worker 静态配置（worker = 一个出口 IP / 一台机器）
#  降级后 worker 自注册（起进程即往 bus 发心跳）：本配置只是「声明」，
#  给 assign 提供 program/max_slots 默认值 + 前端展示 public_ip。
#  真正能否被指派看 bus 心跳，不看这里——所以不再有 host/ssh_key（自动调度时代靠 SSH 拉起远端，已废）。
# ══════════════════════════════════════════════════════════════════

@dataclass
class WorkerConfig:
    worker_id: str
    public_ip: str = ""
    max_slots: int = 3
    program: str = ""
    proxy: str = ""
