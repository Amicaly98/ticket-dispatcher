# deploy/ — Worker 节点 systemd 托管

把 Linux worker 装成 systemd 服务，解决两件事：**不用再挂着终端**（`Restart=always` 崩了自愈）、
**重启不用手 kill**（`systemctl restart`）。配置走 `EnvironmentFile`，改配置不动 unit。

> 适用：worker 在 **Linux 云 ECS**。本地「大脑」（Windows）跑 API/前端，不需要这套。

## 一键初始化（推荐）

新机器上一行命令搞定全部（装依赖 → clone → venv → systemd → 启动）：

```bash
curl -sSL https://raw.githubusercontent.com/<your-org>/ticket-dispatcher/main/deploy/setup-worker.sh \
  | bash -s -- --id worker_cloud --redis <大脑机IP> --password <redis密码>
```

可选参数：`--program <driver名>`、`--slots N`、`--port 6379`。

## 文件

| 文件 | 作用 |
|---|---|
| `setup-worker.sh` | **一键初始化**：装依赖 + clone + venv + systemd + 启动 |
| `ticket-worker.service` | systemd unit 模板（占位符 `__ENV_FILE__/__REPO_DIR__/__VENV_PYTHON__` 由安装脚本填实） |
| `worker.env.example` | 配置模板 → 装到 `/etc/ticket-dispatcher/worker.env` |
| `install-worker.sh` | 一次性安装：建 venv + 装依赖 + 写 env + 装 unit + 起服务（幂等） |
| `redeploy-worker.sh` | 从大脑机一键更新远端 worker：`git pull` + `systemctl restart` |
| `set-identity.sh` | 从镜像快照新开机后，一条命令设 `WORKER_ID`/`REDIS_HOST`/密码 + 重启 |

## 手动安装（在 worker 机器，repo 根目录）

```bash
git clone <repo> ~/ticket-dispatcher && cd ~/ticket-dispatcher
sudo WORKER_ID=worker_cloud MAX_SLOTS=1 REDIS_HOST=<redis> REDIS_PASSWORD=<密码> \
  deploy/install-worker.sh
# 之后按需改： sudo nano /etc/ticket-dispatcher/worker.env && sudo systemctl restart ticket-worker
```

装完 worker 自注册到 bus，前端「Worker 控制台」就能看到它（无需在 settings.yaml 登记）。

## 日常

```bash
systemctl status ticket-worker          # 状态
journalctl -u ticket-worker -f          # 实时日志（替代挂终端 + tee）
sudo systemctl restart ticket-worker    # 改配置/拉新代码后重启
sudo deploy/install-worker.sh --uninstall   # 卸载（保留 env 与 repo）
```

## 镜像快照批量上 worker（最省事的扩容方式）

要加 N 台 worker，不用每台都 `git clone`+装依赖。做一次基准镜像，之后开机改个名就上线：

1. **基准机装好一次**：在一台云 ECS 上 `git clone` + `deploy/install-worker.sh`
   （建好 `.venv`、装齐依赖、装好 unit）。
2. **做快照前清掉身份**：把 `/etc/ticket-dispatcher/worker.env` 的 `WORKER_ID` 留空/占位。
   否则克隆出来的机器全同名 → 心跳互相覆盖，控制台只看得到一台。
   ```bash
   sudo sed -i 's|^WORKER_ID=.*|WORKER_ID=|' /etc/ticket-dispatcher/worker.env
   sudo systemctl stop ticket-worker     # 快照时别让它带着空身份乱跑
   ```
3. **云控制台基于该实例打镜像快照**。
4. **新 worker 从快照开机**，一条命令设身份并上线：
   ```bash
   sudo WORKER_ID=worker_cloud2 REDIS_HOST=<大脑/Redis ip> REDIS_PASSWORD=<密码> \
     deploy/set-identity.sh
   ```
   它会改好 `worker.env` + `systemctl restart` → worker 自注册 → 前端控制台自动出现。

### 端口放行

这是 **pull 模型**：worker 只**主动出站**连 Redis，**自身不需要开任何入站端口**——
对 NAT/安全组后的云机最省事。真正要放行的是**大脑/Redis 那一侧的 Redis 端口**（默认 6379），
让所有 worker 连得上。（务必给 Redis 设 `requirepass` + 安全组限源 IP。）

## 改完代码同步远端（在大脑机，git-bash）

```bash
git push                                        # 先把代码推上去
deploy/redeploy-worker.sh user@worker-host      # 远端 git pull + restart 一条龙
```

## 注意

- **Redis ≥ 5.0**（队列用 Streams）。连不上会让 worker 启动即失败（不再静默回退内存 bus）。
- **program**：必须与 Driver 注册时的名称一致。内置只有 `dryrun`（验证用），第三方 Driver 需先注册。
