# 全链路测试计划

> **范围**：分支 `integrate-worker-mgmt` vs `e3bdeb0`，含两批改动：
> **A. worker 自注册 + 心跳带 program** + 前端创建/删除 worker
> + Redis 连不上即崩 + 密码 + protocol=2 + systemd 托管（`deploy/`）。
> **B. 增强 worker 可视化 + 修停止 + 错误全链路捕获**：当前富信息块、实时日志（worker_log）、
> 失败 detail 全链路、修停止（cancel 回流 cancelled 结果）、converter 改零模板。

## 图例

| 标记 | 含义 |
|---|---|
| **可代测** | Claude 能在本机自动跑（后端逻辑、API 端点、pytest、build、脚本语法）——已跑过的标 done |
| **需自测** | 必须你来：涉及浏览器 UI 点击、真 Redis>=5 环境、Linux 云机、真实抢票 |
| **已知缺陷** | 当前代码就是这个行为，测了会「失败」，是预期 |

---

## 0. 前置：基线自动化测试 (done)

```bash
pytest tests/ -q
```
**已验证**：`114 passed + 5 skipped`。这是回归底座，任何改动后先过这条。

---

## 1. Worker 自注册 + 可视化（后端）

### 1.1 自注册：worker 不在 settings.yaml 也能上线 (done)
**已代测**（真 Redis 双进程，心跳链路）：起一个 `WORKER_ID=worker_cloud` 的独立 worker 进程
（settings.yaml 为空），`GET /workers` 立刻返回它，`status:idle`。

复现（InMemoryBus 单进程版，你也能跑）：
```bash
# settings.yaml 保持 workers: []
DRYRUN=1 API_PORT=8098 python main.py api &
curl -s localhost:8098/workers          # 期望 {}（无声明、无心跳）
# 注：InMemoryBus 模式下 worker 由 api 进程内代起，仅当 settings.yaml 有声明时才起；
# 真要看自注册，用 §5 的真 Redis 双进程路径。
```

### 1.2 心跳带 program -> 卡片显示「程序」 (done)
`GET /workers/{id}` 返回体含 `program` 字段。前端卡片新增「程序」行 -> 见 §2.4。

---

## 2. 前端创建/删除 Worker

### 2.1 创建端点 `POST /workers` (done)
**已代测**：空->创建返回 `hint`（启动命令）->`/workers` 显示离线占位（program/slots/IP 正确）->重名返回 **409**->
settings.yaml 正确写入。命令级复现：
```bash
curl -s -X POST localhost:8098/workers -H 'Content-Type: application/json' \
  -d '{"worker_id":"wk_test","program":"my_program","max_slots":2,"public_ip":"9.9.9.9"}'
# 期望 status:ok + hint 含 "WORKER_ID=wk_test ... python main.py worker"
```

### 2.2 删除端点 `DELETE /workers/{id}` (done)
**已代测**：删声明 worker->settings.yaml 回空；删不存在的「僵尸」->仍 200（幂等清心跳）。

### 2.3 单元测试 (done)
`pytest tests/test_worker_crud.py -q` -> **5 passed**。

### 2.4 前端 UI：创建弹窗 + 删除按钮 (需自测)
**需你在浏览器验**：
```bash
DRYRUN=1 python main.py api          # 后端 :8000
cd dashboard && npm run dev          # 前端 :3000
```
逐项核对：
- [ ] 控制制台右上「+ 创建 Worker」打开弹窗
- [ ] 填 id/program/slots/IP -> 点「创建」-> **弹出启动命令**（hint，等宽字体框）
- [ ] 创建后卡片出现，状态「离线」，「程序」行显示刚选的 program
- [ ] 重名创建 -> 弹窗显示「创建失败: 409」
- [ ] 卡片「删除」按钮：离线 worker 直接确认；**在线 worker 点删除时弹警告**「不会杀远端进程」
- [ ] 删除后卡片消失（settings.yaml 里的声明也没了）
- [ ] `npm run build` 通过 (done)

---

## 3. Redis 连不上即崩 + 密码 + protocol=2

### 3.1 连不上即崩（不再静默回退）(done)
**已代测**：`REDIS_HOST` 指向不可用 Redis -> API 进程在 startup **直接抛 ConnectionError 退出**。
```bash
REDIS_HOST=127.0.0.1 REDIS_PORT=6399 API_PORT=8097 python main.py api
# 期望：启动即报错退出，日志见 redis.exceptions.ConnectionError，不是 "回退 InMemoryBus"
```

### 3.2 protocol=2 兼容老 Redis (done)
**已代测**：本机 Redis 3.0 + redis-py 8.0，未钉 protocol 时报 `unknown command 'HELLO'`；钉 `protocol=2` 后 `ping` 通、心跳链路通。

### 3.3 密码 AUTH (需自测)
**需你在有 `requirepass` 的 Redis 上验**：
```bash
redis-server --requirepass testpass --port 6399 &
REDIS_HOST=127.0.0.1 REDIS_PORT=6399 REDIS_PASSWORD=testpass python main.py api
# 期望：正常启动（AUTH 成功）；故意填错密码 -> startup 崩
```

### 3.4 Redis 必须 >=5.0（队列用 Streams）
**已知硬限制**：Redis 3.x 跑 `assign` 会报 `unknown command 'XADD'`（Streams 是 5.0 才有）。
心跳/自注册能通、一 assign 就崩 = Redis 太老的信号。**真要验队列跨进程，必须 Redis>=5**（见 §5）。

---

## 4. systemd 托管（deploy/）

### 4.1 脚本语法 + unit 占位符替换 (done)
**已代测**：`bash -n` 两个脚本均 `SYNTAX OK`；`sed` 占位符替换产出合法 unit。

### 4.2 真机安装 (需自测)
**必须你在 Linux 云 worker 上验**（本机是 Windows，跑不了 systemd）：
```bash
sudo WORKER_ID=worker_cloud REDIS_HOST=<redis> deploy/install-worker.sh
systemctl status ticket-worker          # 期望 active (running)
journalctl -u ticket-worker -f          # 期望见 "Worker agent 启动: worker_cloud"
```
重点盯这些**第一次最可能踩的坑**（装时盯输出，报错贴我）：
- [ ] `python3 -m venv` 成功（缺 `python3-venv` 包会失败 -> `apt install python3-venv`）
- [ ] worker.env 权限 600、内容正确
- [ ] `Restart=always` 生效：`sudo systemctl kill ticket-worker` 后它**自动重启**
- [ ] 改 worker.env -> `systemctl restart` -> 新配置生效

### 4.3 一键重部署 (需自测)
```bash
git push                                       # 大脑机先推
deploy/redeploy-worker.sh user@worker-host     # 期望：远端 git pull + restart + 回报 status
```

---

## 5. 真 Redis 跨进程全链路（队列层）—— 已在阿里云 ECS 真 Redis 5+ 验通

**为什么需你来**：本机 Redis 3.0 没有 Streams（§3.4）。请在 **Redis>=5**（docker `redis:7-alpine` 最省事）上跑：
```bash
docker run -d -p 6379:6379 redis:7-alpine
# 终端 A（API）
REDIS_HOST=127.0.0.1 DRYRUN=1 python main.py api
# 终端 B（独立 worker 进程，模拟另一台机器）
REDIS_HOST=127.0.0.1 DRYRUN=1 WORKER_ID=worker_cloud python main.py worker
```
逐项：
- [ ] `curl localhost:8000/workers` -> worker_cloud 自注册出现
- [ ] assign 一个 job（见下）-> worker 终端日志 `收到 Job` -> RUNNING -> 回 result
- [ ] `curl localhost:8000/history/results` -> 落库
```bash
curl -s -X POST localhost:8000/workers/worker_cloud/assign -H 'Content-Type: application/json' \
  -d '{"account_id":"<号池里的id>","buyer_ids":[<id>],"project_id":1,"screen_id":1,"sku_id":1,"pay_money":100,"deadline":""}'
```

---

## 6. 停止 / 错误全链路捕获（本次重点验证）

### 6.1 停止终止在飞 job + 回流 cancelled 结果 (done)
**已代测**：`executor.cancel()` 现在产出 `reason="cancelled"` 的
AttemptResult 入 `_done` -> worker `_poll_results` 收走 -> push_result + 清 `_current` + 立即心跳。
**建议 UI 复验**：busy worker 点「停止」-> 卡片「当前」**立刻清空**、「上次」显示「失败 cancelled」、状态秒回空闲。

### 6.2 错误全链路捕获（result->store->前端展开）(done)
覆盖：
- `AttemptResult.detail` 字段：失败时带 stdout 尾巴/退出码/traceback（限 ~4KB），成功为空
- prepare->`prepare_error`、start->`start_error`、poll->`poll_error`、超时->`timeout`、售罄->`sold_out`、取消->`cancelled`
- `test_store.py`：detail 落库 roundtrip + 老库 ALTER TABLE 迁移
- `test_parser.py`：关键字->友好 reason + detail 尾巴限长
- `test_proc_cleanup.py`：`kill_process_tree` 连根杀后代（防孤儿进程）

**UI 复验**：失败的 worker「上次」字段可点开 -> 弹窗显示 `detail` 原文。

### 6.3 实时日志 worker_log (done 逻辑 / 需自测 UI)
**已代测**（逻辑）：`Executor.drain_output` 收增量 stdout、worker `_pump_logs` 推 `worker_log` 事件、
store 环形缓冲。**UI 复验**：worker 卡片「日志」按钮 -> 实时面板显示黑盒 stdout，按 worker 过滤、自动滚动。

---

## 执行顺序建议

1. §0 基线（已 114 passed + 5 skipped）—— 任何时候先跑
2. §2.4 前端 UI（浏览器，~5 分钟）—— 创建/删除 worker + 当前富信息 + 日志/detail 弹窗
3. §6.1 UI 复验停止（点停止看「当前」秒清）
4. §5 真 Redis 跨进程（docker redis:7，~10 分钟）—— 把瓶颈焊死
5. §4.2 systemd 真机（云 worker，首次盯输出）—— 上线前必做
