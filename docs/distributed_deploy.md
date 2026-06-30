# 分布式部署 checklist（本地 API + 云 Worker，经 Redis）

> 目标拓扑：**本地电脑**当大脑（号池 + 指派 + dashboard），**云 ECS** 当执行手
> （拉起黑盒进程抢票）。两端通过 **Redis** 通信。

```
[本地] main.py api  ──┐                              ┌── [云ECS] main.py worker
  · accounts.yaml(号池+cookie)                       │     · 黑盒二进制
  · settings.yaml(worker列表)        REDIS (6379)    │     · converter 在这里跑(driver.prepare)
  · dashboard 指派         ◄──────────────────────►  │
  · Collector 收结果落库                              │
```

**核心心智模型**：converter 在 **worker 端**跑（`pop_job → driver.prepare → executor.start`）。
converter 从零构建 config（零模板），运行时实时拉项目元数据。
**号池和 cookie 在本地**，cookie 随 `Job`（`account.snapshot()`）经 Redis 流到云端 converter。

---

## 0. 文件/数据该放哪（最容易搞混）

| 东西 | 放哪个节点 | 说明 |
|---|---|---|
| `config/accounts.yaml`（号池 + cookie） | **本地 API** | assign 时 `load_accounts()` 读它 |
| `config/settings.yaml`（worker 列表） | **本地 API** | assign 的 `worker_id` 必须在这里登记 |
| 黑盒二进制 + 外部依赖 | **云 Worker** | executor 在云上拉起黑盒 |

---

## 1. Redis（先把通信底座立起来）

> **版本要求：Redis >= 5.0**（Job/Result 队列用 Streams，XADD/XREADGROUP 是 5.0 才有的。
> 3.x 会报 `unknown command 'XADD'`。心跳/状态用 Hash 不受影响，
> 所以「worker 能注册但 assign 必崩」就是 Redis 太老的信号）。

在云 ECS 上（或任一两端都可达的主机）：

```bash
apt-get install -y redis-server   # 确认 redis-server --version >= 5.0
redis-server --daemonize yes --save "" --appendonly no   # 无需持久化
# 要密码就加： --requirepass '<强密码>'
redis-cli ping        # PONG
```

**密码（已支持）**：设 `REDIS_PASSWORD` 环境变量，两端 `get_bus()` 自动带 AUTH（`src/queue.py`）。
**别把 6379 裸暴露公网**，按强度选：
- **（推荐）`--requirepass` + 安全组白名单**：6379 只放行对端公网 IP，且必须 AUTH。
- **内网/VPC**：两端同内网，redis bind 内网网卡。
- **SSH 隧道**：本地 `ssh -N -L 6379:127.0.0.1:6399 root@<云IP>`，本地用 `REDIS_HOST=127.0.0.1`。

---

## 2. 云 Worker 节点

```bash
cd ~/ticket-scheduler
git pull
pip install -r requirements.txt
# 某些 Driver 可能需要额外依赖，参见对应 Driver 文档

# 检查清单
chmod +x <你的黑盒二进制>   # 二进制可执行
```

启动 worker（连 Redis，**不要 DRYRUN**）：

**推荐：systemd 托管**（不用挂终端、崩了自愈、重启不用手 kill）：
```bash
sudo WORKER_ID=worker_cloud MAX_SLOTS=1 REDIS_HOST=127.0.0.1 REDIS_PASSWORD=<可选> \
  deploy/install-worker.sh
journalctl -u ticket-worker -f      # 看日志
```
详见 `deploy/README.md`。装完 worker 自注册到 bus，本地前端「Worker 控制台」直接可见、可指派。

**或：前台手动起**（调试时）：
```bash
REDIS_HOST=127.0.0.1 REDIS_PORT=6379 \
WORKER_ID=worker_cloud MAX_SLOTS=1 \
python main.py worker
```
启动日志应看到 `Worker agent 启动: worker_cloud`。注意：设了 `REDIS_HOST` 却连不上现在会**直接崩**
（不再静默回退内存 bus，见坑#1）；要内存模式就别设 `REDIS_HOST`。

---

## 3. 本地 API 节点

`config/settings.yaml` —— **不再需要登记 worker**。worker 自注册：云端起 `WORKER_ID=worker_cloud
python main.py worker` 后会往 bus 发心跳，本地 api 的 `GET /workers` 自动列出它、assign 直接认。
`settings.yaml` 的 `workers:` 现在是**可选声明**（只为「worker 没上线时也想在控制台占个离线位」或覆盖
`program`/`max_slots` 默认值），留 `workers: []` 即可，控制台完全由活 worker 心跳驱动。

`config/accounts.yaml` —— 号池里要有目标号 + **有效 cookie** + 购票人：
- 最稳：起 api 后用 dashboard `/accounts` **扫码登录**，自动写 uid/username/cookie。
- 账号 `account_id` 命名要和号池 key 一致。

启动 api（连同一个 Redis）：
```bash
REDIS_HOST=127.0.0.1 REDIS_PORT=6379 REDIS_PASSWORD=<可选> \
python main.py api
# dashboard： cd dashboard && npm run dev  （proxy 到本地 8000）
```

---

## 4. 连通性验证（先验管道，再验抢票）

**A. 两端都连到同一个 Redis**（坑#1 的反面）：
```bash
redis-cli -h <redis地址> ping            # 两端各跑一次，都要 PONG
redis-cli -h <redis地址> hgetall ts:workers   # 应看到 worker_cloud 心跳
```

**B. DRYRUN 过一遍管道**（不碰真实下单）：本地 api 上临时设 `DRYRUN=1` 起一份，
或直接指派一个 job，看云 worker 日志是否 `收到 Job` -> 回 `result`、本地 `/history/results` 落库。

**C. 真实级**：去掉 DRYRUN，指派一个在售项目：
```bash
curl -s -X POST http://localhost:8000/workers/worker_cloud/assign \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"your_account_id","project_id":1001742,"screen_id":1007565,
       "sku_id":883895,"pay_money":8800,"buyer_ids":[3643011],"deadline":""}'
```
盯云 worker 日志确认任务被消费。

---

## 5. 常见坑（按踩坑概率排序）

1. **~~worker 静默回退 InMemoryBus~~（已修）** —— 旧行为：`get_bus()` 连不上 Redis 时不报错、退回内存 bus，
   worker 永远收不到 job、assign 石沉大海。**现在**：设了 `REDIS_HOST` 却连不上会**直接崩**（进程启动即失败），
   不再伪装正常。要单机内存模式就别设 `REDIS_HOST`。
2. **~~`worker_id` 对不上~~（已改：worker 自注册）** —— assign 不再要求 `worker_id` 在 `settings.yaml` 登记；
   worker 起进程发心跳即可被指派。仍要注意两端连**同一个** Redis（库号/host/密码一致）。
3. **cookie 以本地号池为准** —— converter 注入的是**号池**的 cookie（零模板，不依赖模板文件）。号池 cookie 过期 ->
   converter 拉购票人失败 -> **真实模式 prepare 直接报错不抢**（fail-safe，刻意的）。
4. **老 api/worker 进程跑旧代码** —— 改完代码记得 `git pull` + 重启两端进程。
5. **多 slot 撞 config** —— 某些黑盒程序无 `--config` 参数、固定读默认配置文件，`MAX_SLOTS>1`
   同机会互相覆盖。先用 `MAX_SLOTS=1`。
6. **改 driver 代码后必须重启 worker** —— import 时的内存常量，改磁盘文件
   不重启进程不生效。
7. **WS 刷新后日志面板空白** —— 旧版 WS 不回放历史。新版连接时自动回放最近 400 条日志事件。
   确保 API 代码是最新的。

---

## 6. 真实抢票安全提醒

- 跨过下单接口可能**真下单、真扣款、真出票**。先拿**便宜/不在意的在售项目**验证链路，
  别拿心仪票当试验品。
- 确认目标项目**还在售**（`sale_end` 时间戳）——过期会直接报售罄。
- cookie / 购票人身份证号是敏感数据：`accounts.yaml` 已 gitignore，别提交、别外传。
