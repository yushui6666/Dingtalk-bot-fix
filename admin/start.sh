#!/bin/bash
# 启动管理后台（本机使用，前台运行，Ctrl+C 停止）
# 密码 / 端口 / 监听地址 / 数据库路径都在 admin/.env 里设置，
# 这里只负责挑一个带 fastapi 的解释器再把参数交给 server.py。
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# 选一个带 fastapi/uvicorn 的解释器（与 manage.sh / start-tunnel.sh 逻辑一致）。
# 注意：.venv 里没有 fastapi，所以不能直接裸用 python3。
PYTHON=""
for cand in "$DIR/.venv/bin/python" "/opt/homebrew/bin/python3" python3; do
  if "$cand" -c "import fastapi, uvicorn" 2>/dev/null; then PYTHON="$cand"; break; fi
done
[ -z "$PYTHON" ] && PYTHON="python3"

# 可选：第一个参数覆盖端口。不传则用 admin/.env 的 ADMIN_PORT。
# 密码由 server.py 自行解析（真实环境变量 > admin/.env > data/.admin_password），
# 启动横幅会报告密码来源，不再打印任何伪造的密码提示。
if [ -n "$1" ]; then
  exec "$PYTHON" admin/server.py --port "$1"
else
  exec "$PYTHON" admin/server.py
fi
