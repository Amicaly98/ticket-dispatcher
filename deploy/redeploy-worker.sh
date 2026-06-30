#!/usr/bin/env bash
# 从「大脑」机器一键更新远端 worker：git pull + 重启 systemd 服务。
# 把「改完→push→ssh过去→pull→找PID kill→重启」压成一条命令。
#
# 前提：worker 已用 install-worker.sh 装好；你能 ssh 到它；代码已 push 到 worker 能拉的 remote。
#
# 用法（在 Windows 用 git-bash，或任意有 ssh 的机器）：
#   deploy/redeploy-worker.sh user@worker-host
#   deploy/redeploy-worker.sh user@worker-host /opt/ticket-scheduler   # 自定义 repo 路径
#   WORKER_SSH=user@host deploy/redeploy-worker.sh                     # 用环境变量
set -euo pipefail

TARGET="${1:-${WORKER_SSH:-}}"
REMOTE_REPO="${2:-~/ticket-scheduler}"
SERVICE_NAME="ticket-worker"

if [[ -z "${TARGET}" ]]; then
  echo "用法: $0 user@worker-host [远端repo路径]" >&2
  echo "  或: WORKER_SSH=user@host $0" >&2
  exit 1
fi

echo "==> [${TARGET}] git pull + 重启 ${SERVICE_NAME}（repo: ${REMOTE_REPO}）"

# 一次 ssh 干完：拉代码 → 重启服务 → 回报状态。git pull 无更新也安全。
ssh "${TARGET}" bash -s <<EOF
set -e
cd "${REMOTE_REPO}"
echo "--- git pull ---"
git pull --ff-only
echo "--- restart ---"
sudo systemctl restart ${SERVICE_NAME}
sleep 1
sudo systemctl --no-pager status ${SERVICE_NAME} | head -6
EOF

echo "==> 完成。看实时日志： ssh ${TARGET} journalctl -u ${SERVICE_NAME} -f"
