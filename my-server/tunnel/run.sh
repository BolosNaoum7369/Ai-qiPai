#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
: "${GH_TOKEN:?缺少 GH_PAT Actions Secret}"
: "${GH_REPO:?缺少目标仓库}"
: "${TUNNEL_UDID:?缺少 TUNNEL_UDID Actions Secret}"
: "${TUNNEL_TOKEN:?缺少 TUNNEL_TOKEN Actions Secret}"
: "${GITHUB_REF_NAME:?缺少工作流分支}"
: "${DB_HOST:?缺少 DB_HOST Actions Secret}"
: "${DB_USER:?缺少 DB_USER Actions Secret}"
: "${DB_PASSWORD:?缺少 DB_PASSWORD Actions Secret}"
: "${DB_NAME:?缺少 DB_NAME Actions Secret}"
: "${DB_PORT:?缺少 DB_PORT Actions Secret}"
export NODE_ENV=production
client_pid=""
server_pid=""
relay_pid=""
work_dir="$(mktemp -d)"
config_path="$work_dir/client.json"
client_log="$work_dir/tunnel.log"
server_log="$work_dir/server.log"
relay_at=$((SECONDS + 355 * 60))

cleanup() {
  trap - EXIT
  local pid attempt alive
  local -a pids=()
  for pid in "$client_pid" "$server_pid" "$relay_pid"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
    pids+=("$pid")
  done
  # 进程卡住时最多等待三秒，避免任务不退出导致 watchdog 无法补启。
  for ((attempt = 0; attempt < 30; attempt++)); do
    alive=false
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive=true
      fi
    done
    [[ "$alive" == true ]] || break
    sleep 0.1
  done
  for pid in "${pids[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  rm -rf -- "$work_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sha256sum --check tunnel-client-linux.sha256
chmod +x tunnel-client-linux
python3 - "$config_path" <<'PY'
import json
import os
import sys
from pathlib import Path

config = json.loads(Path("client.json").read_text(encoding="utf-8"))
config["udid"] = os.environ["TUNNEL_UDID"]
config["token"] = os.environ["TUNNEL_TOKEN"]
# JSON 也是合法 YAML，已有二进制可以直接读取。
Path(sys.argv[1]).write_text(json.dumps(config), encoding="utf-8")
PY

echo '服务端运行包 SHA256：'
sha256sum server.tar.gz
mkdir "$work_dir/server"
tar -xzf server.tar.gz --directory "$work_dir/server"
(cd "$work_dir/server" && npm ci --omit=dev --no-audit --no-fund)
(cd "$work_dir/server" && exec node dist/index.js) >"$server_log" 2>&1 &
server_pid=$!

# 根路径没有处理器，使用真实 Socket.IO 握手确认服务就绪。
ready=false
for ((attempt = 0; attempt < 60; attempt++)); do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo '棋牌服务器在就绪前退出'
    tail -n 50 "$server_log"
    exit 1
  fi
  response="$(curl --fail --silent --max-time 2 \
    'http://127.0.0.1:9527/socket.io/?EIO=4&transport=polling' || true)"
  if [[ "$response" == '0{'* ]]; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo '棋牌服务器 Socket.IO 握手等待超时'
  tail -n 50 "$server_log"
  exit 1
fi
echo '棋牌服务器 Socket.IO 握手成功'
cat "$server_log"

./tunnel-client-linux -config "$config_path" >"$client_log" 2>&1 &
client_pid=$!
mapping="$(python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path("client.json").read_text(encoding="utf-8"))
entry = config["mappings"][0]
print(f'[Mapping] {entry["lan_ip"]}:{entry["lan_port"]} -> {config["server"]}:{entry["remote_port"]}')
PY
)"
mapped=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if grep -Fq "$mapping" "$client_log"; then
    mapped=true
    break
  fi
  if ! kill -0 "$client_pid" 2>/dev/null; then
    break
  fi
  sleep 2
done
cat "$client_log"
if [[ "$mapped" != true ]]; then
  echo '第二个 client 的端口映射等待超时'
  exit 1
fi
echo '第二个 client 端口映射成功'

# 在六小时上限前触发下一轮；等待由主管循环完成，便于取消时清理。
relay_started=false
relay_failures=0
health_at=$((SECONDS + 30))
health_failures=0
while true; do
  if ! kill -0 "$client_pid" 2>/dev/null; then
    echo '隧道客户端退出'
    exit 1
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo '棋牌服务器退出'
    tail -n 50 "$server_log"
    exit 1
  fi
  if [[ "$SECONDS" -ge "$health_at" ]]; then
    response="$(curl --fail --silent --max-time 2 \
      'http://127.0.0.1:9527/socket.io/?EIO=4&transport=polling' || true)"
    if [[ "$response" == '0{'* ]]; then
      health_failures=0
    else
      health_failures=$((health_failures + 1))
      if [[ "$health_failures" -ge 3 ]]; then
        echo '棋牌服务器连续三次握手失败'
        tail -n 50 "$server_log"
        exit 1
      fi
    fi
    health_at=$((SECONDS + 30))
  fi
  if [[ "$relay_started" == false && "$SECONDS" -ge "$relay_at" ]]; then
    gh workflow run ci.yml --ref "$GITHUB_REF_NAME" &
    relay_pid=$!
    relay_started=true
  fi
  if [[ -n "$relay_pid" ]] && ! kill -0 "$relay_pid" 2>/dev/null; then
    if ! wait "$relay_pid"; then
      relay_failures=$((relay_failures + 1))
      if [[ "$relay_failures" -ge 3 ]]; then
        echo '接力 Actions 连续三次启动失败，结束本轮以便 watchdog 补启'
        exit 1
      fi
      echo '接力 Actions 启动失败，30 秒后重试'
      relay_started=false
      relay_at=$((SECONDS + 30))
    else
      echo '已请求启动下一轮 Actions'
    fi
    relay_pid=""
  fi
  sleep 5
done
