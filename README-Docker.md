# Docker 部署（钉钉群报修工单系统 v4.1）

镜像组成：`python:3.13-slim`（主系统 + 管理后台）+ 官方 Node 20（运行 `dws` CLI）。
镜像只装项目代码与依赖，**不含任何钉钉登录凭证**——登录由你在目标机上自行完成，
凭证留存在目标机目录里（默认 `./dws-data`）。`dws` CLI 版本锁定 `1.0.61`。

## 0. 前置条件

- 目标机已安装 Docker（含 docker compose v2）。
- **先停掉宿主机旧进程**：`bash manage.sh stop`。同一钉钉账号双跑会重复消费群消息（重复建单/回复）。

## 1. 打包镜像

在开发机上构建并导出（离线传到目标机），或直接在目标机上构建：

```bash
# 方式 A：目标机有外网时，直接把项目拷过去现场构建
docker build -t dingtalk-tickets .

# 方式 B：离线交付——开发机构建后导出 tar，拷到目标机 load
docker build -t dingtalk-tickets .
docker save dingtalk-tickets | gzip > dingtalk-tickets.tar.gz
# 目标机：
docker load < dingtalk-tickets.tar.gz
```

> 构建已适配国内网络：npm 源默认走 npmmirror（官方源会卡死），可用
> `docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org .` 切回官方。

## 2. 启动

```bash
docker compose up -d
docker compose logs -f main        # 看主系统日志
```

- 主系统：监听群消息、建单、催办、归档、看板同步。
- 管理后台：<http://127.0.0.1:8899>（默认无密码；公网暴露前在 compose 设 `ADMIN_PASSWORD`）。

## 3. 登录钉钉（你自行处理，凭证留存目标机）

```bash
docker compose run --rm main dws auth login   # 扫码/设备流登录，凭证留存目标机，只需一次
docker compose restart main
```

Linux 容器里凭证分落两处（compose 已分别挂到本机目录）：

| 容器内 | 本机留存 | 内容 |
|---|---|---|
| `/data/dws` | `./dws-data` | profile/身份配置（`DWS_CONFIG_DIR` 指向） |
| `/root/.local/share/dws-cli` | `./dws-keychain` | **token 密文**（Linux 固定路径，不跟随 `DWS_CONFIG_DIR`） |

两个目录都必须留存，重建/升级镜像不影响登录态；镜像本身不保存任何凭证。

**从本机（macOS）迁移登录态**（可选，不想重新扫码时用）：

```bash
# 本机导出（token 本体在 macOS 钥匙串 + ~/Library/Application Support/dws-cli 密文，
# export 会打成 Linux 可用的认证包，不含明文）：
dws auth export -o dws-auth.tar.gz
# 目标机导入：
docker compose run --rm main dws auth import -i /路径/dws-auth.tar.gz
dws auth status   # 确认 refresh token 有效
```

> 两个凭证目录含敏感材料，已加入 `.gitignore` 与 `.dockerignore`，严禁提交或打进镜像。

## 4. 数据与配置

| 内容 | 位置 | 说明 |
|---|---|---|
| 工单库 / 群配置 | `./data` | 直接复用宿主机现有数据，无需迁移（`groups.json` 启动强校验，必须存在） |
| 日志 | `./logs` | 与宿主机共用 |
| 归档图片/Markdown | `./archives` | 与宿主机共用 |
| 密钥与开关 | `.env` | 以只读方式挂载 + 注入进程环境，**不会打进镜像**；真实环境变量优先于 .env |

时间敏感逻辑（SLA、时效提醒）按东八区计算，镜像已固定 `TZ=Asia/Shanghai`。

## 5. 常用运维

```bash
docker compose restart                 # 重启
docker compose build && docker compose up -d   # 改代码后升级
docker compose exec main bash          # 进容器
docker compose run --rm main pytest tests/ -q  # 容器内自测
docker compose down                    # 停止并移除容器（数据卷与挂载不受影响）
```

`main` 服务以 `SIGINT` 停止（等价 Ctrl-C），`main.py` 会走正常收尾：取消任务、关闭归档、落库退出。

## 6. 可选：淘宝对账共享表

共享表路径默认指向 macOS 桌面（`config.py` 中 `ORDER_STORE_TABLE_PATH` 等），
容器内不存在会导致订单登记写共享表失败（调度器会按 300s 间隔重试，不影响其他功能）。
需要该功能时按 `docker-compose.yml` 中的注释挂载 `淘宝对账` 目录并设置对应环境变量。

## 7. 直跑（不用 compose）

```bash
docker build -t dingtalk-tickets .
docker run -d --name dingtalk-tickets \
  -v "$PWD/.env":/app/.env:ro -v "$PWD/data":/app/data \
  -v "$PWD/logs":/app/logs -v "$PWD/archives":/app/archives \
  -v "$PWD/dws-data":/data/dws \
  dingtalk-tickets
# 管理后台另起一个容器：
docker run -d --name dingtalk-admin -p 8899:8899 \
  -v "$PWD/data":/app/data -v "$PWD/logs":/app/logs \
  dingtalk-tickets python admin/server.py --port 8899 --host 0.0.0.0
```
