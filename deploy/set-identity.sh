#!/usr/bin/env bash
# 从镜像快照新开一台 worker 后，一条命令设好「身份 + Redis 连接」并重启。
# 把「改 worker.env 三项 + systemctl restart」收成一步。
#
# 用法（在新 worker 机器上，root）：
#   sudo WORKER_ID=worker_cloud2 WORKER_PROGRAM=my_driver \
#        REDIS_HOST=<brain-ip> REDIS_PASSWORD=<密码> deploy/set-identity.sh
#   # 可选再带 MAX_SLOTS / REDIS_PORT
#
# 前提：该机已由 install-worker.sh 装好 unit（或来自装好的镜像快照）。
set -euo pipefail

ENV_FILE="/etc/ticket-dispatcher/worker.env"
SERVICE_NAME="ticket-worker"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root：请用 sudo 运行。" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "未找到 ${ENV_FILE}：这台机器还没装过 worker。先跑 deploy/install-worker.sh。" >&2
  exit 1
fi
if [[ -z "${WORKER_ID:-}" ]]; then
  echo "必须给 WORKER_ID（每台 worker 唯一，否则心跳互相覆盖）。" >&2
  exit 1
fi

# 把命令行可见的 env 覆盖进 worker.env（有则替换，无则追加）
for k in WORKER_ID WORKER_PROGRAM MAX_SLOTS REDIS_HOST REDIS_PORT REDIS_PASSWORD; do
  v="${!k:-}"
  [[ -z "${v}" ]] && continue
  if grep -q "^${k}=" "${ENV_FILE}"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "${ENV_FILE}"
  else
    echo "${k}=${v}" >> "${ENV_FILE}"
  fi
done

echo "==> 已更新 ${ENV_FILE}：WORKER_ID=${WORKER_ID} REDIS_HOST=${REDIS_HOST:-未改}"
systemctl restart "${SERVICE_NAME}"
echo "==> 已重启 ${SERVICE_NAME}；worker 将自注册到 bus，前端控制台稍后出现。"
systemctl --no-pager status "${SERVICE_NAME}" || true
