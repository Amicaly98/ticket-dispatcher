"""示例 Converter 骨架：把 Job 翻译成目标程序的配置文件。

本文件展示了 Converter 的所有接口和最佳实践。
复制此文件并替换 TODO 标记处的逻辑，即可实现你自己的 Converter。

使用方式：
1. 复制此文件，重命名为 your_converter.py
2. 替换 ExampleConverter 为你的类名
3. 实现 convert() 方法中的逻辑
4. 在 src/converters/__init__.py 中注册，或调用 register_converter()

详见 docs/converter_development.md。
"""
from __future__ import annotations
import json
import logging
import os

log = logging.getLogger("converter.example")


class ExampleConverter:
    """示例 Converter：把 Job 转换成 JSON 配置文件。

    Converter 使用 Protocol 机制，不需要继承基类。
    只需实现 convert(job, output_dir) 方法即可。

    TODO: 替换为你的真实实现。
    """

    def convert(self, job, output_dir: str) -> None:
        """把 Job 翻译成目标程序的配置文件，写入 output_dir。

        Args:
            job: Job 对象（pydantic 模型），包含所有任务信息
                - job.account: 账号快照（含 cookie、uid、proxy 等）
                - job.buyers: 购票人列表
                - job.project_id / screen_id / sku_id: 活动/场次/票档
                - job.deadline: 开售时间戳
                - job.count: 购买数量
                - job.pay_money: 支付金额（分）
            output_dir: 配置文件输出目录（已存在，由 Driver 的 workdir() 创建）

        注意：
        - 必须幂等——多次调用结果相同
        - 不应抛出异常（Driver 的 prepare() 会捕获并兜底）
        - 如果缺少必要数据，记录警告并生成最小可用配置
        """
        # ---- 从 Job 提取账号信息 ----
        account = job.account
        cookie = account.get("cookie", {})

        # ---- TODO: 构建你的目标程序配置格式 ----
        # 以下是一个 JSON 配置示例，替换为你目标程序期望的格式
        config = {
            # 账号信息
            "account": {
                "uid": account.get("uid"),
                "username": account.get("username"),
                # TODO: 替换为你的平台凭证字段名
                "session_token": cookie.get("session_token", ""),
                "csrf_token": cookie.get("csrf_token", ""),
            },
            # 代理设置（如果目标程序支持）
            "proxy": account.get("proxy", ""),
            # 目标活动
            "target": {
                "project_id": job.project_id,
                "screen_id": job.screen_id,
                "sku_id": job.sku_id,
                "count": job.count,
                "pay_money": job.pay_money,
                "deadline": job.deadline,
            },
            # 购票人
            "buyers": [
                {
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "personal_id": b.get("personal_id"),
                    "id_type": b.get("id_type", 0),
                }
                for b in job.buyers
            ],
            # 运行时选项
            "options": {
                "retry_interval": job.retry_interval,
                "check_interval": job.check_interval,
                "enable_check_stock": job.enable_check_stock,
            },
        }

        # ---- TODO: 根据需要添加更多配置 ----
        # 例如：环境特定配置、Feature flags 等

        # ---- 写入配置文件 ----
        # TODO: 替换为你目标程序期望的文件名和格式
        config_path = os.path.join(output_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        log.info("[%s] 配置已生成 -> %s", job.job_id, config_path)
