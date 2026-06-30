# ticket-dispatcher API 连接指南

## 基础信息

| 项目 | 值 |
|------|------|
| 默认地址 | `http://<服务器IP>:8000` |
| 认证 | 设 `API_KEY` 环境变量后启用，写操作请求头加 `X-API-Key: <key>` |
| 数据格式 | JSON（`Content-Type: application/json`） |
| 实时事件 | WebSocket `ws://<addr>/ws`（连接时回放最近日志 + 断开检测）或 HTTP `GET /events` |

---

## 一、Worker 管理

### 查看所有 Worker

```bash
curl http://localhost:8000/workers
```

返回配置声明的 + 心跳上报的全部 worker。`status` 字段：`idle`（空闲）、`busy`（在执行）、`offline`（心跳超时）、`paused`（暂停接单）。

### 创建 Worker（声明式）

```bash
curl -X POST http://localhost:8000/workers \
  -H 'Content-Type: application/json' \
  -d '{"worker_id":"worker_2","program":"my_driver","max_slots":1}'
```

写入 `settings.yaml` 占位。返回该 worker 的启动命令 hint。**不远程拉进程**——需要在目标机手动启动：
```bash
WORKER_ID=worker_2 WORKER_PROGRAM=my_driver python main.py worker
```

自注册 worker（不在 settings.yaml 里）直接心跳上报也会出现在 `/workers`。

### 删除 Worker

```bash
curl -X DELETE http://localhost:8000/workers/worker_2
```

移除 settings.yaml 声明 + 清心跳僵尸。**不杀远端进程**。

### 查看单个 Worker

```bash
curl http://localhost:8000/workers/worker_1
```

---

## 二、账号管理

### 查看号池

```bash
curl http://localhost:8000/accounts
```

返回所有账号（**不含 cookie**，只有 `has_cookie` 布尔 + 基本信息 + 购票人列表）。

### 添加/更新账号

```bash
curl -X POST http://localhost:8000/accounts/save \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"my_account_1","program":"my_driver","uid":12345,
       "username":"用户A","cookie":{"token":"xxx"},"buyers":[]}'
```

cookie 的格式由你的 Driver 和 Converter 决定——框架只做存储和透传。

### 删除账号

```bash
curl -X DELETE http://localhost:8000/accounts/my_account_1
```

---

## 三、平台辅助 API（Bilibili 扩展）

框架通过 `src/platform/` 目录支持平台特定的 API 扩展。当前内置 Bilibili 扩展，挂载在 `/bilibili/` 前缀下。

### 扫码登录

```bash
# 1. 生成二维码
curl -X POST http://localhost:8000/bilibili/qr/gen
# → {"url": "https://passport.bilibili.com/...", "qrcode_key": "..."}

# 2. 轮询扫码状态（每秒一次）
curl -X POST http://localhost:8000/bilibili/qr/poll \
  -H 'Content-Type: application/json' \
  -d '{"qrcode_key":"..."}'
# → 成功时返回 SESSDATA + bili_jct + uid

# 3. 保存到号池
curl -X POST http://localhost:8000/accounts/save \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"bili_12345","uid":12345,"username":"用户A",
       "cookie":{"sess_data":"...","bili_jct":"..."}}'
```

### 查询活动详情

```bash
curl -X POST http://localhost:8000/bilibili/event/info \
  -H 'Content-Type: application/json' \
  -d '{"project_id": 1001742}'
```

### 查询库存状态

```bash
curl -X POST http://localhost:8000/bilibili/event/stock \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1001742}'
```

### 查询购票人列表

```bash
curl -X POST http://localhost:8000/bilibili/buyer/list \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"bili_12345"}'
```

### 刷新账号购票人 + 大会员状态

```bash
curl -X POST http://localhost:8000/bilibili/account/refresh \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"bili_12345"}'
```

### 添加自己的平台扩展

参考 `src/platform/bilibili.py`，实现你的平台 API 并通过 `register_api_router()` 注册：

```python
from fastapi import APIRouter
from src.api import register_api_router

router = APIRouter(prefix="/myplatform", tags=["myplatform"])

@router.post("/login")
async def login(): ...

register_api_router(router)
```

---

## 四、指派任务（核心）

### 指派一张任务

```bash
curl -X POST http://localhost:8000/workers/worker_1/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": "my_account_1",
    "buyer_ids": [1001],
    "project_id": 12345,
    "screen_id": 100,
    "sku_id": 200,
    "pay_money": 8800,
    "deadline": "2026-07-11T10:00:00+08:00",
    "label": "示例活动"
  }'
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `account_id` | ✅ | 号池里的账号 ID |
| `project_id` | ✅ | 活动 ID |
| `screen_id` | ✅ | 场次 ID |
| `sku_id` | ✅ | 票档 ID |
| `pay_money` | ✅ | 单张票金额（分），如 ¥88 = 8800 |
| `deadline` | ⬜ | 开售时间，ISO-8601 或 epoch。留空/0 = 立即执行 |
| `buyer_ids` | ⬜ | 指定购票人 ID 列表。留空 = 用该账号全部购票人 |
| `label` | ⬜ | 活动标签（前端显示） |
| `program` | ⬜ | 覆盖 Driver 类型。通常不用填，自动从 worker 心跳取 |
| `retry_interval` | ⬜ | 下单重试间隔秒数（默认 0.3） |
| `check_interval` | ⬜ | 库存检查间隔秒数（默认 3.0） |

返回：
```json
{"status": "ok", "job_id": "job-1781894004361", "worker_id": "worker_1", "buyers": 1}
```

### program 优先级

1. body 显式 `program`
2. 该 worker 心跳实际上报的 `program`（自注册 worker 的唯一真相）
3. settings.yaml 声明
4. 账号默认

---

## 四、控制命令

### 停止当前任务

```bash
curl -X POST http://localhost:8000/workers/worker_1/stop
```

发送 cancel 信号 → 终止在飞 Job → worker 回 IDLE。

### 暂停接单

```bash
curl -X POST http://localhost:8000/workers/worker_1/pause
```

停止消费**新** Job（在飞 Job 继续跑完，进程不退）。用于异常处理时临时摘下 worker。

### 恢复接单

```bash
curl -X POST http://localhost:8000/workers/worker_1/resume
```

---

## 五、实时监控

### 事件流

```bash
curl -N http://localhost:8000/events
```

返回 JSON 数组，包含最近事件。事件类型：

| type | 说明 |
|------|------|
| `worker_log` | 黑盒实时 stdout（增量行，每 tick 最多 40 行） |
| `worker_error` | 失败详情（reason + detail） |
| `job_dispatched` | Job 被指派 |
| `job_result` | Job 完成（success/order_id/reason） |
| `worker_registered` | Worker 上线 |
| `worker_offline` | Worker 离线 |

### WebSocket 实时推送

```javascript
const ws = new WebSocket('ws://<addr>/ws');
ws.onmessage = (e) => {
  const data = JSON.parse(e.data);
  if (data.type === 'worker_log') {
    console.log(`[${data.worker_id}] ${data.lines.join('\n')}`);
  }
};
```

**行为说明**：
- **连接时回放**：自动回放最近 400 条 `worker_log` / `worker_error` 事件。
- **增量推送**：只推连接后产生的新事件（按单调序号 `seq`）。
- **断开检测**：客户端断开后服务端立即释放资源。

### 状态概览

```bash
curl http://localhost:8000/status
```

### 指标

```bash
curl http://localhost:8000/metrics
```

### 历史结果

```bash
curl http://localhost:8000/history/results
```

---

## 六、注意事项

- **`pay_money` 是分**：¥88 → `8800`，¥280 → `28000`
- **`deadline` 是开售时间**：Driver 的目标进程通常会倒计时等到点才执行。留空 = 立即执行
- **无防重**：同一个 account_id 可以同时派给多个 worker（用户自负）
- **cookie 会过期**：需要定期更新。过期后目标进程可能失败
- **program**：必须与 Driver 注册时的名称一致
