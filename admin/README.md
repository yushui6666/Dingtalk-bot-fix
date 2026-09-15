# 管理后台

直接操作 `data/tickets.db` 的可视化后台。

## 启动

配置（密码 / 端口 / 监听地址 / 数据库路径）统一放在 **`admin/.env`**，改完需重启生效：

```bash
bash dingtalk_script/admin/start.sh            # 前台启动（本机访问，Ctrl+C 停止）
bash dingtalk_script/admin/start-tunnel.sh     # 隧道模式：对外走 cloudflared（必须有密码）
bash manage.sh start                           # 主系统 + 后台一起后台运行
```

`admin/.env` 关键项：

| 键 | 说明 |
|---|---|
| `ADMIN_PASSWORD` | 登录密码。留空则沿用 `data/.admin_password`；**两处都空 = 免密**（登录框不出现） |
| `ADMIN_HOST` | 默认 `127.0.0.1`（本机 + 隧道）；填 `0.0.0.0` 会让局域网内任何设备都能访问 |
| `ADMIN_PORT` | 默认 `8899` |
| `DB_PATH` | 留空用 `data/tickets.db`；可指向某个备份来查看历史库（注意增删改会作用到该库） |

优先级：**真实环境变量 > `admin/.env` > `data/.admin_password`**，所以命令行也能临时覆盖：

```bash
ADMIN_PASSWORD=临时密码 python3 dingtalk_script/admin/server.py --port 8899
```

浏览器打开: **http://127.0.0.1:8899**

> ⚠️ **没有"默认密码"这回事**——历史上 README / 登录框 / `start.sh` 里写的 `admin123` 纯属误导，代码从未使用过该值。
> ⚠️ `admin/.env` 已被 `.gitignore` 忽略（`.env` 规则匹配任意层级），但仍不要把密码提交进 git 或写进文档。

## 功能

- **仪表盘**: 工单/消息/订单统计 + 最近工单 + 快捷入口
- **🎫 工单管理（最常用）**: 左侧顶部「＋ 新建工单」/「工单管理」
  - 新建：选门店 → 填主题/位置/问题描述 → 选时效（1天/3天/7天/待商榷）→ 创建；
    编号自动生成（店名-主题-时效-序号），走 `TicketRepository.create_ticket`，与群内建单规则完全一致
  - 编辑状态：卡片点「编辑状态」→ 六选一（进行中/待确认/待商榷/已完成/已取消/已停修）；
    副作用与主系统对齐：终态写 `closed_at` 与对应留痕（`admin-manual`）+ 关责任周期 + 清用户上下文，从终态切回进行中按重开处理（`closed_at` 清空、`reopen_count`+1、清 SLA 去重）
  - 删除：卡片点「删除」→ 先展示关联数据（消息/判断/方案/订单等计数）→ 确认后级联删除并整库备份到 `data/tickets.db.bak_admin_del_<id>_<时间>`
  - 卡片列表支持编号/门店/主题搜索 + 状态筛选
  - **导出 Excel**（`admin/exports.py`，只读，取月份下拉，默认上一个自然月）
    - 「导出 SLA」→ 单 sheet「工单SLA状态」，口径与群内时效提醒同源（取 `tickets.status`）
    - 「导出绩效」→ 「按人汇总」+ **每位工程师一张 sheet（表内即该人的相关工单）** +
      「未归属工单」+「问题台账」；归属按实际提交人（版本表 `engineer_id`），
      一人一 sheet 便于逐单核对，无法归属的工单单独成表并标成因
- **22 张表直接操作**: 左侧按业务分组，支持搜索、排序、分页
  - 双击单元格直接改，自动备份
  - 点击表头排序
  - 新增行（自增主键自动跳过）
  - 删除行、导出 CSV
- **SQL 控制台**: 任意 SQL，写操作自动备份 `data/tickets.db.bak_admin_*`
- **一键备份**: 顶部“备份数据库”

## 表分组

- 核心业务: tickets, groups, ticket_special_cases
- 消息链路: inbox_messages, messages, message_ticket_links, message_attachments, semantic_decisions
- 维修流程: diagnosis_versions, repair_method_versions, timeout_cycles, responsibility_cycles
- 订单/物流: order_monitor, taobao_orders, delivery_confirmations
- 系统/队列: pending_actions, action_executions, ticket_contexts, notification_deliveries, processed_events, schema_migrations

## 安全

- 写操作前自动 `shutil.copy2` 备份
- 所有 SQL/更新走同一 DB 文件，WAL 模式
- 登录密码在 `admin/.env` 的 `ADMIN_PASSWORD`（或环境变量）里设置，**没有默认密码**
  - 未设置任何密码时后台免密运行，启动横幅会打印醒目告警
  - 会话 token **每次启动随机生成**，重启后需重新登录（旧的登录凭据立即失效）
  - 未登录只能访问 `/api/config`，且它只返回"是否需要密码"，不含数据库路径
  - 不接受 `?token=` 形式，避免会话密钥写进访问日志与浏览器历史
