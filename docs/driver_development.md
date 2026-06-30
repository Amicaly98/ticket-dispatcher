# Driver 开发指南

Driver 是 ticket-scheduler 与外部黑盒进程之间的解耦边界。每个 Driver 把一个外部程序的
生命周期封装成统一接口：配置生成 -> 进程拉起 -> 状态轮询 -> 取消 -> 清理。

## 目录

- [Driver ABC 接口](#driver-abc-接口)
- [生命周期：prepare -> start -> poll -> cancel -> cleanup -> new_output](#生命周期)
- [RunState 枚举](#runstate-枚举)
- [注册 Driver](#注册-driver)
- [完整示例](#完整示例)
- [工具模块](#工具模块)
- [最佳实践](#最佳实践)

---

## Driver ABC 接口

所有 Driver 继承 `src.driver.base.Driver` 抽象基类：

```python
from src.driver.base import Driver, RunState
from src.models import AttemptResult, Job

class Driver(abc.ABC):
    program: str = ""          # 子类必须设置，全局唯一标识符

    def workdir(self, job: Job) -> str:
        """返回 per-job 工作目录（自动创建）。"""

    @abc.abstractmethod
    def prepare(self, job: Job) -> str:
        """把 Job 翻译成目标程序原生配置，写入工作目录，返回该目录路径。"""

    @abc.abstractmethod
    def start(self, job: Job, slot_idx: int = 0) -> object:
        """拉起黑盒进程，返回不透明 handle。slot_idx 用于端口分配等。"""

    @abc.abstractmethod
    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        """轮询：RUNNING -> result=None；DONE -> 返回 AttemptResult。"""

    @abc.abstractmethod
    def cancel(self, handle: object, graceful: bool = True) -> None:
        """取消：graceful=True 不打断在飞请求；False 强杀。"""

    def cleanup(self, handle: object) -> None:
        """回收 handle 资源（可选，默认空操作）。"""

    def new_output(self, handle: object) -> list:
        """返回自上次调用以来新产生的 stdout 行（增量，默认空列表）。"""
```

**关键设计点**：
- `program` 是 class 属性，不是实例属性。同一个 Driver 类的 `program` 值全局唯一。
- `start()` 返回的 handle 是不透明对象——executor 不关心其内部结构，只把它传给 `poll`/`cancel`/`cleanup`/`new_output`。
- Driver 实例无状态地服务多个 Job；每个 `start()` 返回独立的 handle。

---

## 生命周期

```
Job 到达
  |
  v
prepare(job)            把 Job 翻译成目标程序的配置文件，写入 workdir
  |
  v
handle = start(job)     拉起黑盒进程，返回 handle
  |
  v
loop:
  state, result = poll(handle)    轮询进程状态
  if state == DONE:
    break
  lines = new_output(handle)      可选：获取增量 stdout
  |
  v
cleanup(handle)         回收资源（可选）
  |
  v
result -> Bus           AttemptResult 回流到 API 节点
```

### prepare(job) -> str

把 Job 转换成目标程序能理解的配置。典型操作：
- 创建 per-job 工作目录（用 `self.workdir(job)`）
- 写出配置文件（YAML/JSON/INI/环境变量）
- 返回工作目录路径

### start(job, slot_idx) -> handle

拉起黑盒进程。`slot_idx` 是 executor 分配的 slot 序号（0-based），用于：
- 端口分配（避免多 slot 端口冲突）
- 日志文件命名
- 任何需要区分同机多实例的场景

返回值是不透明 handle，通常是 subprocess.Popen 或自定义包装对象。

### poll(handle) -> (RunState, AttemptResult | None)

轮询进程状态。返回值有三种情况：
- `(RUNNING, None)` —— 进程还在跑
- `(DONE, AttemptResult(...))` —— 进程结束，返回结构化结果

**注意**：DONE 时必须返回 AttemptResult，不能返回 None。

### cancel(handle, graceful=True)

取消正在运行的进程。
- `graceful=True`：发送 SIGTERM，等待进程自行收尾（不打断在飞请求）
- `graceful=False`：直接 SIGKILL 强杀

使用 `kill_process_tree()` 确保杀掉整棵进程树（见下方工具模块）。

### cleanup(handle)

回收 handle 关联的资源（临时文件、端口等）。默认空操作，按需实现。

### new_output(handle) -> list

返回自上次调用以来新产生的 stdout 行。用于实时日志推送到前端。
实现时维护一个游标，只返回增量部分。默认返回空列表。

---

## RunState 枚举

```python
class RunState(str, Enum):
    RUNNING = "running"
    DONE = "done"
```

只有两个状态。executor 通过 `poll()` 的返回值判断是否结束。

---

## 注册 Driver

### 方式一：在 `src/driver/__init__.py` 中注册

编辑 `src/driver/__init__.py`：

```python
from .my_driver import MyDriver

_DRIVERS: dict[str, Type[Driver]] = {
    "dryrun": DryRunDriver,
    "my_program": MyDriver,   # 新增
}
```

### 方式二：运行时注册（推荐用于插件式扩展）

在 Driver 模块的 `__init__.py` 或启动脚本中：

```python
from src.driver import register_driver
from .my_driver import MyDriver

register_driver("my_program", MyDriver)
```

`register_driver(program, cls)` 接受：
- `program`：字符串标识符，全局唯一，后注册覆盖先注册
- `cls`：Driver 子类（不是实例）

### 使用 Driver

```python
from src.driver import get_driver

# 根据 program 名称获取实例
driver = get_driver("my_program")

# DRYRUN=1 环境变量或 force_dryrun=True 时强制返回 DryRunDriver
driver = get_driver("my_program", force_dryrun=True)
```

---

## 完整示例

以下是一个模拟 Driver 的完整实现，用于演示所有接口：

```python
"""示例 Driver：模拟一个外部购票程序。

实际使用时替换为真实的目标程序交互逻辑。
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
from typing import Optional

from src.driver.base import Driver, RunState
from src.driver._parse import parse_target_output
from src.driver._proctree import kill_process_tree
from src.models import AttemptResult, Job

log = logging.getLogger("driver.example")


class _Handle:
    """封装进程状态。poll/cancel/cleanup 都通过这个对象操作。"""
    def __init__(self, proc: subprocess.Popen, job: Job):
        self.proc = proc
        self.job = job
        self.stdout_lines: list[str] = []
        self.out_cursor: int = 0


class ExampleDriver(Driver):
    program = "example"   # 全局唯一标识符

    def prepare(self, job: Job) -> str:
        """把 Job 翻译成目标程序的配置文件。"""
        wd = self.workdir(job)
        config = {
            "project_id": job.project_id,
            "screen_id": job.screen_id,
            "sku_id": job.sku_id,
            "count": job.count,
            "deadline": job.deadline,
            "cookie": job.account.get("cookie", {}),
            "buyer_ids": job.buyer_ids,
        }
        config_path = os.path.join(wd, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        log.info("[%s] 配置已写入 %s", job.job_id, config_path)
        return wd

    def start(self, job: Job, slot_idx: int = 0) -> object:
        """拉起黑盒进程。"""
        wd = self.workdir(job)
        config_path = os.path.join(wd, "config.json")
        log_path = os.path.join(wd, "output.log")

        # 实际使用时替换为你的黑盒程序路径
        cmd = ["your_bot_binary", "--config", config_path]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=wd,
        )
        log.info("[%s] 进程已启动 pid=%d slot=%d", job.job_id, proc.pid, slot_idx)
        return _Handle(proc, job)

    def poll(self, handle: object) -> tuple[RunState, Optional[AttemptResult]]:
        """轮询进程状态。"""
        h: _Handle = handle
        proc = h.proc

        # 读取增量 stdout
        if proc.stdout:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                h.stdout_lines.append(line.rstrip("\n"))

        retcode = proc.poll()
        if retcode is None:
            return RunState.RUNNING, None

        # 进程结束，解析结果
        full_text = "\n".join(h.stdout_lines)
        result = parse_target_output(full_text, retcode, h.job)
        return RunState.DONE, result

    def cancel(self, handle: object, graceful: bool = True) -> None:
        """取消进程：连根拔起整棵进程树。"""
        h: _Handle = handle
        kill_process_tree([h.proc.pid], graceful=graceful)

    def cleanup(self, handle: object) -> None:
        """确保进程已终止，释放资源。"""
        h: _Handle = handle
        try:
            h.proc.kill()
        except OSError:
            pass

    def new_output(self, handle: object) -> list:
        """返回增量 stdout 行。"""
        h: _Handle = handle
        new = h.stdout_lines[h.out_cursor:]
        h.out_cursor = len(h.stdout_lines)
        return new
```

### 注册这个 Driver

在你的包的 `__init__.py` 中：

```python
from src.driver import register_driver
from .example import ExampleDriver

register_driver("example", ExampleDriver)
```

然后设置 `WORKER_PROGRAM=example` 环境变量即可。

---

## 工具模块

### `_parse.py` —— stdout 解析器

`parse_target_output()` 是共享的 stdout 解析函数，内置多种检测模式：

```python
from src.driver._parse import parse_target_output, SUCCESS_KEYWORDS, add_success_keywords

result = parse_target_output(
    text=stdout_text,       # 完整 stdout 文本
    retcode=exit_code,      # 进程退出码
    job=job,                # 对应的 Job 对象
    extra_reason="",        # 可选：额外原因标记
)
```

**解析优先级**（从高到低）：
1. JSON 中找 `orderId` 字段
2. 纯文本找 `订单号：XXXXXXXXXX`
3. JSON 中找 `errno=0`
4. 成功关键字匹配（`SUCCESS_KEYWORDS` 列表）
5. 已知错误关键字 -> 友好短码（如 "库存不足" -> "sold_out"）
6. 退出码兜底（0 -> "no_result"，非 0 -> "exit_N"）

**自定义成功关键字**：

```python
from src.driver._parse import add_success_keywords

# 在 Driver 初始化时追加项目特定的成功关键字
add_success_keywords("抢票成功", "ticket_acquired")
```

**已知错误关键字映射**（内置）：

| 关键字 | reason 短码 |
|--------|-------------|
| 未登录 / 登录失效 | `not_logged_in` |
| 库存不足 / 已售罄 / 无票 | `sold_out` |
| 风控 | `risk_control` |
| 验证码 | `captcha` |
| 频繁 | `rate_limited` |
| 项目不可售 / 未开售 | `not_on_sale` |

### `_proctree.py` —— 进程树杀手

```python
from src.driver._proctree import kill_process_tree

# 优雅终止：先 SIGTERM，等 1.5 秒，还活着就 SIGKILL
kill_process_tree([pid1, pid2], graceful=True, grace=1.5)

# 强杀：直接 SIGKILL
kill_process_tree([pid], graceful=False)
```

**为什么需要进程树杀手**：许多打包工具（如 PyInstaller onefile）启动时父 bootloader 会 fork
出真正跑业务的子进程。普通的 `terminate()` 只杀父进程，子进程被 reparent 到 init 继续运行，
可能导致重复下单等意料之外的行为。`kill_process_tree()` 在 Linux 上通过 `/proc` 枚举全部
后代，整棵一起杀。

---

## 最佳实践

### 1. handle 设计

- handle 应包含进程对象、Job 引用、stdout 缓冲区和游标。
- 不要在 Driver 实例上存储 per-job 状态——多个 Job 共享同一个 Driver 实例。

### 2. 取消处理

- `cancel()` 必须用 `kill_process_tree()` 而非 `proc.terminate()`，防止孤儿进程。
- `graceful=True` 时不打断在飞请求，给进程自行收尾的机会。

### 3. 输出流式

- `new_output()` 维护游标，只返回增量行，避免重复推送。
- `poll()` 中也要读取 stdout（防止 pipe 缓冲满导致进程阻塞）。

### 4. 错误报告

- 失败时必须返回 `AttemptResult(success=False, reason="...", detail="...")`。
- `detail` 带 stdout 尾巴 + 退出码，限长 ~4KB，方便前端诊断。
- `reason` 用 `parse_target_output()` 自动生成的短码，便于前端分类展示。

### 5. 配置生成

- `prepare()` 必须幂等——多次调用结果相同。
- 用 `self.workdir(job)` 获取 per-job 工作目录，不要硬编码路径。
- cookie 等敏感数据从 `job.account` 读取，不要落盘到日志。

### 6. 多 slot

- `start()` 的 `slot_idx` 参数用于端口分配等场景，避免同机多实例冲突。
- 每个 slot 的 handle 独立，互不干扰。

### 7. 测试

- 继承 `DryRunDriver` 或写 Scripted Driver 做端到端测试，不依赖真实黑盒。
- 用 `parse_target_output()` 离线测试各种 stdout 样本的解析结果。
