# Converter 开发指南

Converter 负责把 Job 翻译成目标程序的原生配置文件。它与 Driver 配合使用：
Driver 的 `prepare()` 方法调用 Converter 生成配置，然后 `start()` 拉起进程。

## 目录

- [Converter 协议](#converter-协议)
- [注册 Converter](#注册-converter)
- [完整示例](#完整示例)
- [DryRunDriver 如何使用 Converter](#dryrundriver-如何使用-converter)
- [最佳实践](#最佳实践)

---

## Converter 协议

Converter 使用 Python 的 `Protocol` 机制，不需要继承基类，只需实现 `convert` 方法：

```python
from src.converters import Converter

class MyConverter:
    def convert(self, job, output_dir: str) -> None:
        """把 Job 翻译成目标程序的配置文件，写入 output_dir。"""
        ...
```

**参数**：
- `job`：`Job` 对象（pydantic 模型），包含所有任务信息
  - `job.account`：账号快照（含 cookie、uid、proxy 等）
  - `job.buyers`：购票人列表
  - `job.project_id` / `screen_id` / `sku_id`：活动/场次/票档
  - `job.deadline`：开售时间戳
  - `job.count`：购买数量
  - `job.pay_money`：支付金额（分）
- `output_dir`：配置文件输出目录（已存在）

**返回值**：None。Converter 只负责写文件，不返回值。

---

## 注册 Converter

### 方式一：在 `src/converters/__init__.py` 中注册

编辑 `src/converters/__init__.py`：

```python
from .my_converter import MyConverter

_CONVERTERS: dict[str, Converter] = {
    "my_program": MyConverter(),
}
```

### 方式二：运行时注册（推荐）

```python
from src.converters import register_converter
from .my_converter import MyConverter

register_converter("my_program", MyConverter())
```

`register_converter(program, converter)` 接受：
- `program`：字符串标识符，全局唯一，与 Driver 的 `program` 对应
- `converter`：Converter 实例（不是类）

### 获取 Converter

```python
from src.converters import get_converter

converter = get_converter("my_program")
if converter:
    converter.convert(job, output_dir)
```

---

## 完整示例

以下是一个 Converter 示例，把 Job 转换成 JSON 配置文件：

```python
"""示例 Converter：生成 JSON 配置文件。"""
from __future__ import annotations
import json
import logging
import os

log = logging.getLogger("converter.example")


class ExampleConverter:
    """把 Job 翻译成目标程序的 JSON 配置。"""

    def convert(self, job, output_dir: str) -> None:
        """生成 config.json 到 output_dir。"""
        # 从 Job 提取账号信息
        account = job.account
        cookie = account.get("cookie", {})

        # 构建目标程序的配置格式
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

        # 写入配置文件
        config_path = os.path.join(output_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        log.info("[%s] 配置已生成 -> %s", job.job_id, config_path)
```

### 注册

```python
from src.converters import register_converter
from .example import ExampleConverter

register_converter("example", ExampleConverter())
```

---

## DryRunDriver 如何使用 Converter

`DryRunDriver` 在 `prepare()` 中自动查找并调用已注册的 Converter：

```python
# src/driver/dryrun.py 中的 prepare 方法
def prepare(self, job: Job) -> str:
    wd = self.workdir(job)
    try:
        from ..converters import get_converter
        converter = get_converter(job.program)
        if converter:
            converter.convert(job, wd)
            return wd
    except Exception:
        pass
    # 兜底：写出 Job 快照 JSON
    ...
```

**流程**：
1. `DryRunDriver.prepare()` 被调用
2. 查找 `job.program` 对应的 Converter
3. 如果找到，调用 `converter.convert(job, wd)` 生成配置
4. 如果没找到或出错，兜底写出 Job 快照 JSON

**这意味着**：即使在 DRYRUN 模式下，只要你注册了 Converter，DryRunDriver 也会用它生成配置。
这对于测试 Converter 逻辑非常有用。

---

## 最佳实践

### 1. 配置格式

- Converter 输出的配置格式必须与目标程序期望的格式一致。
- 常见格式：JSON、YAML、INI、环境变量文件、命令行参数。
- 可以同时生成多种格式（如 config.json + .env）。

### 2. 幂等性

- `convert()` 必须幂等——多次调用结果相同。
- 不要依赖外部状态（如上一次调用的结果）。

### 3. 错误处理

- Converter 不应抛出异常（Driver 的 `prepare()` 会捕获并兜底）。
- 如果缺少必要数据，记录警告日志并生成最小可用配置。

### 4. Cookie 处理

- Cookie 格式因平台而异。Converter 负责把 `job.account["cookie"]` 转换成目标程序期望的格式。
- 常见模式：
  - JSON 配置文件中的 `cookie` 字段
  - 环境变量中的 `SESSION_TOKEN=xxx`
  - 命令行参数 `--cookie "token=xxx"`
  - Netscape 格式的 cookie 文件

### 5. 与 Driver 配合

- Converter 和 Driver 通常成对实现，使用相同的 `program` 名称注册。
- Converter 只负责配置生成，不负责进程管理。
- Driver 的 `prepare()` 调用 Converter，`start()` 读取生成的配置并拉起进程。

### 6. 测试

- 可以单独测试 Converter：构造 Job 对象，调用 `convert()`，检查输出文件。
- 用 DryRunDriver 端到端测试：注册 Converter + 设置 DRYRUN=1，观察是否正确生成配置。
