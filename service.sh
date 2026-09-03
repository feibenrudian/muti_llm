#!/usr/bin/env bash
# muti_llm 服务管理脚本：start | stop | restart | status
# 单进程交付：uvicorn 同时提供 Web UI（静态托管）与 OpenAI 兼容 API。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
RUN_DIR="$ROOT_DIR/run"
PID_FILE="$RUN_DIR/muti_llm.pid"
LOG_FILE="$RUN_DIR/server.log"

HOST="${MUTILLM_HOST:-127.0.0.1}"
PORT="${MUTILLM_PORT:-8000}"

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

wait_healthy() {
  local i
  for i in $(seq 1 60); do
    if curl -sf "http://$HOST:$PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

ensure_frontend_built() {
  if [ ! -f "$ROOT_DIR/frontend/dist/index.html" ]; then
    echo "→ frontend/dist 不存在，先构建 Web UI（npm run build）…"
    (cd "$ROOT_DIR/frontend" && npm run build)
  fi
}

print_ports() {
  echo "──────────────────────────────────────────────────────"
  echo "  Web UI          http://$HOST:$PORT/"
  echo "  OpenAI 兼容 API  http://$HOST:$PORT/v1/chat/completions   (Authorization: Bearer <服务Key>)"
  echo "  管理 API         http://$HOST:$PORT/api/admin"
  echo "  API 文档         http://$HOST:$PORT/docs"
  echo "  健康检查         http://$HOST:$PORT/health"
  echo "  运行日志         $LOG_FILE"
  echo "──────────────────────────────────────────────────────"
}

do_start() {
  if is_running; then
    echo "✗ 服务已在运行 (pid $(cat "$PID_FILE"))，端口 $PORT；如需重启请用: $0 restart"
    exit 1
  fi
  rm -f "$PID_FILE"
  mkdir -p "$RUN_DIR"
  ensure_frontend_built

  echo "→ 启动 uvicorn，监听 $HOST:$PORT …"
  (
    cd "$BACKEND_DIR"
    nohup env MUTILLM_HOST="$HOST" MUTILLM_PORT="$PORT" \
      uv run uvicorn app.main:app --host "$HOST" --port "$PORT" \
      >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )

  if wait_healthy; then
    echo "✓ 服务已启动 (pid $(cat "$PID_FILE"))"
    print_ports
  else
    echo "✗ 启动超时（30s 内 /health 未就绪），最近日志："
    tail -n 20 "$LOG_FILE" || true
    do_stop >/dev/null 2>&1 || true
    exit 1
  fi
}

do_stop() {
  if ! is_running; then
    echo "✓ 服务未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  echo "→ 停止服务 (pid $pid)…"
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "→ 强制结束"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "✓ 已停止"
}

do_status() {
  if is_running; then
    echo "✓ 运行中 (pid $(cat "$PID_FILE"))，端口 $PORT"
    curl -sf "http://$HOST:$PORT/health" && echo ""
  else
    echo "✗ 未运行"
    exit 1
  fi
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop || true; do_start ;;
  status) do_status ;;
  *)
    echo "用法: $0 {start|stop|restart|status}"
    echo "环境变量: MUTILLM_HOST(默认 127.0.0.1)  MUTILLM_PORT(默认 8000)"
    exit 1
    ;;
esac
