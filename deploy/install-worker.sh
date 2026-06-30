#!/usr/bin/env bash
# 在 Linux worker 机器上把 worker 装成 systemd 服务（一次性）。
# 解决「要挂着终端」「重启要手 kill」——之后增删/改配置全是 systemctl + 改 env 文件。
#
# 用法（在 worker 机器、repo 根目录下）：
#   sudo deploy/install-worker.sh                  # 用默认值装
#   sudo WORKER_ID=worker_cloud MAX_SLOTS=2 deploy/install-worker.sh
#   sudo deploy/install-worker.sh --uninstall      # 卸载
#
# 幂等：重复跑安全；已存在的 worker.env 不会被覆盖（只补缺失项靠你手改）。
set -euo pipefail

SERVICE_NAME="ticket-worker"
ENV_DIR="/etc/ticket-dispatcher"
ENV_FILE="${ENV_DIR}/worker.env"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"

# repo 根目录 = 本脚本上一级
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "==> 卸载 ${SERVICE_NAME}"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "${UNIT_DST}"
  systemctl daemon-reload
  echo "已移除 unit（保留 ${ENV_FILE} 与 repo，未动数据）。"
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "需要 root：请用 sudo 运行。" >&2
  exit 1
fi

echo "==> repo: ${REPO_DIR}"

# 1) venv
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "==> 未发现 .venv，创建并安装依赖"
  python3 -m venv "${REPO_DIR}/.venv"
  "${VENV_PYTHON}" -m pip install --upgrade pip
  "${VENV_PYTHON}" -m pip install -r "${REPO_DIR}/requirements.txt"
else
  echo "==> 复用已有 .venv: ${VENV_PYTHON}"
fi

# 2) EnvironmentFile：不覆盖已存在的（保住你填好的 WORKER_ID/密码）
mkdir -p "${ENV_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  echo "==> 保留已有 ${ENV_FILE}（未覆盖）"
else
  install -m 600 "${SCRIPT_DIR}/worker.env.example" "${ENV_FILE}"
  # 把脚本可见的 env 变量覆盖进去（命令行传的 WORKER_ID= 等）
  for k in WORKER_ID WORKER_PROGRAM MAX_SLOTS REDIS_HOST REDIS_PORT REDIS_PASSWORD; do
    v="${!k:-}"
    [[ -z "${v}" ]] && continue
    if grep -q "^${k}=" "${ENV_FILE}"; then
      sed -i "s|^${k}=.*|${k}=${v}|" "${ENV_FILE}"
    else
      echo "${k}=${v}" >> "${ENV_FILE}"
    fi
  done
  echo "==> 已写 ${ENV_FILE}（按需再编辑，含 600 权限保护密码）"
fi

# 3) 填 unit 占位符并安装
echo "==> 安装 unit -> ${UNIT_DST}"
sed -e "s|__ENV_FILE__|${ENV_FILE}|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__VENV_PYTHON__|${VENV_PYTHON}|g" \
    "${SCRIPT_DIR}/ticket-worker.service" > "${UNIT_DST}"

# 4) 起服务
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "==> 完成。常用命令："
echo "    systemctl status ${SERVICE_NAME}      # 看状态"
echo "    journalctl -u ${SERVICE_NAME} -f      # 实时日志（替代挂终端）"
echo "    sudo nano ${ENV_FILE} && sudo systemctl restart ${SERVICE_NAME}   # 改配置后重启"
echo "    sudo systemctl restart ${SERVICE_NAME}  # 拉新代码后重启（git pull 之后）"
systemctl --no-pager status "${SERVICE_NAME}" || true
