# ticket-dispatcher

**通用票务调度框架（主要适配b站会员购） — 黑盒进程的手动指派台 + 分布式 Worker fleet。**

## 背景

此为本人今年以bw为契机耗时一周vibe出来的框架（其中一开二开当天都直接通宵改代码了）最终也是如愿以偿今年三天普票全勤，顺便也帮身边所有朋友都抢到了票。没事干了就直接单开一个仓库把这个框架分享给大家（当然最敏感的driver和convertor部分我得捂住，大伙各凭本事自己vibe）听说最近三开还耍猴延期了，不过本人没事干就看戏ing。

一开始是去年某四字软件的群友教沙盒群控部署，的确管用但还是有些许问题。最主要的痛点还是在于配置不灵活，不利于总控可视化管理和把握。于是本人索性把「调度」这部分抽出来做成了一个独立的系统。

最终的形态就是这样：**你当大脑，它当手脚。** 你决定用哪个账号、哪几个购买人、抢哪场、用哪个执行脚本、用哪个worker开抢；它负责把任务分发到被选中的worker、拉起脚本执行、实时汇报状态、抢到了通知你去付款。

## 为什么不自研抢票脚本

说实话，当前 B 站抢票的脚本生态已经很成熟了——开源的、闭源的、各种语言实现的，该踩的坑前人都踩过了，token 算法、风控绕过、验证码识别这些核心能力也不是说搞不出来，你可以参考开源的代码、根据b站的接口逆向，甚至可以逆向闭源的脚本......但归根结底大多还是在重复造轮子，很难作出更进一步的突破。我自己的实战经验也证明，用现有的成熟脚本配合一个靠谱的调度层，效果远好于从零造轮子再花时间验证。

所以这个项目**只做调度，不做执行**。具体跑什么脚本、怎么跑，全由你通过 **Driver** 接口自行决定。框架不绑定任何特定的抢票程序，也不提供任何现成的驱动实现。

## 这个项目包含什么

- ✅ **调度框架**：任务指派、Worker 生命周期管理、并发控制
- ✅ **Bus 抽象**：Redis Streams（生产）/ In-memory（开发），一行配置切换
- ✅ **实时监控**：WebSocket 事件推送、Worker 心跳、日志流
- ✅ **号池管理**：账号 CRUD、购票人管理
- ✅ **推送通知**：Server酱，抢到/失败即时推送
- ✅ **Vue 3 前端**：Worker 控制台 + 号池 + 事件流 + 审计面板
- ✅ **完整测试**：72 个测试用例，CI 可跑
- ✅ **部署方案**：Docker Compose + systemd 托管，含一键初始化脚本

## 这个项目不包含什么

- ❌ 任何抢票脚本 / Bot / 自动化程序
- ❌ 任何平台特定的 Driver 实现（登录、下单、风控绕过等）
- ❌ 任何平台 API 的调用代码
- ❌ 任何 token 签名 / 加密 / 反爬逻辑

这些都需要你根据自己的目标程序自行实现。框架提供了清晰的接口和示例骨架，详见下面的「如何接入目标脚本」。

当然此仓库只是是对原仓库进行了简单的移植以及敏感代码和文档的删减替换，并没有实战测试过。框架本身如果存在任何因移植和删减替换而产生的bug本人只能私密马赛了。不过如果driver都会写这些也就顺手改一下的事吧。

## 免责声明

本项目仅提供**任务调度框架**，使用者应自行确保其接入的脚本和目标程序符合相关平台的服务条款和法律法规。本项目作者不对使用者的任何行为承担责任。

## 快速开始

### 单进程开发模式（无需 Redis）

```bash
pip install -r requirements.txt
DRYRUN=1 python main.py api    # DryRunDriver 模拟执行，跑通主干
```

### 端到端验证

```bash
# 1) 起 API（InMemoryBus + DryRunDriver）
DRYRUN=1 python main.py api

# 2) 另一个终端：指派任务
curl -X POST http://localhost:8000/workers/worker_local/assign \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"example_12345","project_id":999,"screen_id":888,
       "sku_id":777,"pay_money":100,"deadline":""}'

# 3) 查看结果
curl http://localhost:8000/workers
curl http://localhost:8000/history/results
```

### 生产部署

```bash
docker-compose up -d
```

## 如何接入目标脚本

核心就两件事：实现 **Driver**（拉起目标脚本）和可选的 **Converter**（生成脚本配置）。

### 1. 实现 Driver

```python
from src.driver import Driver, RunState, register_driver
from src.driver._parse import parse_target_output

class MyDriver(Driver):
    program = "my_script"

    def prepare(self, job):
        """把 Job 翻译成目标脚本需要的配置文件，写到工作目录。"""
        wd = self.workdir(job)
        # 写配置到 wd/config.json ...
        return wd

    def start(self, job, slot_idx=0):
        """拉起目标脚本进程，返回一个 handle（可以是 Popen、pid、或任何对象）。"""
        import subprocess
        proc = subprocess.Popen(["python", "my_script.py", ...])
        return {"proc": proc, "job": job, "stdout": []}

    def poll(self, handle):
        """轮询进程状态。返回 (RunState, AttemptResult 或 None)。"""
        proc = handle["proc"]
        if proc.poll() is None:
            return RunState.RUNNING, None
        # 进程结束，解析输出
        stdout = "\n".join(handle["stdout"])
        result = parse_target_output(stdout, proc.returncode, handle["job"])
        return RunState.DONE, result

    def cancel(self, handle, graceful=True):
        """取消：杀掉进程。"""
        from src.driver._proctree import kill_process_tree
        kill_process_tree([handle["proc"].pid], graceful=graceful)

    def new_output(self, handle):
        """返回增量 stdout 行（供实时日志面板显示）。"""
        # 从 pipe 读新行...
        return []

register_driver("my_script", MyDriver)
```

### 2. 可选：实现 Converter

如果目标脚本需要配置文件，可以让 Converter 自动生成：

```python
from src.converters import register_converter

class MyConverter:
    def convert(self, job, output_dir: str) -> None:
        import json, os
        config = {
            "project_id": job.project_id,
            "cookie": job.account.get("cookie", {}),
            "buyers": [b["id"] for b in job.buyers],
        }
        with open(os.path.join(output_dir, "config.json"), "w") as f:
            json.dump(config, f)

register_converter("my_script", MyConverter())
```

### 3. 注册后使用

在你的入口文件里 import 注册代码，然后正常启动 worker：

```bash
WORKER_ID=worker_1 WORKER_PROGRAM=my_script python main.py worker
```

更详细的说明见：
- [docs/driver_development.md](docs/driver_development.md) — Driver 开发完整指南
- [docs/converter_development.md](docs/converter_development.md) — Converter 开发指南
- [src/driver/example.py](src/driver/example.py) — 示例 Driver 骨架（带详细注释）
- [src/converters/example.py](src/converters/example.py) — 示例 Converter 骨架

## 架构

两个角色通过 **Bus**（Redis 或 In-memory）通信。**没有调度循环**——指派是 HTTP 请求直接 push job，后台只有一个被动 Collector 线程 drain 结果落库。

```
 前端(号池 + Worker 控制台)      API 节点 (main.py api)              Bus          Worker 节点 (main.py worker)
  │--①配好的任务+选worker------->│--push_job(定向 worker_id)--------->│--pop_job--->│ driver.prepare/start 黑盒
  │--②stop---------------------->|--signal(cancel)------------------->│--drain----->│ executor.cancel
  │<--③实时状态(心跳+last_result)-│  Collector 线程: pop_result→store  │<--push------│ 完成/失败/停 → 自动回 IDLE
  └──────────────────────────────│<------heartbeat / result-----------│             │
                                  ↕ Bus (Redis Streams / InMemory)
```

## 命令

```bash
python main.py api                # API 节点（FastAPI + Collector，默认 0.0.0.0:8000）
python main.py worker             # Worker agent 节点
python main.py status             # 查看号池 + worker 心跳
DRYRUN=1 python main.py api       # DryRunDriver 模拟模式
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WORKER_ID` | `worker_local` | Worker agent 的 ID |
| `WORKER_PROGRAM` | (查 settings.yaml) | Worker 跑哪种 Driver |
| `MAX_SLOTS` | `1` | Worker agent 最大并发 slot 数 |
| `REDIS_HOST` | (空) | Redis 地址，未设则用 InMemoryBus；设了却连不上**直接崩** |
| `REDIS_PORT` | `6379` | Redis 端口 |
| `REDIS_PASSWORD` | (空) | Redis 密码。需 Redis ≥5.0（队列用 Streams） |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API 绑定地址/端口 |
| `DRYRUN` | (空) | 设为 `1` 强制 DryRunDriver |
| `API_KEY` | (空) | 设置后启用鉴权 |

## API 端点

### 核心框架

| 端点 | 用途 |
|------|------|
| `GET /workers` | 所有 worker 实时状态 |
| `POST /workers` | 创建（声明）worker |
| `DELETE /workers/{id}` | 删除 worker |
| `POST /workers/{id}/assign` | 指派任务 |
| `POST /workers/{id}/stop` | 停止当前任务 |
| `POST /workers/{id}/pause` / `/resume` | 暂停/恢复接单 |
| `GET /accounts` | 号池 |
| `POST /accounts/save` | 添加/更新账号 |
| `DELETE /accounts/{id}` | 删除账号 |
| `GET /status` | 聚合状态 |
| `GET /metrics` | 全局指标 |
| `GET /history/results` | 结果历史 |
| `GET /events` / `WS /ws` | 实时事件流 |

### Bilibili 平台扩展（`src/platform/bilibili.py`）

| 端点 | 用途 |
|------|------|
| `POST /bilibili/qr/gen` | 生成扫码登录二维码 |
| `POST /bilibili/qr/poll` | 轮询扫码状态 |
| `POST /bilibili/check/login` | 检查登录状态 |
| `POST /bilibili/event/info` | 查询活动详情（场次 + 票档） |
| `POST /bilibili/event/stock` | 查询库存状态 |
| `POST /bilibili/buyer/list` | 查询购票人列表 |
| `POST /bilibili/account/refresh` | 刷新购票人 + 大会员状态 |

这些是**平台 API 辅助端点**，不是抢票执行逻辑。抢票执行在 Driver 里。
你可以参考 `src/platform/bilibili.py` 为其他平台实现类似的扩展。

详见 [docs/api_guide.md](docs/api_guide.md)。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

| 层 | 文件 | 内容 |
|---|---|---|
| 指派链路 | `test_assign.py` | assign→消费→result 回流→回 IDLE |
| Driver 契约 | `test_driver_contract.py` | prepare/状态单调/cancel 幂等/故障注入 |
| 解析器 | `test_parser.py` | 离线测试（JSON/关键字/退出码/边界） |
| Store | `test_store.py` | SQLite 持久化 + 老库迁移 |
| RedisBus | `test_redis_bus.py` | fakeredis 消费者组 + PEL 崩溃恢复 |
| Worker CRUD | `test_worker_crud.py` | 声明写盘往返 + 删除幂等 |
| 进程清理 | `test_proc_cleanup.py` | `kill_process_tree` 连根杀后代 |
| 推送 | `test_notify.py` | 推送渠道发送/开关门控 |

## 文件结构

```
ticket-dispatcher/
├── main.py                    # 入口（api / worker / status）
├── src/
│   ├── api.py                 # FastAPI 端点
│   ├── bus.py                 # Bus 抽象 + InMemoryBus
│   ├── queue.py               # RedisBus（Streams 消费者组）
│   ├── worker_agent.py        # Worker 主循环
│   ├── executor.py            # 并发进程管理
│   ├── models.py              # Job / AttemptResult / ControlSignal
│   ├── driver/                # Driver ABC + DryRun + 示例 + 工具
│   ├── converters/            # Converter 注册 + 示例
│   ├── collector.py           # 后台结果收集
│   ├── store.py               # SQLite 持久化
│   ├── notify.py              # 推送通知
│   ├── config.py              # 配置加载
│   ├── clock.py               # 时钟抽象
│   └── timeutil.py            # 时间工具
├── dashboard/                 # Vue 3 前端
├── deploy/                    # systemd 托管脚本
├── docs/                      # 文档
├── tests/                     # 测试
├── docker-compose.yml         # Docker 部署
└── config/                    # 配置（accounts/settings 不提交）
```

## License

MIT
