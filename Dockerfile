# syntax=docker/dockerfile:1
# ============================================================================
# 钉钉群报修工单系统 v4.1 — 容器镜像
#
# 构建：  docker build -t dingtalk-tickets .
# 运行：  推荐配合同目录 docker-compose.yml 使用（详见 README-Docker.md）
#
# 组成：
#   - python:3.13-slim  主系统（main.py，asyncio 长驻）+ 管理后台（admin/server.py）
#   - node:20           dws CLI（npm 包 dingtalk-workspace-cli）的运行时；
#                       监听群消息 / 发送通知 / 读写 AI 表格全部经它完成
# ============================================================================

# ── 阶段 1：取官方 Node 运行时 ──────────────────────────────────────────────
# node:20-slim 即 bookworm（与下方 python 的 -bookworm 同版），/usr/local 整树复制
# 文件名互不冲突、可安全合并；不用 -bookworm 后缀 tag 以兼容部分国内镜像加速器
FROM node:20-slim AS node

# ── 阶段 2：运行时 ─────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm

# 并入 Node + npm（dws CLI 依赖）
COPY --from=node /usr/local /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    # SLA/时效全部按本地时间计算，时区固定东八区
    # dws 登录态目录固定到卷上（config 可用 DWS_CONFIG_DIR 覆盖默认 ~/.dws），容器重建不丢登录
    DWS_CONFIG_DIR=/data/dws \
    # dws 已装入 PATH，与宿主机 manage.sh 的取用方式一致
    DWS_CMD=dws

# 时区数据 + sqlite3 命令行（运维排查/手工修数据用）+ unzip（dws 安装脚本解压 skills 包必需）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata sqlite3 unzip \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# npm 源可覆盖构建：docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org .
# 默认 npmmirror（国内网络实测官方源拉 dws 的平台二进制会长时间卡死）
ARG NPM_REGISTRY=https://registry.npmmirror.com

# 先装 Python 依赖，利用层缓存（改代码不触发重装）
COPY requirements.txt ./
# fastapi/uvicorn 为管理后台依赖（requirements.txt 未含，manage.sh 在宿主机也是单独检测）；
# pytest 随镜像安装，便于容器内自测
RUN pip install --no-cache-dir -r requirements.txt fastapi uvicorn

# dws CLI：版本与宿主机已验证的 1.0.61 对齐，升级时与宿主机 npm 包同步改这里
RUN npm config set registry "$NPM_REGISTRY" \
    && npm install -g --no-audit --no-fund dingtalk-workspace-cli@1.0.61 \
    && npm cache clean --force

# 再拷代码（.dockerignore 已排除 .env / data / logs / archives 等敏感与运行时内容）
COPY . .

# 运行时目录兜底（compose 挂载宿主机 data/logs/archives 后此处只是占位）
RUN mkdir -p data logs archives /data/dws

EXPOSE 8899

# 默认启动主系统；管理后台见 compose 的 admin 服务。
# docker run 直跑示例（凭证留存本机两个目录，镜像不含任何凭证）：
#   docker run -d --name dingtalk-tickets \
#     -v "$PWD/.env":/app/.env:ro -v "$PWD/data":/app/data \
#     -v "$PWD/logs":/app/logs -v "$PWD/archives":/app/archives \
#     -v "$PWD/dws-data":/data/dws \
#     -v "$PWD/dws-keychain":/root/.local/share/dws-cli \
#     dingtalk-tickets
CMD ["python", "main.py", "--mode", "PRODUCTION"]
