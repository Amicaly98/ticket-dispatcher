# 从零部署 ticket-scheduler（非快照）

适用于全新云服务器（Ubuntu/Debian），不依赖镜像快照，从头搭建 API + Worker。

## 架构总览

```
大脑机（API）                Worker 云机 *N
+--------------+           +--------------+
| main.py api  |<-Redis--->| main.py worker|
| Collector    |           | CustomDriver  |
| FastAPI:8000 |           +--------------+
| 前端:3000    |
+--------------+
```

- **API 节点**：FastAPI + Collector（结果收集 + 推送），跑在大脑机上
- **Worker 节点**：常驻 agent，从 Redis 消费 Job、拉起黑盒进程、回报结果
- **Redis**：跨进程通信总线（Streams + Hash + Pub/Sub），需 >= 5.0

---

## 一、Redis（大脑机上）

```bash
# 安装 Redis 7
sudo apt update && sudo apt install -y redis-server

# 设密码（省掉 SSH 隧道，worker 直连）
sudo sed -i 's/^# requirepass.*/requirepass 你的密码/' /etc/redis/redis.conf
sudo sed -i 's/^bind .*/bind 0.0.0.0/' /etc/redis/redis.conf
sudo systemctl restart redis-server

# 验证
redis-cli -a 你的密码 ping   # 应返回 PONG
redis-cli -a 你的密码 info server | grep redis_version   # 需 >= 5.0
```

> 如果大脑机和 Worker 在同一内网，可以不设密码、不开放公网端口。
> 如果跨公网，建议用密码 + 防火墙只放行 Worker IP。

---

## 二、API 节点（大脑机）

### 2.1 系统依赖

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### 2.2 拉代码 + 虚拟环境

```bash
cd /opt
git clone https://github.com/your-org/ticket-scheduler.git
cd ticket-scheduler

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **快捷方式**：`deploy/setup-worker.sh` 一条命令完成 clone + venv + 依赖安装 + systemd 注册，
> 适合快速搭建 Worker 节点。

### 2.3 配置

```bash
# 号池（cookie + 购票人）
cp config/accounts.yaml.example config/accounts.yaml   # 或手动创建
# 编辑 accounts.yaml 填入真实 cookie 和购票人信息

# Worker 声明（前端「创建 Worker」也会写这里）
# config/settings.yaml 一般不需要手动改，前端操作即可
```

`config/accounts.yaml` 格式：

```yaml
accounts:
  - account_id: my_account_123
    program: my_program
    uid: 12345678
    username: "你的用户名"
    cookie:
      session_token: "你的会话令牌"
      csrf_token: "你的CSRF令牌"
    buyers:
      - id: 1001
        name: "张三"
        tel: "13800138000"
        personal_id: "身份证号"
        id_type: 0
```

### 2.4 推送配置（可选）

编辑 `config/settings.yaml`：

```yaml
push:
  enabled: true
  on_success: true
  on_failure: false
  channels:
    - type: sct
      url: "https://你的pushkey.push.ft07.com/send/sctp你的key.send"
```

### 2.5 启动 API

```bash
# 前台运行（调试用）
REDIS_HOST=127.0.0.1 REDIS_PASSWORD=你的密码 .venv/bin/python main.py api

# 后台运行（生产用）
nohup REDIS_HOST=127.0.0.1 REDIS_PASSWORD=你的密码 \
  .venv/bin/python -u main.py api >> logs/api.log 2>&1 &
```

### 2.6 systemd 托管（推荐）

```bash
sudo tee /etc/systemd/system/ticket-api.service << 'EOF'
[Unit]
Description=ticket-scheduler API
After=network.target redis.service

[Service]
Type=simple
WorkingDirectory=/opt/ticket-scheduler
Environment=REDIS_HOST=127.0.0.1
Environment=REDIS_PASSWORD=你的密码
ExecStart=/opt/ticket-scheduler/.venv/bin/python main.py api
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ticket-api
sudo journalctl -u ticket-api -f   # 看日志
```

### 2.7 验证

```bash
# API 健康检查
curl http://localhost:8000/workers

# 前端
浏览器打开 http://你的IP:8000   # 或 nginx 反代到 80/443
```

---

## 三、Worker 节点（云机 *N）

### 3.1 系统依赖

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### 3.2 拉代码 + 虚拟环境

```bash
cd /opt
git clone https://github.com/your-org/ticket-scheduler.git
cd ticket-scheduler

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 某些 Driver 可能需要额外依赖，参见 docs/driver_development.md
```

> **快捷方式**：`deploy/setup-worker.sh` 一条命令完成 clone + venv + 依赖安装 + systemd 注册。

### 3.3 黑盒二进制

```bash
# 将你的黑盒程序放在项目根目录或指定路径
# 确保有执行权限
chmod +x your_bot_binary
```

### 3.4 配置 Worker 身份

每个 Worker 需要唯一的 `WORKER_ID`：

```bash
# 方式一：环境变量
export WORKER_ID=worker_cloud1
export WORKER_PROGRAM=my_program
export MAX_SLOTS=1

# 方式二：写入 .env 文件（systemd 方式用这个）
cat > /opt/ticket-scheduler/worker.env << EOF
WORKER_ID=worker_cloud1
WORKER_PROGRAM=my_program
MAX_SLOTS=1
REDIS_HOST=大脑机IP
REDIS_PASSWORD=你的密码
EOF
```

### 3.5 启动 Worker

```bash
# 前台运行（调试用）
REDIS_HOST=大脑机IP REDIS_PASSWORD=你的密码 \
  WORKER_ID=worker_cloud1 WORKER_PROGRAM=my_program \
  .venv/bin/python main.py worker

# 后台运行
nohup REDIS_HOST=大脑机IP REDIS_PASSWORD=你的密码 \
  WORKER_ID=worker_cloud1 WORKER_PROGRAM=my_program \
  .venv/bin/python -u main.py worker >> logs/worker.log 2>&1 &
```

### 3.6 systemd 托管（推荐）

```bash
sudo tee /etc/systemd/system/ticket-worker.service << 'EOF'
[Unit]
Description=ticket-scheduler Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ticket-scheduler
EnvironmentFile=/opt/ticket-scheduler/worker.env
ExecStart=/opt/ticket-scheduler/.venv/bin/python main.py worker
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ticket-worker
sudo journalctl -u ticket-worker -f
```

### 3.7 验证 Worker 上线

```bash
# 在大脑机上检查
curl http://localhost:8000/workers
# 应看到 worker_cloud1 出现，status=idle

# 或用 CLI
python main.py status
```

---

## 四、指派第一个任务

### 4.1 前端操作

1. 浏览器打开 `http://大脑机IP:8000`
2. 「号池」标签 -> 扫码登录或手动填 cookie -> 点「刷新」拉购票人
3. 回到「Worker 控制台」-> 等 Worker 显示 online
4. 点「指派」-> 选号 -> 填活动 ID -> 加载场次票档 -> 选购票人 -> 填 deadline -> 提交

### 4.2 API 指派

```bash
curl -X POST http://localhost:8000/workers/worker_cloud1/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "account_id": "my_account_123",
    "project_id": 1001653,
    "screen_id": 1009926,
    "sku_id": 893317,
    "pay_money": 12800,
    "buyer_ids": [1001],
    "deadline": "2026-07-10T18:00:00+08:00"
  }'
```

---

## 五、批量部署 Worker（快照方式）

当需要多台 Worker 时，不用每台从头装：

1. 在一台 Worker 上完整走完 3.1-3.6
2. 制作该机器的云盘快照
3. 用快照创建新机器
4. 在新机器上只改身份：

```bash
# 一键设身份 + 重启
sudo WORKER_ID=worker_cloud2 WORKER_PROGRAM=my_program \
  /opt/ticket-scheduler/deploy/set-identity.sh
```

---

## 六、常见问题

| 问题 | 排查 |
|------|------|
| Worker 在 `/workers` 不出现 | 检查 `REDIS_HOST` / `REDIS_PASSWORD` 是否正确，`redis-cli` 能否连通 |
| 指派后 Worker 一直 idle | Worker 日志有无报错？`WORKER_PROGRAM` 是否匹配 |
| 抢到票但没推送 | 检查 `settings.yaml` 的 `push.enabled` 和 `channels` 配置 |
| Redis 连不上 | 检查防火墙 / 安全组是否放行 6379 端口 |

---

## 七、端口清单

| 端口 | 用途 | 开放范围 |
|------|------|----------|
| 8000 | API + 前端 | 按需（公网 or 内网） |
| 6379 | Redis | 仅 Worker IP |
| 3000 | 前端开发（dev 模式） | 本地 |
