# Worker 单独部署 + 快照批量初始化（实操）

> 目标：**云服务器一机一个 worker**，每台都连上**你电脑上的 API 端**，在控制台里出现、能被指派、能实时回日志。
> 用镜像快照 + 一条命令把新机拉起来。

---

## 0. 先搞懂连接关系（很关键，否则白折腾）

**Worker 不连 API 的 HTTP 端口（8000）。** worker 只连 **Redis**（总线）。
你电脑上的 API 也只连同一个 Redis。worker 之所以能在你控制台里出现，是因为
**两边连的是同一个 Redis**，不是 worker 直连你电脑。

```
  你电脑(API + 前端控制台)              云 Redis(公网, 总线)            云 worker *N(一机一个)
   main.py api --REDIS_HOST出站连接-->  redis:6379(requirepass) <--REDIS_HOST出站连接-- main.py worker
        ^                                    (Job/Result/心跳/事件)                         |
        +-------------------- 控制台看到 worker、指派、收结果，全经 Redis -----------------+
```

**推论**：你电脑在家用网络（无公网入站），所以 **Redis 不能放你电脑**——放一台**公网可达的云服务器**，
你电脑的 API 和所有云 worker 都**向它出站**连接。本机就符合：Redis 6.0+、bind 0.0.0.0、
有密码——可直接当总线。

要求：**Redis >= 5.0**（Job/Result 队列用 Streams，3.x 会报 `unknown command 'XADD'`）。

---

## 1. 一次性：定一台云服务器当 Redis 总线

在这台机器确认：

```bash
# Redis 在跑、可达、版本 >=5、有密码、bind 0.0.0.0
redis-cli -a '<REDIS_PASSWORD>' ping                 # PONG
redis-cli -a '<REDIS_PASSWORD>' INFO server | grep redis_version   # >= 5.0
redis-cli -a '<REDIS_PASSWORD>' CONFIG GET bind      # 0.0.0.0
```

**安全组**：放行入站 `6379/tcp`（来源限你电脑公网 IP + 各 worker 公网 IP，别对全网开）。
拿这台机的**公网 IP**（下文记作 `<REDIS_IP>`）：云控制台看，或 `curl -s ifconfig.me`。

> 必须 `requirepass`——公网裸奔的 Redis 会被秒爆破挖矿。

---

## 2. 你电脑（API 端）：连云 Redis 起 API

在**你电脑**的 repo 目录：

```bash
REDIS_HOST=<REDIS_IP> REDIS_PORT=6379 REDIS_PASSWORD=<REDIS_PASSWORD> \
  python main.py api
# 前端：另开 cd dashboard && npm run dev -> 浏览器 localhost:3000
```

`REDIS_HOST` 一设，API 就不再起本地 Redis，直连云总线。前端控制台 `/workers` 里会出现
所有连同一 Redis 的 worker（自注册，无需在 settings.yaml 预先登记）。

---

## 3. 做 worker 快照模板（一次，烤进镜像）

在**一台**配好的 worker 机上准备好「与身份无关、所有克隆共用」的东西，再打快照：

1. **repo + `.venv`**（含 Driver 所需依赖）——`sudo deploy/install-worker.sh` 会建 venv、装 unit、写 env。
2. **黑盒二进制**：你的目标程序。
3. **systemd unit 已装**（install-worker.sh 跑过）。
4. `worker.env` 里**预填** `WORKER_PROGRAM` / `REDIS_*`，但 **WORKER_ID 留到克隆后再设**。

**打快照前务必停服务**，否则克隆出来的机器一开机就用模板的旧 WORKER_ID 抢注，和别人心跳互盖：

```bash
sudo systemctl stop ticket-worker
# 控制台对这台机打镜像快照
```

---

## 4. 每台云 worker（一机一个，克隆后一条命令）

从快照新开机器后，root 跑 `set-identity.sh`：

```bash
sudo WORKER_ID=worker_cloud2 WORKER_PROGRAM=my_program \
     REDIS_HOST=<REDIS_IP> REDIS_PORT=6379 REDIS_PASSWORD=<REDIS_PASSWORD> \
     deploy/set-identity.sh
```

`set-identity.sh` 做的事：把这几项写进 `/etc/ticket-scheduler/worker.env` -> `systemctl restart ticket-worker`
-> worker 自注册到 Redis 总线。**每台 `WORKER_ID` 必须唯一**（否则心跳互相覆盖、控制台只剩一台）。

> 注意：如果 Redis 总线就是这台机本身（少见），`REDIS_HOST` 写 `127.0.0.1`；
> 一机一 worker 的常规场景，`REDIS_HOST` 写**总线机的公网 IP**。

---

## 5. 验证：连上 + 心跳正常

**在 worker 机上**：

```bash
journalctl -u ticket-worker -n 20 --no-pager
#   应看到 "Worker agent 启动: worker_cloud2 (max_slots=1, program=my_program...)"
#   不应有 redis 连接错误 / "unknown command 'XADD'" / 反复 Restart

# 直接验 Redis 可达（最快定位）
redis-cli -h <REDIS_IP> -a '<REDIS_PASSWORD>' ping     # 必须 PONG
```

**在你电脑（或控制台）**：

```bash
curl -s http://localhost:8000/workers | python3 -m json.tool | grep -A4 worker_cloud2
#   status 应为 "idle"（心跳新鲜）、program 为你设的值
```

控制台 `/`（Worker 控制台）里该 worker 卡片出现、状态 idle，即连通成功。

---

## 6. 心跳排错速查

| 现象 | 原因 / 处理 |
|---|---|
| worker 起来了但控制台**根本不出现** | worker 连不上 Redis：`REDIS_HOST/PORT/PASSWORD` 错，或安全组没放行 6379。worker 端 `redis-cli -h <IP> -a <pw> ping` 不 PONG 就是网络/认证问题。 |
| worker 显示 **offline** | 曾注册过、现在心跳停了（进程挂/网络断）。`journalctl -u ticket-worker -f` 看；`systemctl status` 看是否在反复重启。 |
| 指派后 worker **永远不动** | Redis < 5.0：`journalctl` 里有 `unknown command 'XADD'`。换 >=5 的 Redis。 |
| 控制台 worker 数**少于实际机器数** | `WORKER_ID` 撞了——多台用了同一个 ID，心跳互相覆盖。每台必须唯一。 |
| worker 起来即退出（exit） | 设了 `REDIS_HOST` 却连不上会**直接崩**（不再静默回退 InMemoryBus）。先把 Redis 连通。 |

---

## 7. 日常运维

```bash
# 拉新代码后重启某台 worker（从大脑机一键）
deploy/redeploy-worker.sh user@<worker-host>

# 改配置后重启
sudo nano /etc/ticket-scheduler/worker.env && sudo systemctl restart ticket-worker

# 实时日志（替代挂终端）
journalctl -u ticket-worker -f
```

相关文档：`deploy/README.md`（systemd 工作流）、`docs/distributed_deploy.md`（分布式总览）。
