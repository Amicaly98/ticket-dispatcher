#!/usr/bin/env bash
# ticket-dispatcher Worker 一键初始化脚本
# 用法：curl -sSL https://raw.githubusercontent.com/<org>/ticket-dispatcher/main/deploy/setup-worker.sh | bash -s -- --id worker_01 --redis 10.0.0.1:6379 --password xxx
#
# 做的事：
#   1. 安装系统依赖（Python 3.10+, git）
#   2. clone 仓库
#   3. 创建 venv + 安装 Python 依赖
#   4. 配置 worker 身份（WORKER_ID, REDIS_HOST 等）
#   5. 注册 systemd 服务 + 启动
#
# 前置条件：root 用户，Ubuntu/Debian，能连 GitHub 和 Redis

set -euo pipefail

# ── 参数解析 ──
WORKER_ID=""
REDIS_HOST=""
REDIS_PORT="6379"
REDIS_PASSWORD=""
PROGRAM=""
MAX_SLOTS="1"
GH_TOKEN=""
MIRROR=""
REPO_URL="https://github.com/<your-org>/ticket-dispatcher.git"
INSTALL_DIR="/root/ticket-dispatcher"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id)        WORKER_ID="$2";       shift 2 ;;
    --redis)     REDIS_HOST="$2";      shift 2 ;;
    --port)      REDIS_PORT="$2";      shift 2 ;;
    --password)  REDIS_PASSWORD="$2";  shift 2 ;;
    --program)   PROGRAM="$2";         shift 2 ;;
    --slots)     MAX_SLOTS="$2";       shift 2 ;;
    --token)     GH_TOKEN="$2";        shift 2 ;;
    --mirror)    MIRROR="$2";          shift 2 ;;
    --dir)       INSTALL_DIR="$2";     shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ -n "$MIRROR" ]]; then
  case "$MIRROR" in
    ghproxy)   REPO_URL="https://ghproxy.com/https://github.com/<your-org>/ticket-dispatcher.git" ;;
    gitclone)  REPO_URL="https://gitclone.com/github.com/<your-org>/ticket-dispatcher.git" ;;
    *)         REPO_URL="$MIRROR" ;;
  esac
fi

if [[ -n "$GH_TOKEN" && -z "$MIRROR" ]]; then
  REPO_URL="https://<user>:${GH_TOKEN}@github.com/<your-org>/ticket-dispatcher.git"
fi

if [[ -z "$WORKER_ID" || -z "$REDIS_HOST" ]]; then
  echo "用法: $0 --id <worker_id> --redis <redis_host[:port]> [--password <pwd>] [--program <driver>] [--slots N]"
  echo ""
  echo "示例:"
  echo "  $0 --id worker_cloud --redis 10.0.0.1 --password mypass"
  echo "  $0 --id worker_2 --redis 10.0.0.1:6379 --program my_driver --slots 2"
  exit 1
fi

echo "═══════════════════════════════════════════════"
echo "  ticket-dispatcher Worker 初始化"
echo "  ID: $WORKER_ID  Redis: $REDIS_HOST:$REDIS_PORT  Program: $PROGRAM"
echo "═══════════════════════════════════════════════"

# ── 1. 系统依赖 ──
echo ""
echo "[1/6] 安装系统依赖..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl > /dev/null 2>&1
echo "  ✓ 系统依赖完成"

# ── 2. Clone 仓库 ──
echo ""
echo "[2/6] Clone 仓库..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "  仓库已存在，pull 最新代码..."
  cd "$INSTALL_DIR"
  git pull --quiet
else
  git clone --quiet "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi
echo "  ✓ 仓库就绪"

# ── 3. Python venv + 依赖 ──
echo ""
echo "[3/6] 创建 Python 环境..."
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip install --quiet -r requirements-dev.txt 2>/dev/null || true
echo "  ✓ Python 环境就绪"

# ── 4. 配置 ──
echo ""
echo "[4/6] 配置 worker 身份..."
ENV_FILE="/etc/ticket-dispatcher/worker.env"
mkdir -p /etc/ticket-dispatcher

cat > "$ENV_FILE" <<EOF
# ticket-dispatcher worker 环境配置
# 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')

WORKER_ID=$WORKER_ID
WORKER_PROGRAM=$PROGRAM
MAX_SLOTS=$MAX_SLOTS

REDIS_HOST=$REDIS_HOST
REDIS_PORT=$REDIS_PORT
REDIS_PASSWORD=$REDIS_PASSWORD
EOF

echo "  ✓ 配置写入 $ENV_FILE"

# ── 5. systemd 服务 ──
echo ""
echo "[5/6] 注册 systemd 服务..."
cat > /etc/systemd/system/ticket-worker.service <<EOF
[Unit]
Description=ticket-dispatcher worker ($WORKER_ID)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python main.py worker
Restart=always
RestartSec=3
SuccessExitStatus=0 1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ticket-worker
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ticket-worker --quiet
echo "  ✓ systemd 服务已注册"

# ── 6. 启动 ──
echo ""
echo "[6/6] 启动 worker..."
systemctl restart ticket-worker
sleep 3

if systemctl is-active --quiet ticket-worker; then
  echo "  ✓ Worker 启动成功！"
  echo ""
  echo "═══════════════════════════════════════════════"
  echo "  完成！Worker $WORKER_ID 已连接到总线"
  echo ""
  echo "  查看状态: systemctl status ticket-worker"
  echo "  查看日志: journalctl -u ticket-worker -f"
  echo "  重启:     systemctl restart ticket-worker"
  echo "═══════════════════════════════════════════════"
else
  echo "  ✗ Worker 启动失败！查看日志："
  echo "    journalctl -u ticket-worker -n 20"
  exit 1
fi
