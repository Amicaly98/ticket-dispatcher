"""示例 Driver 骨架：实现一个完整的自定义 Driver。

本文件展示了 Driver 的所有接口和最佳实践。
复制此文件并替换 TODO 标记处的逻辑，即可实现你自己的 Driver。

使用方式：
1. 复制此文件，重命名为 your_driver.py
2. 替换 ExampleDriver 为你的类名
3. 修改 program 类属性为你的程序标识符
4. 实现各方法中的 TODO 逻辑
5. 在 src/driver/__init__.py 中注册，或调用 register_driver()

详见 docs/driver_development.md。
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
from typing import Optional

from src.driver.base import Driver, RunState
from src.driver._parse import parse_target_output, add_success_keywords
from src.driver._proctree import kill_process_tree
from src.models import AttemptResult, Job

log = logging.getLogger("driver.example")


class _Handle:
    """封装进程状态。

    poll/cancel/cleanup/new_output 都通过这个对象操作。
    不要在 Driver 实例上存储 per-job 状态——多个 Job 共享同一个 Driver 实例。

    字段说明：
        proc: subprocess.Popen 对象，代表拉起的黑盒进程
        job: 对应的 Job（用于解析结果时填充 buyer_ids 等）
        stdout_lines: 累积的 stdout 行（new_output 用游标返回增量）
        out_cursor: 上次 new_output 返回到的位置
    """
    def __init__(self, proc: subprocess.Popen, job: Job):
        self.proc = proc
        self.job = job
        self.stdout_lines: list[str] = []
        self.out_cursor: int = 0


class ExampleDriver(Driver):
    """示例 Driver：模拟一个外部购票程序。

    TODO: 替换为你的真实实现。
    """
    # ---- 全局唯一标识符 ----
    # 设置 WORKER_PROGRAM 环境变量为此值，worker 就会使用这个 Driver
    program = "example"

    def __init__(self):
        # ---- 可选：注册自定义成功关键字 ----
        # 如果你的目标程序输出独特的成功标识，可以在这里追加
        # add_success_keywords("your_success_keyword")

        # ---- 可选：配置项 ----
        self.binary_path = os.environ.get(
            "EXAMPLE_EXE",
            # TODO: 替换为你的黑盒程序默认路径
            "./your_bot_binary",
        )

    def prepare(self, job: Job) -> str:
        """把 Job 翻译成目标程序的配置文件。

        典型操作：
        1. 创建 per-job 工作目录（用 self.workdir(job)）
        2. 写出配置文件（JSON/YAML/INI/环境变量）
        3. 返回工作目录路径

        注意：
        - 必须幂等——多次调用结果相同
        - cookie 等敏感数据从 job.account 读取，不要落盘到日志
        """
        # 创建 per-job 工作目录
        wd = self.workdir(job)

        # ---- 从 Job 提取账号信息 ----
        account = job.account
        cookie = account.get("cookie", {})

        # ---- TODO: 构建你的目标程序配置 ----
        # 以下是一个 JSON 配置示例，替换为你目标程序期望的格式
        config = {
            "account": {
                "uid": account.get("uid"),
                "username": account.get("username"),
                "session_token": cookie.get("session_token", ""),
                "csrf_token": cookie.get("csrf_token", ""),
            },
            "target": {
                "project_id": job.project_id,
                "screen_id": job.screen_id,
                "sku_id": job.sku_id,
                "count": job.count,
                "pay_money": job.pay_money,
                "deadline": job.deadline,
            },
            "buyers": [
                {
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "personal_id": b.get("personal_id"),
                    "id_type": b.get("id_type", 0),
                }
                for b in job.buyers
            ],
            "options": {
                "retry_interval": job.retry_interval,
                "check_interval": job.check_interval,
                "enable_check_stock": job.enable_check_stock,
            },
        }

        # ---- 写入配置文件 ----
        config_path = os.path.join(wd, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        log.info("[%s] 配置已写入 %s", job.job_id, config_path)
        return wd

    def start(self, job: Job, slot_idx: int = 0) -> object:
        """拉起黑盒进程。

        slot_idx 用于端口分配等场景，避免同机多实例冲突。
        返回值是不透明 handle——executor 不关心内部结构。

        注意：
        - stdout 必须 PIPE（new_output 需要读取增量输出）
        - stderr 合并到 stdout（方便统一解析）
        - cwd 设为工作目录（目标程序可能读取相对路径配置）
        """
        wd = self.workdir(job)
        config_path = os.path.join(wd, "config.json")

        # ---- TODO: 构建你的命令行 ----
        # 替换为你的黑盒程序的实际命令
        cmd = [
            self.binary_path,
            "--config", config_path,
            # TODO: 添加其他命令行参数
        ]

        # ---- 拉起进程 ----
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # 合并 stderr 到 stdout
            text=True,
            cwd=wd,
        )
        log.info("[%s] 进程已启动 pid=%d slot=%d", job.job_id, proc.pid, slot_idx)
        return _Handle(proc, job)

    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        """轮询进程状态。

        三种返回值：
        - (RUNNING, None): 进程还在跑
        - (DONE, AttemptResult): 进程结束，返回结构化结果

        注意：
        - 必须在 poll 中读取 stdout，防止 pipe 缓冲满导致进程阻塞
        - DONE 时必须返回 AttemptResult，不能返回 None
        - 使用 parse_target_output() 自动解析 stdout
        """
        h: _Handle = handle
        proc = h.proc

        # ---- 读取增量 stdout（防止 pipe 缓冲满） ----
        if proc.stdout:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                h.stdout_lines.append(line.rstrip("\n"))

        # ---- 检查进程是否结束 ----
        retcode = proc.poll()
        if retcode is None:
            return RunState.RUNNING, None

        # ---- 进程结束，解析结果 ----
        full_text = "\n".join(h.stdout_lines)
        result = parse_target_output(full_text, retcode, h.job)
        return RunState.DONE, result

    def cancel(self, handle: object, graceful: bool = True) -> None:
        """取消进程。

        必须用 kill_process_tree() 而非 proc.terminate()，防止孤儿进程。
        许多打包工具（如 PyInstaller onefile）会 fork 子进程，
        普通 terminate() 只杀父进程，子进程继续运行可能导致重复下单。
        """
        h: _Handle = handle
        kill_process_tree([h.proc.pid], graceful=graceful)

    def cleanup(self, handle: object) -> None:
        """确保进程已终止，释放资源。

        可选实现。如果 cancel() 已经杀掉进程，这里可以只做文件清理等。
        """
        h: _Handle = handle
        try:
            h.proc.kill()
        except OSError:
            pass   # 进程已终止，忽略

    def new_output(self, handle: object) -> list:
        """返回增量 stdout 行。

        维护游标，只返回自上次调用以来的新行。
        用于实时日志推送到前端。
        """
        h: _Handle = handle
        new = h.stdout_lines[h.out_cursor:]
        h.out_cursor = len(h.stdout_lines)
        return new
