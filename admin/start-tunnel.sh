#!/bin/bash
# 隧道模式一键启动：本地管理后台（默认只绑 127.0.0.1 + 必须有密码）+ Cloudflare Tunnel
# 用法: bash dingtalk_script/admin/start-tunnel.sh
# 配置: 密码/端口/监听地址见 admin/.env（真实环境变量优先）
# 前提: 已做过 cloudflared tunnel login / create / route（见 cloudflared-config.example.yml）
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
ENVFILE="$DIR/admin/.env"

# ---- 配置一律以 admin/.env 为准（server.py 也读同一个文件） ----
# 端口：真实环境变量 > admin/.env 的 ADMIN_PORT > 8899
if [ -z "$ADMIN_PORT" ] && [ -f "$ENVFILE" ]; then
  ADMIN_PORT="$(grep -E '^[[:space:]]*ADMIN_PORT=' "$ENVFILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
fi
PORT=${ADMIN_PORT:-8899}

# 监听地址：真实环境变量 > admin/.env 的 ADMIN_HOST > 127.0.0.1（隧道模式只绑本机）
if [ -z "$ADMIN_HOST" ] && [ -f "$ENVFILE" ]; then
  ADMIN_HOST="$(grep -E '^[[:space:]]*ADMIN_HOST=' "$ENVFILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
fi
HOST=${ADMIN_HOST:-127.0.0.1}

mkdir -p logs data

# ---- 选一个带 fastapi/uvicorn 的解释器（逻辑与 manage.sh 一致） ----
PYTHON=""
for cand in "$DIR/.venv/bin/python" "/opt/homebrew/bin/python3" python3; do
  if "$cand" -c "import fastapi, uvicorn" 2>/dev/null; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then PYTHON="python3"; fi

# ---- 密码：真实环境变量 > admin/.env > data/.admin_password > 自动生成 ----
# server.py 自己会读 admin/.env 与 data/.admin_password，所以这里**不做 export**
# （硬 export 反而会覆盖 admin/.env，造成"改了 .env 却不生效"的假象）。
# 只在两者都没提供密码时才生成一个，确保隧道模式绝不会以无密码状态对公网开放。
PWFILE="$DIR/data/.admin_password"
if [ -z "$ADMIN_PASSWORD" ] \
   && ! grep -qE '^[[:space:]]*ADMIN_PASSWORD=[^[:space:]]' "$ENVFILE" 2>/dev/null \
   && [ ! -s "$PWFILE" ]; then
  printf '%s' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')" > "$PWFILE"
  chmod 600 "$PWFILE"
  echo "已生成管理后台密码并保存到 data/.admin_password（仅本机可读）"
  echo "  想自己设密码：编辑 admin/.env 的 ADMIN_PASSWORD"
fi

# ---- cloudflared 定位（项目自带优先） ----
CLOUDFLARED="${CLOUDFLARED:-}"
if [ -z "$CLOUDFLARED" ]; then
  if [ -x "$DIR/bin/cloudflared" ]; then CLOUDFLARED="$DIR/bin/cloudflared";
  elif command -v cloudflared >/dev/null 2>&1; then CLOUDFLARED="cloudflared";
  elif [ -x "$HOME/.local/bin/cloudflared" ]; then CLOUDFLARED="$HOME/.local/bin/cloudflared";
  else echo "cloudflared 不在项目 bin/PATH（含 ~/.local/bin），先装好再跑"; exit 1; fi
fi

# ---- 起后台（只绑本机，不走 manage.sh 的默认） ----
if [ -f data/admin.pid ] && kill -0 "$(cat data/admin.pid)" 2>/dev/null; then
  echo "管理后台已在运行 pid=$(cat data/admin.pid)，跳过启动"
else
  echo "启动管理后台 http://$HOST:$PORT（对外走隧道域名）"
  ADMIN_PASSWORD="$ADMIN_PASSWORD" nohup "$PYTHON" admin/server.py --port "$PORT" --host "$HOST" >> logs/admin.log 2>&1 &
  echo $! > data/admin.pid
  sleep 2
  if ! kill -0 "$(cat data/admin.pid)" 2>/dev/null; then echo "启动失败，看 logs/admin.log"; tail -20 logs/admin.log; exit 1; fi
fi
curl -sf "http://127.0.0.1:$PORT/api/config" >/dev/null \
  && echo "后台存活确认 OK" \
  || { echo "后台无响应，看 logs/admin.log"; exit 1; }

# ---- 跑隧道（前台，Ctrl+C 即停隧道；后台停用 bash manage.sh stop admin） ----
echo "启动隧道（Ctrl+C 停止隧道）。公网入口见 ~/.cloudflared/config.yml 里的 hostname。"
exec "$CLOUDFLARED" tunnel run dingtalk-admin
