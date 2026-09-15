#!/bin/bash
# 一键 管理  报修工单系统 + 管理后台
# 用法: bash manage.sh start|stop|restart|status|logs
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MAIN_PID="data/main.pid"
ADMIN_PID="data/admin.pid"
MAIN_LOG="logs/app.log"
STDERR_LOG="logs/stderr.log"  # 仅承载未捕获异常 traceback；常规日志由 logging 文件 handler 落盘
ADMIN_LOG="logs/admin.log"
# 管理后台配置统一走 admin/.env（server.py 也读同一文件）：端口、监听地址、密码
ADMIN_ENV_FILE="admin/.env"
if [ -z "$ADMIN_PORT" ] && [ -f "$ADMIN_ENV_FILE" ]; then
  ADMIN_PORT="$(grep -E '^[[:space:]]*ADMIN_PORT=' "$ADMIN_ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
fi
if [ -z "$ADMIN_HOST" ] && [ -f "$ADMIN_ENV_FILE" ]; then
  ADMIN_HOST="$(grep -E '^[[:space:]]*ADMIN_HOST=' "$ADMIN_ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
fi
ADMIN_PORT="${ADMIN_PORT:-8899}"
# 默认只绑本机：要局域网访问请设 ADMIN_HOST=0.0.0.0（无密码时等于把数据库交出去）
ADMIN_HOST="${ADMIN_HOST:-127.0.0.1}"
PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then PYTHON="python3"; fi
# 管理后台需 fastapi/uvicorn：逐个试候选解释器
# 注意：终端若激活了 .venv，裸 python3 会被遮蔽成 venv（同样无 fastapi），
# 因此把 /opt/homebrew/bin/python3 显式放在裸 python3 之前。
ADMIN_PYTHON=""
for cand in "$PYTHON" "/opt/homebrew/bin/python3" python3; do
  if "$cand" -c "import fastapi, uvicorn" 2>/dev/null; then ADMIN_PYTHON="$cand"; break; fi
done
# 兜底：沿用 PYTHON（启动失败时日志会提示 pip install fastapi uvicorn）
if [ -z "$ADMIN_PYTHON" ]; then ADMIN_PYTHON="$PYTHON"; fi
DWS_PATH="$HOME/.workbuddy/binaries/node/cli-connector-packages/bin"
export PATH="$DWS_PATH:$PATH"

mkdir -p data logs

is_running() {
  local pidfile=$1
  if [ -f "$pidfile" ]; then
    local pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then return 0; fi
    rm -f "$pidfile"
  fi
  return 1
}

do_start_main() {
  if is_running "$MAIN_PID"; then echo "主系统已在运行 pid=$(cat $MAIN_PID)"; return 0; fi
  echo "启动主系统 (PRODUCTION) ..."
  # stdout 丢弃（2026-09-07）：main.py 的 logging console StreamHandler 会被 nohup 重定向，
  # 与 RotatingFileHandler 双写同一 app.log，导致每行成对重复；全量日志由文件 handler 落盘即可。
  # stderr 单独保留：未捕获异常 traceback 不走 logging，需要留现场。
  nohup $PYTHON main.py --mode PRODUCTION >> /dev/null 2>> "$STDERR_LOG" &
  echo $! > "$MAIN_PID"
  echo "  pid=$!  log=$MAIN_LOG  stderr=$STDERR_LOG"
  sleep 1
  if ! is_running "$MAIN_PID"; then echo "启动失败，查看 $MAIN_LOG 与 $STDERR_LOG"; tail -20 "$MAIN_LOG"; tail -20 "$STDERR_LOG" 2>/dev/null || true; return 1; fi
}

do_start_admin() {
  if is_running "$ADMIN_PID"; then echo "管理后台已在运行 pid=$(cat $ADMIN_PID)  http://$ADMIN_HOST:$ADMIN_PORT"; return 0; fi
  echo "启动管理后台 http://$ADMIN_HOST:$ADMIN_PORT ..."
  echo "  解释器=$ADMIN_PYTHON"
  # 密码来源：真实环境变量 > admin/.env > data/.admin_password（由 server.py 解析并打印告警）
  # 不要在此处硬编码密码或 host，否则会覆盖 admin/.env 的配置。
  nohup $ADMIN_PYTHON admin/server.py --port "$ADMIN_PORT" --host "$ADMIN_HOST" >> "$ADMIN_LOG" 2>&1 &
  echo $! > "$ADMIN_PID"
  echo "  pid=$!  log=$ADMIN_LOG"
  sleep 1
  if ! is_running "$ADMIN_PID"; then echo "启动失败，查看 $ADMIN_LOG"; tail -20 "$ADMIN_LOG"; return 1; fi
}

do_stop() {
  local what=$1  # main|admin|all
  local stopped=0
  if [ "$what" = "main" ] || [ "$what" = "all" ]; then
    if is_running "$MAIN_PID"; then
      pid=$(cat "$MAIN_PID")
      echo "停止主系统 pid=$pid ..."
      kill "$pid" 2>/dev/null || true
      for i in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$MAIN_PID"
      stopped=1
    else
      # 兜底：按进程名杀
      pkill -f "main.py --mode" 2>/dev/null && stopped=1 || true
    fi
    # 二次兜底（2026-09-03）：裸启动（无 --mode 参数，如 `python main.py`）
    # 的残留进程会被上面的 pattern 漏掉，与新进程双跑、静默重复处理消息。
    # 只匹配「解释器+脚本相邻」的真实调用，避免误伤普通 shell。
    for _mpid in $(pgrep -f "python.*main\.py" 2>/dev/null); do
      [ "$_mpid" = "$$" ] && continue
      if kill -0 "$_mpid" 2>/dev/null; then
        kill "$_mpid" 2>/dev/null || true
        stopped=1
      fi
    done
  fi
  if [ "$what" = "admin" ] || [ "$what" = "all" ]; then
    if is_running "$ADMIN_PID"; then
      pid=$(cat "$ADMIN_PID")
      echo "停止管理后台 pid=$pid ..."
      kill "$pid" 2>/dev/null || true
      for i in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$ADMIN_PID"
      stopped=1
    else
      pkill -f "admin/server.py" 2>/dev/null && stopped=1 || true
      # 再按端口兜底
      lsof -ti :$ADMIN_PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
    fi
  fi
  if [ $stopped -eq 0 ]; then echo "没有运行中的进程"; fi
}

do_status() {
  echo "── 状态 ──"
  if is_running "$MAIN_PID"; then echo "主系统   ● 运行中  pid=$(cat $MAIN_PID)  log=$MAIN_LOG"; else echo "主系统   ○ 未运行"; fi
  if is_running "$ADMIN_PID"; then echo "管理后台 ● 运行中  pid=$(cat $ADMIN_PID)  http://$ADMIN_HOST:$ADMIN_PORT  log=$ADMIN_LOG"; else echo "管理后台 ○ 未运行  ($ADMIN_HOST:$ADMIN_PORT)"; fi
  echo ""
  echo "数据库: $ROOT/data/tickets.db  ($(du -h data/tickets.db 2>/dev/null | cut -f1))"
  echo "日志  : tail -f $MAIN_LOG  |  tail -f $ADMIN_LOG"
}

case "${1:-status}" in
  start)
    do_start_main
    do_start_admin
    echo ""
    do_status
    echo ""
    echo "✅ 全部启动完成  管理后台: http://$ADMIN_HOST:$ADMIN_PORT"
    echo "   密码/端口/监听地址在 admin/.env 里改（密码为空则免密，勿对公网免密开放）"
    ;;
  stop)
    do_stop all
    echo "已停止"
    ;;
  restart)
    do_stop all; sleep 1
    do_start_main; do_start_admin
    echo ""; do_status
    ;;
  start-main)  do_start_main; do_status ;;
  start-admin) do_start_admin; do_status ;;
  stop-main)   do_stop main; echo "已停止主系统" ;;
  stop-admin)  do_stop admin; echo "已停止管理后台" ;;
  status) do_status ;;
  logs) echo "=== $MAIN_LOG ==="; tail -n 50 "$MAIN_LOG"; echo ""; echo "=== $ADMIN_LOG ==="; tail -n 50 "$ADMIN_LOG" ;;
  log-main) tail -f "$MAIN_LOG" ;;
  log-admin) tail -f "$ADMIN_LOG" ;;
  *) echo "用法: bash manage.sh {start|stop|restart|status|logs|start-main|start-admin|stop-main|stop-admin}"; exit 1 ;;
esac
