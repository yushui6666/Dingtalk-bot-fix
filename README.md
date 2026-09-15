# 钉钉群报修工单系统（v4.1）

基于钉钉群聊的报修工单归档与处理系统：门店群内通过自然语言报修，系统自动建单、路由、催办、归档，并把工单同步到钉钉 AI 表格看板做可视化。

> **2026-08-24 四项改进**：① 编号硬校验（写错编号拒绝并列出候选，不再静默归属）② 回复静默化（只回复需要操作/确认的内容）③ 店长完成确认流（工程师报完工 → `PENDING_CONFIRM` → 店长「确认修好/没修好」，确认期聊天强制归档）④ 响应 SLA（超 1h 提醒责任方、超 4h 升级+单聊，每 4h 循环）。详见 [`docs/业务流程与工单生命周期.md`](./docs/业务流程与工单生命周期.md)。

> 详细设计见 [`计划书.md`](./计划书.md)，业务使用说明见 [`使用须知.txt`](./使用须知.txt)。

---

## 一、系统概览

- **部署形态**：单进程 asyncio 长驻服务 + SQLite（WAL）。
- **钉钉接入**：通过 `dws` CLI（钉钉工作台命令行）监听群消息、发送群通知、读写 AI 表格。
- **语义理解**：OpenAI-compatible 云端文本模型 + 独立视觉模型，均可用环境变量切换供应商。
- **流程编排**：LangGraph Dispatcher 处理每条消息；每张工单使用独立 checkpoint 的逻辑 Agent。
- **设计原则**：关键词 JSON 是唯一业务真相；大模型只负责"理解"，规则引擎负责"执行"；不确定时澄清而非猜测。
- **AI 表格看板**：本地 SQLite 为真相源，定时把工单同步到钉钉 AI 表格（Kanban 看板 + 仪表盘 + 工程师门店导航视图）。

## 二、架构

### 2.1 分层

```
钉钉群 ──dws event──▶ event_listener ──▶ event_normalizer ──▶ InboxWorker
                                                                    │
                                                                    ▼
                                                        LangGraph Dispatcher
                                                                    │
                                             ┌──────────────────────┴──────────────────────┐
                                             ▼                                             ▼
                               Ticket Agent (ticket:1)                       Ticket Agent (ticket:N)
                                             │                                             │
                                             └──────────────────────┬──────────────────────┘
                                                                    ▼
                                                现有 classifier/router/validator/executor
                                                                    │
                    ┌──────────────────────────────────────────────────┘
                    ▼
              SQLite (data/tickets.db)
                    │
                    ├── workers/inbox_worker   收件箱幂等处理 + 重试
                    ├── workers/scheduler      定时任务（SLA 催办/订单提醒/看板同步）
                    ├── notifier               Outbox 事务通知
                    └── workers/aitable_sync   工单同步到钉钉 AI 表格
```

### 2.2 消息处理链路

1. `event_listener` 为每个群启动 `dws event +listen-im` 子进程，NDJSON 逐行消费。
2. `event_normalizer` 把原始事件标准化为 `NormalizedMessage`（含角色、引用、图片附件）。
3. `pipeline` 保持原调用接口，把消息交给 Dispatcher Graph；图节点复用现有关键词、云端语义、本地协议校验、路由和执行器。
4. 消息归属工单后，以 `ticket:{ticket_id}` 恢复该工单的 Ticket Agent Graph。单群多工单共用图定义，各自保存排障摘要和处理阶段。
5. 自助排障采用关键词 + LLM 判断店长反馈。RAG 已预留统一接口，当前 `NullKnowledgeRetriever` 返回空文档，不需要外部知识库服务。
6. 图片附件先由 `images/archive` 归档，再由 `images/vision` 多模态解析，作为附件证据参与语义判断。
7. 所有外部通知写入 Outbox，事务提交后由 `notifier` 发送。

## 三、模块

| 模块 | 职责 |
|---|---|
| `main.py` | 入口：组装管道、并发启动监听/收件箱/调度器 |
| `event_listener.py` | 多群消息监听（dws listen-im 子进程） |
| `event_normalizer.py` | 事件 → 标准化消息 |
| `pipeline.py` | 核心处理管道：关键词/语义、路由、确认、执行编排 |
| `graphs/` | LangGraph Dispatcher、按工单隔离的 Agent、checkpoint 生命周期与空 RAG 接口 |
| `db.py` | SQLite 数据层：schema、迁移、事务、工单/收件箱/附件/待确认 |
| `models.py` | 数据模型与角色/状态枚举 |
| `config.py` | 全局配置（路径、群配置、模型、图片、看板同步） |
| `role_resolver.py` / `id_mapper.py` | 角色识别与 openDingtalkId↔userId 映射 |
| `notifier.py` | Outbox 通知投递 |
| `ordering.py` | 消息顺序与乱序保护 |
| `tickets/` | 工单命令、执行器、仓储 |
| `routing/` | 多工单路由、用户短期上下文、待确认动作 |
| `semantics/` | 协议加载/编译/校验、关键词匹配、语义分类、模型客户端、离线评测 |
| `workers/` | 收件箱 Worker、后台调度器、AI 表格同步 |
| `images/` | 图片安全归档 + 多模态视觉解析 |
| `reconciling/` | 淘宝订单↔门店共享表（xlsx）对账 |
| `scripts/` | 运维脚本（导出、导入、模拟、同步、评测） |
| `protocols/` | 语义协议 JSON 与 Schema |

## 四、快速开始

### 4.1 环境准备

- Python 3.13+
- `dws` CLI 已安装并在 `PATH` 中（登录过钉钉账号）
- 依赖安装：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4.2 配置

- 群与成员配置：`data/groups.json`（默认）或 `data/group-test.json`（测试），含每个门店群的角色 userId。
- 密钥与模型：通过环境变量注入，见 `config.py` 顶部（`LLM_*`、`VISION_*`、`AITABLE_SYNC_*`）。
- LangGraph：`LANGGRAPH_ENABLED=true`，checkpoint 默认写入 `data/langgraph-checkpoints.sqlite`；`RAG_ENABLED=false` 时使用空检索器。
- 群配置切换：启动时 `--test` 或 `--groups-config <path>`。

### 4.3 运行

```bash
# 生产模式（默认，按协议确认策略执行）
python main.py --mode PRODUCTION

# 辅助模式（模型来源动作一律待确认）
python main.py --mode ASSISTED

# 影子模式（只记录语义决策，不执行）
python main.py --mode SHADOW
```

### 4.4 语法检查

```bash
python -m compileall -q .
```

## 五、核心概念

### 5.1 工单生命周期

```
ACTIVE ──► ACTIVE_OVERDUE ──► PENDING_CONFIRM ──► COMPLETED
    │               │              ▲   │    ▲       ▲
    │               │              │   │    │       └── 店长本人 #完毕（直接完成）
    │               │              │   │    └────────── 店长「确认修好」
    └───────────────┴──────────────┘   └────────────── 店长「没修好」退回
      (ticket.reopen 作用于终态工单)
    └─────────────────────────────► CANCELLED
```

- 工程师报完工 → `PENDING_CONFIRM`（待店长确认，责任方切店长侧）；店长本人报完工直接 `COMPLETED`。
- 确认窗口内该群所有消息（含闲聊）强制归档进待确认工单。
- 同一群允许多张未完成工单并行；同一工单按 `(sent_at, message_id)` 稳定顺序单调应用。
- 乐观版本控制（`version`）防止并发覆盖。

### 5.2 语义协议

`protocols/ticket_semantics.v4.json` 定义动作、字段、权限、状态与匹配策略（唯一业务真相），由 `semantics/protocol_compiler` 从业务源编译，`protocol_loader` 加载校验。模型只能从协议中选择，不能创造新动作/字段/状态。

### 5.3 收件箱状态机

```
RECEIVED → CLASSIFYING → CLASSIFIED → ROUTING → EXECUTING → COMPLETED
```

异常态：`WAITING_CONFIRMATION / RETRY_PENDING / MODEL_FAILED / VALIDATION_REJECTED / DEAD_LETTER`。Inbox Worker 是唯一重试所有者。

### 5.4 AI 表格看板同步

`workers/aitable_sync.py` 把本地工单同步到钉钉 AI 表格「报修工单」表：

- 本地为真相源：新增/更新自动同步，本地删除自动镜像删除（`AITABLE_SYNC_PRUNE`）。
- 工程师字段按门店群配置自动填充，支撑「工程师 → 门店 → 工单」分组视图。
- 调度器默认每 120 秒同步一次（`AITABLE_SYNC_ENABLED`、`AITABLE_SYNC_INTERVAL_SECONDS`）。
- 手动同步：`python scripts/sync_tickets_to_aitable.py --prune`（`--dry-run` 预览、`--full` 全量）。

## 六、业务规则要点

- **角色**：店长（MANAGER）/ 工程师（ENGINEER）/ 其他成员（OTHER）/ 工程负责人（LEADER），同一 userId 不得重叠。
- **编号硬校验**（2026-08-24 #1）：显式编号在本群（含终态工单）查不到 → 硬拒绝并回「未找到工单 + 候选短编号 + 近似建议」；编号存在但状态不允许 → 明确提示状态不符。
- **回复静默化**（2026-08-24 #2）：补充、故障判断、维修方式、超时原因、取消、停修、重开、选单、订单登记等纯告知回执一律静默；保留建单一行式回执、完工确认请求、错误/澄清提示、SLA 提醒与查询结果。
- **完成确认流**（2026-08-24 #3）：工程师报完工 → `PENDING_CONFIRM`；店长「确认修好」→ `COMPLETED`（留痕 `completed_confirm_by/at`）；「没修好」→ 退回 `ACTIVE` 并通知工程师；确认窗口聊天强制归档。cancel/stop 可作用于待确认工单。
- **响应 SLA**（2026-08-24 #4）：责任方超 1h 群内提醒一次，超 4h 升级（群内 + 对升级对象单聊）各一次/周期，不循环；工程师侧升级工程总监A（`RESPONSE_SLA_ENGINEER_ESCALATE_USER_ID`，未配置回退群配置 `engineering_leader_id`），店长侧升级区域经理；`PENDING_CONFIRM` 的店长确认超时同样适用。
- **时效 SLA**：1 / 3 / 7 天，临近到期与超时提醒。
- **完成校验**：需存在故障判断、维修方式、采购场景下的有效订单号，且全部超时周期已解释。
- **订单协作**：淘宝订单号写入共享表（xlsx），签收后开始计时、自签收次日起每日提醒直至完成（不再自动延期；签收当天只发一次性「开始计时维修」通知）。

## 七、目录说明

```
data/         SQLite 数据库、群配置、导出文件（.gitignore）
archives/     归档 Markdown/JSON 与图片附件（.gitignore）
logs/         运行日志（.gitignore）
protocols/    语义协议与 Schema（入库）
tests/        pytest 测试
docs/         业务文档与测试用例
```

> 仓库原则：**只追踪程序文件，程序产出不追踪**。`dingtalk.db`/`*.db-shm|wal`、`框架/`、`docs/上线前已处理事项.md`、`scripts/` 下 6 个一次性脚本（`export_knowledge_base`/`export_tickets_csv`/`link_engineer_msg_to_ticket30`/`manual_create_*`/`replay_cid*`）、`data/`/`logs/`/`archives/` 均已在 `.gitignore` 中忽略。
