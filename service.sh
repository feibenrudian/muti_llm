#!/usr/bin/env bash
# muti_llm 服务管理脚本：start | stop | restart | status
# 单进程交付：uvicorn 同时提供 Web UI（静态托管）与 OpenAI 兼容 API。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
RUN_DIR="$ROOT_DIR/run"
PID_FILE="$RUN_DIR/muti_llm.pid"
LOG_FILE="$RUN_DIR/server.log"

HOST="${MUTILLM_HOST:-0.0.0.0}"
PORT="${MUTILLM_PORT:-8000}"

# 0.0.0.0 仅表示“监听所有网卡”，本机访问/健康检查仍走 127.0.0.1
connect_host() {
  if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
    echo "127.0.0.1"
  else
    echo "$HOST"
  fi
}

# 局域网访问地址（取默认路由出口网卡的 IPv4，取不到则回退 127.0.0.1）
lan_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]\+\).*/\1/p' | head -n1 || true
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# 占用 $PORT 的监听进程 PID 列表（可能为空）
port_pids() {
  lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

# 该 PID 的工作目录是否是本仓库 backend（即本服务相关进程，含脚本外启动的 dev server）
is_our_backend() {
  local pid="$1" cwd
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 || true)"
  [ -n "$cwd" ] && [ "$cwd" = "$BACKEND_DIR" ]
}

kill_and_wait() {
  local pid="$1" i
  kill "$pid" 2>/dev/null || true
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  echo "→ pid $pid 未响应，强制结束 (kill -9)"
  kill -9 "$pid" 2>/dev/null || true
}

# 进程已死就不再等，避免健康检查被端口上的其他进程“代答”造成假成功
wait_healthy() {
  local pid="$1" i
  for i in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || return 1
    if curl -sf "http://$(connect_host):$PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# 前端源码/构建配置比上次产物新（或产物缺失）时返回 0
frontend_outdated() {
  local marker="$ROOT_DIR/frontend/dist/index.html"
  [ ! -f "$marker" ] && return 0
  local changed
  changed="$(find "$ROOT_DIR/frontend/src" \
    "$ROOT_DIR/frontend/index.html" \
    "$ROOT_DIR/frontend/package.json" \
    "$ROOT_DIR/frontend/package-lock.json" \
    "$ROOT_DIR/frontend/tsconfig.json" \
    "$ROOT_DIR/frontend/vite.config.ts" \
    -type f -newer "$marker" -print -quit 2>/dev/null || true)"
  [ -n "$changed" ]
}

ensure_frontend_built() {
  if frontend_outdated; then
    echo "→ 检测到前端源码/配置更新（dist 过旧或缺失），重新构建 Web UI（npm run build）…"
    (cd "$ROOT_DIR/frontend" && npm run build)
  else
    echo "→ 前端 dist 已是最新，跳过构建"
  fi
}

print_ports() {
  local display_host="$HOST" lip
  if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
    lip="$(lan_ip)"
    display_host="${lip:-127.0.0.1}"
  fi
  echo "──────────────────────────────────────────────────────"
  if [ "$display_host" != "$HOST" ]; then
    echo "  监听地址         $HOST（所有网卡，局域网可访问）"
    echo "  局域网访问       http://$display_host:$PORT/"
    echo "  本机访问         http://127.0.0.1:$PORT/"
  fi
  echo "  Web UI          http://$display_host:$PORT/"
  echo "  OpenAI 兼容 API  http://$display_host:$PORT/v1/chat/completions   (Authorization: Bearer <服务Key>)"
  echo "  管理 API         http://$display_host:$PORT/api/admin"
  echo "  API 文档         http://$display_host:$PORT/docs"
  echo "  健康检查         http://$display_host:$PORT/health"
  echo "  运行日志         $LOG_FILE"
  echo "──────────────────────────────────────────────────────"
}

do_start() {
  if is_running; then
    echo "✗ 服务已在运行 (pid $(cat "$PID_FILE"))，端口 $PORT；如需重启请用: $0 restart"
    exit 1
  fi
  rm -f "$PID_FILE"

  # 端口被占用时直接失败：否则新进程 bind 失败退出，健康检查却被占用者代答，造成“假启动成功”
  local occupants
  occupants="$(port_pids)"
  if [ -n "$occupants" ]; then
    echo "✗ 端口 $PORT 已被占用 (pid: $(echo $occupants))，请先执行: $0 stop"
    exit 1
  fi

  mkdir -p "$RUN_DIR"
  ensure_frontend_built

  echo "→ 启动 uvicorn（启动即加载最新后端代码，依赖由 uv 自动同步），监听 $HOST:$PORT …"
  (
    cd "$BACKEND_DIR"
    nohup env MUTILLM_HOST="$HOST" MUTILLM_PORT="$PORT" \
      uv run uvicorn app.main:app --host "$HOST" --port "$PORT" \
      >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
  )

  local pid
  pid="$(cat "$PID_FILE")"
  if wait_healthy "$pid" && kill -0 "$pid" 2>/dev/null; then
    echo "✓ 服务已启动 (pid $pid)"
    print_ports
  else
    echo "✗ 启动失败（进程提前退出或 30s 内 /health 未就绪），最近日志："
    tail -n 20 "$LOG_FILE" || true
    do_stop >/dev/null 2>&1 || true
    exit 1
  fi
}

do_stop() {
  local stopped=1
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "→ 停止服务 (pid $pid)…"
    kill_and_wait "$pid"
    stopped=0
  fi
  rm -f "$PID_FILE"

  # 兜底：PID 文件丢失/失效（或进程由 make dev-backend 等脚本外方式启动）时，
  # 按“占用端口且 cwd 是本仓库 backend”识别并清理残留服务进程
  local leftovers p i ours=""
  leftovers="$(port_pids)"
  for p in $leftovers; do
    if is_our_backend "$p"; then
      ours="$ours $p"
    else
      echo "⚠ 端口 $PORT 被非本服务进程占用 (pid $p)，未动它"
    fi
  done
  if [ -n "$ours" ]; then
    echo "→ 清理占用端口 $PORT 的残留服务进程 (pid:$ours )"
    kill $ours 2>/dev/null || true
    for i in $(seq 1 20); do
      local any=0
      for p in $ours; do kill -0 "$p" 2>/dev/null && any=1; done
      [ "$any" -eq 0 ] && break
      sleep 0.5
    done
    for p in $ours; do
      if kill -0 "$p" 2>/dev/null; then
        echo "→ pid $p 未响应，强制结束 (kill -9)"
        kill -9 "$p" 2>/dev/null || true
      fi
    done
    stopped=0
  fi

  if [ "$stopped" -eq 1 ]; then
    echo "✓ 服务未在运行"
  else
    echo "✓ 已停止"
  fi
}

do_status() {
  if is_running; then
    echo "✓ 运行中 (pid $(cat "$PID_FILE"))，监听 $HOST:$PORT"
    curl -sf "http://$(connect_host):$PORT/health" && echo ""
    return 0
  fi
  local occupants
  occupants="$(port_pids)"
  if [ -n "$occupants" ]; then
    echo "⚠ PID 文件缺失/失效，但端口 $PORT 正被进程占用 (pid: $(echo $occupants))"
    echo "  多为本脚本外启动的服务（如 make dev-backend）；可执行 $0 restart 接管"
    curl -sf "http://$(connect_host):$PORT/health" && echo ""
    return 0
  fi
  echo "✗ 未运行"
  exit 1
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop || true; do_start ;;
  status) do_status ;;
  *)
    echo "用法: $0 {start|stop|restart|status}"
    echo "环境变量: MUTILLM_HOST(默认 0.0.0.0，监听所有网卡；仅本机可设为 127.0.0.1)  MUTILLM_PORT(默认 8000)"
    exit 1
    ;;
esac
