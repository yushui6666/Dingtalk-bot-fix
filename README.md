# DingTalk Repair Multi-Agent

一个基于 **LangGraph 多 Agent 编排** 的钉钉群报修系统。

系统采用 **Supervisor + Ticket Agents** 模式：群级 Dispatcher Graph 是 Supervisor，负责理解消息上下文并找到目标工单；每张工单都是一个独立的 Ticket Agent，持续负责自己的自助排障、工程师跟进、SLA 和完工确认。

同一个群可以同时运行多张工单。消息先在同一群内并发完成语义识别，再由群级闸门按 `(sent_at, message_id)` 顺序提交路由；不同工单随后可并行运行 Ticket Agent，同一工单严格有序。所有 Ticket Agent 复用同一份 LangGraph 定义，但分别使用自己的 `thread_id` 和 checkpoint，因此上下文、阶段和处理进度不会串单。

## LangGraph 多 Agent 编排图

```mermaid
flowchart TB
    M[钉钉群消息] --> W[InboxWorker<br/>不同群协程并行]
    W --> GA[群 A：消息识别并行]
    W --> GB[群 B：消息识别并行]
    GA --> QA[群 A：OrderedCommitGate<br/>按 sent_at + message_id 有序提交]
    GB --> QB[群 B：OrderedCommitGate<br/>按 sent_at + message_id 有序提交]

    subgraph LG[LangGraph 多 Agent 编排层]
        QA --> D[Supervisor<br/>Dispatcher Graph]
        QB --> D
        D --> R{Conditional Edge<br/>消息属于哪张工单?}

        R -->|工单 12| A1[Ticket Agent #12<br/>排障 · 跟进 · 确认]
        R -->|工单 19| A2[Ticket Agent #19<br/>排障 · 跟进 · 确认]
        R -->|其他工单| AN[Ticket Agent #N<br/>排障 · 跟进 · 确认]

        A1 <--> C1[(Checkpoint<br/>ticket:12)]
        A2 <--> C2[(Checkpoint<br/>ticket:19)]
        AN <--> CN[(Checkpoint<br/>ticket:N)]
    end

    A1 --> DB[(tickets.db<br/>业务真相源)]
    A2 --> DB
    AN --> DB
    S[SchedulerWorker<br/>SLA · 订单 · 提醒] --> DB

    classDef supervisor fill:#6c5ce7,color:#fff,stroke:#4b3fc1
    classDef agent fill:#0984e3,color:#fff,stroke:#0769b2
    classDef checkpoint fill:#e8f4ff,color:#16425b,stroke:#74b9ff
    class D supervisor
    class A1,A2,AN agent
    class C1,C2,CN checkpoint
```

图中的 `Ticket Agent #12`、`#19` 和 `#N` 是同时存在的逻辑 Agent。消息识别可并行，但路由提交不乱序；提交完成后，不同工单可并行执行，同一工单由 `GraphRuntime` 的工单级锁串行恢复状态。它们没有各自启动进程，而是由 LangGraph 按 `thread_id` 恢复对应状态。

## Agent Loop：工单 Agent 的循环

上面那张图回答的是“消息去哪张工单”，这一张回答的是“到了工单之后 Agent 怎么运转”。Ticket Agent 不是跑一次就结束的函数，而是一个被消息反复唤醒的循环：每轮只处理一条消息，处理完把自己的位置存进 checkpoint 就休眠，等下一条消息到来再转一圈。

```mermaid
flowchart TB
    WAKE(["① 被消息唤醒<br/>按 thread_id = ticket:N 恢复 checkpoint"])
    LOAD["② load_ticket_state<br/>重读 tickets.db，校正 agent_stage"]
    DETECT["③ detect_ticket_event<br/>这条消息是什么事件"]
    ROUTE{"④ route_event"}
    TROUBLE["⑤ build_retrieval_query<br/>→ retrieve_knowledge<br/>→ generate_guidance"]
    NOTIFY["⑤ persist_and_notify_guidance<br/>落库 + 群内发出排障建议<br/>stage = WAITING_MANAGER"]
    HAND["⑤ handoff_engineer<br/>通知已转工程师<br/>stage = ENGINEER_TRACKING"]
    SYNC["⑤ sync_lifecycle<br/>按工单业务状态同步 stage"]
    SLEEP(["⑥ 本轮 END<br/>checkpoint 落盘，Agent 休眠"])

    WAKE --> LOAD --> DETECT --> ROUTE
    ROUTE -->|"NEW_ISSUE / UNSOLVED"| TROUBLE
    TROUBLE --> NOTIFY
    ROUTE -->|"HANDOFF"| HAND
    ROUTE -->|"RESOLVED / ENGINEER_* / PASSIVE / CLOSED"| SYNC
    NOTIFY --> SLEEP
    HAND --> SLEEP
    SYNC --> SLEEP
    SLEEP -.->|"下一条属于本工单的消息"| WAKE

    classDef step fill:#0984e3,color:#fff,stroke:#0769b2
    classDef idle fill:#e8f4ff,color:#16425b,stroke:#74b9ff
    class LOAD,DETECT,ROUTE,TROUBLE,NOTIFY,HAND,SYNC step
    class WAKE,SLEEP idle
```

循环的两端各自承担一半连续性：

- **唤醒端（②）**：`load_ticket_state` 每次被唤醒都重读 `tickets.db`，用真实工单状态校正 `agent_stage`。checkpoint 只回答“上次停在哪一步”，业务事实永远以数据库为准。
- **休眠端（⑥）**：三个分支最终都收敛到本轮 END，checkpoint 落盘；下一条属于该工单的消息由 Dispatcher 送进来，循环从①重新开始。
- **失败口**：工单不存在时 `load_ticket_state` 直接结束本轮，不会进入后续节点。

跨多轮看，阶段也不是单向推进的：店长说“还是不行”（`UNSOLVED`）会把 Agent 从 `WAITING_MANAGER` 拉回 `SELF_SERVICE` 再生成一轮建议；店长说“没修好”（`MANAGER_REJECTED`）会把 Agent 从 `WAITING_CONFIRM` 拉回 `ENGINEER_TRACKING`。进入 `CLOSED` 后循环不再产生动作，只由 `sync_lifecycle` 收尾。

## 主要能力

- **自然语言报修**：从群消息提取主题、位置、故障描述、时效等字段并创建工单。
- **单群多工单**：通过完整编号、短编号、消息引用、用户上下文和语义匹配确定消息归属。
- **每单独立 Agent**：所有工单复用同一份 Ticket Agent Graph，通过 `ticket:{ticket_id}` 隔离上下文。
- **自助排障循环**：新建工单后由 LLM 生成检查建议，并结合关键词和 LLM 判断店长反馈。
- **工程师跟进**：店长提出“联系工程师”后，Agent 转入工程师跟进阶段，继续记录诊断、维修方案、订单、超时原因和完工信息。
- **完工确认**：工程师报完工后进入待店长确认状态；店长可以确认修好，也可以反馈未修好并退回处理。
- **SLA 调度**：支持响应提醒、升级通知、临近到期、工单超时和确认超时。
- **订单协作**：记录采购订单，读取共享表中的物流状态，到货后持续提醒维修。
- **图片附件**：归档群内图片，并可使用独立视觉模型提取故障信息。
- **AI 表格看板**：把 SQLite 中的工单同步到钉钉 AI 表格，用于看板和统计。
- **三种运行模式**：支持 `SHADOW`、`ASSISTED`、`PRODUCTION`。

## LangGraph 编排

LangGraph 实际承担消息节点流转、目标 Agent 选择和工单级 checkpoint 恢复；具体业务规则仍由原有业务能力层执行。

| LangGraph 组件 | 在项目中的作用 |
|---|---|
| `StateGraph` | 定义 Dispatcher 和 Ticket Agent 的节点、状态与流转关系 |
| Dispatcher Graph | 充当 Supervisor，每条群消息运行一次并选择目标 Ticket Agent |
| Conditional Edge | 根据业务处理结果和工单归属决定继续、结束或调用哪张工单 |
| Ticket Agent Graph | 处理单张工单的排障、转工程师、等待配件和完工确认 |
| `thread_id` | 将同一份 Ticket Agent Graph 实例化为多张互不串线的逻辑 Agent |
| Async SQLite Saver | 持久化每个 Ticket Agent 的阶段、摘要和最近运行结果 |

多 Agent 的关键不是复制多份代码，而是用同一张 Ticket Agent Graph 创建多个独立状态空间：

```text
同一群消息
  └─ Dispatcher Graph（Supervisor）
       ├─ ticket:12 → Ticket Agent #12
       ├─ ticket:19 → Ticket Agent #19
       └─ ticket:27 → Ticket Agent #27
```

Dispatcher 不保存跨消息的长期对话。Ticket Agent 才拥有工单级记忆，每张工单固定使用：

```text
thread_id = ticket:{ticket_id}
```

LangGraph 保存的是“Agent 下一步应该做什么”，例如继续排障、等待店长、跟进工程师或等待确认。工单状态、消息、订单和 SLA 仍由 `tickets.db` 管理，现有语义协议、Router、Validator 和 Executor 继续决定业务结果。

Agent 阶段包括：

| 阶段 | 含义 |
|---|---|
| `TRIAGE` | 进入工单并判断当前事件 |
| `SELF_SERVICE` | 正在生成或执行自助排障建议 |
| `WAITING_MANAGER` | 等待店长反馈检查结果 |
| `ENGINEER_TRACKING` | 已转工程师，持续跟进处理进度 |
| `WAITING_PARTS` | 等待配件、订单或其他外部条件 |
| `WAITING_CONFIRM` | 工程师已报完工，等待店长确认 |
| `CLOSED` | 工单已完成、取消或停止 |

Agent 每次被消息唤醒时都会重新读取业务数据库，以真实工单状态校正 checkpoint。完整消息、工单状态、SLA 和订单仍保存在业务数据库中。

## 一条报修如何处理

1. `event_listener` 通过 `dws` CLI 监听钉钉群消息。
2. `event_normalizer` 把原始事件转换成统一的 `NormalizedMessage`。
3. 消息写入 Inbox，`InboxWorker` 让跨群及群内识别并行，再由群级闸门按消息顺序提交。
4. Dispatcher Graph 恢复附件和上下文，然后调用现有业务管道。
5. 云端模型完成语义识别（全 AI 判断，关键词快路径已永久停用）。
6. Router 根据编号、引用、用户上下文和候选评分定位工单。
7. Validator 检查角色权限、必填字段和工单状态。
8. `TicketCommandExecutor` 在 SQLite 事务中执行建单或状态变更。
9. 消息被交给对应的 Ticket Agent Graph，更新该工单的 Agent 阶段和排障上下文。
10. 通知通过 Outbox 去重后发送到钉钉群。
11. `SchedulerWorker` 独立扫描业务数据库，处理 SLA、订单和看板同步。

模型只负责理解消息和生成建议，不能直接修改数据库，也不能绕过本地协议校验。

## 店长与 Agent 的交互

新故障创建工单后，Agent 会结合工单信息生成一轮排障建议。店长后续反馈由工单 Agent 内部的关键词优先识别，表达不明确时再交给 LLM 分类（这是 Agent 内部的反馈分类，与上节已永久停用的消息级关键词快路径无关）：

| 店长表达示例 | Agent 行为 |
|---|---|
| “清理以后还是不行” | 记录新现象，重新生成排障建议 |
| “已经恢复正常了” | 同步工单完成结果或当前生命周期 |
| “联系工程师处理吧” | 转入 `ENGINEER_TRACKING` |
| 普通闲聊或无法判断 | 不修改工单业务状态 |

在单群多工单场景下，用户可以通过工单编号、回复某条已归档消息或先选择工单来明确上下文，避免把反馈写到其他工单。

## 语义识别：全 AI 判断

关键词快路径（`#报修` 等显式命令的本地解析）已于 2026-08-20 停用，并于 2026-09-16
明确为**永久停用**：包括 `#报修` 在内的所有消息统一交由云端模型判断，系统不再保留
本地规则兜底。

这意味着模型接口是硬依赖：模型不可用时，消息会按现有重试策略进入
`RETRY_PENDING`，累计失败后进入 `DEAD_LETTER`，不会有任何消息被本地规则接管。

`semantics/keyword_matcher.py` 仍然保留，但只服务于离线评测（`semantics/evaluator.py`、
`semantics/run_eval.py`）和单元测试，不在运行时决策链路中。

## RAG 状态

项目已经定义统一的 `KnowledgeRetriever` 接口，并在 Ticket Agent Graph 中保留以下节点：

```text
build_retrieval_query
  -> retrieve_knowledge
  -> generate_guidance
```

当前使用 `NullKnowledgeRetriever`，固定返回空文档。未配置知识库时，LLM 只依据工单信息和店长最新反馈生成通用建议，不会声称引用了知识库内容。

后续接入向量库、企业知识库或维修手册时，只需要实现新的 Retriever，无需修改 Graph 的主体流程。

## 数据与 checkpoint

系统使用两个独立的 SQLite 文件：

| 文件 | 用途 |
|---|---|
| `data/tickets.db` | 工单、消息、状态、SLA、订单、PendingAction 和 Outbox，是业务真相源 |
| `data/langgraph-checkpoints.sqlite` | Ticket Agent 的阶段和精简上下文 |

LangGraph checkpoint 不参与报表统计，也不替代工单数据库。即使 checkpoint 中的阶段与业务状态不一致，下一次运行也会以 `tickets.db` 为准重新校正。

## 工单生命周期

```text
ACTIVE
  -> ACTIVE_OVERDUE
  -> PENDING_CONFIRM
  -> COMPLETED

ACTIVE / ACTIVE_OVERDUE
  -> PENDING_NEGOTIATION
  -> ACTIVE

ACTIVE / PENDING_CONFIRM / PENDING_NEGOTIATION
  -> CANCELLED 或 STOPPED
```

- 工程师报完工后进入 `PENDING_CONFIRM`。
- 店长确认修好后进入 `COMPLETED`。
- 店长反馈未修好后返回处理中。
- `PENDING_NEGOTIATION` 用于时效暂时无法确定的工单。
- 已取消或停止的工单可以按现有协议重开。

## 技术栈

- Python 3.13+
- asyncio
- LangGraph 1.2
- SQLite / WAL
- httpx
- OpenAI-compatible Chat Completions API
- `dws` CLI
- openpyxl

## 快速开始

### 1. 创建环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 准备群配置

```bash
cp data/groups.example.json data/groups.json
```

在 `data/groups.json` 中配置群 ID、门店名称、店长、工程师、工程负责人、区域经理，以及 `openDingtalkId -> userId` 映射。

### 3. 准备环境变量

```bash
cp .env.example .env
```

至少需要配置模型接口：

```dotenv
LLM_ENABLED=true
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model

LANGGRAPH_ENABLED=true
LANGGRAPH_CHECKPOINT_PATH=data/langgraph-checkpoints.sqlite
RAG_ENABLED=false
```

文本模型使用 OpenAI-compatible Chat Completions 协议。视觉模型可以通过 `VISION_*` 单独配置。

### 4. 启动

直接启动主系统：

```bash
python main.py --mode PRODUCTION
```

也可以使用管理脚本同时管理主系统和管理后台：

```bash
bash manage.sh start
bash manage.sh status
bash manage.sh logs
bash manage.sh stop
```

运行模式：

| 模式 | 行为 |
|---|---|
| `PRODUCTION` | 按协议执行工单动作并发送通知 |
| `ASSISTED` | 模型来源的动作先等待人工确认 |
| `SHADOW` | 只记录语义决策，不修改工单或发送消息 |

可以只监听指定门店群：

```bash
python main.py --mode PRODUCTION --group "门店名称"
```

也可以指定其他群配置文件：

```bash
python main.py --mode PRODUCTION --groups-config /path/to/groups.json
```

## 常用配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `LANGGRAPH_ENABLED` | `true` | 是否启用 LangGraph 编排 |
| `LANGGRAPH_CHECKPOINT_PATH` | `data/langgraph-checkpoints.sqlite` | Agent checkpoint 文件 |
| `RAG_ENABLED` | `false` | RAG 预留开关，当前仍使用空检索器 |
| `LLM_ENABLED` | `true` | 是否启用文本模型 |
| `LLM_TIMEOUT_SECONDS` | `60` | 单次文本模型超时秒数 |
| `VISION_ENABLED` | `false` | 是否启用视觉模型 |
| `IMAGE_ARCHIVE_ENABLED` | `true` | 是否归档图片附件 |
| `RESPONSE_SLA_ENABLED` | `true` | 是否启用响应 SLA 提醒 |
| `AITABLE_SYNC_ENABLED` | `true` | 是否同步钉钉 AI 表格 |

完整配置见 [`config.py`](./config.py) 和 [`.env.example`](./.env.example)。

## 项目结构

```text
.
├── main.py                    应用入口与组件组装
├── pipeline.py                业务管道兼容入口
├── db.py                      SQLite schema、迁移和数据访问
├── notifier.py                Outbox 与钉钉通知
├── graphs/
│   ├── runtime.py             Graph 和 checkpoint 生命周期
│   ├── dispatcher_graph.py    每条消息的 Dispatcher Graph
│   ├── ticket_agent_graph.py  每张工单的 Ticket Agent Graph
│   ├── troubleshooting.py     关键词 + LLM 反馈识别与建议生成
│   └── retrieval/             RAG 接口和空检索器
├── semantics/                 语义协议、关键词、LLM 分类和校验
├── routing/                   多工单路由、选单上下文和 PendingAction
├── tickets/                   工单命令、仓储和事务执行器
├── workers/                   Inbox、Scheduler 和 AI 表格同步
├── images/                    图片归档与视觉分析
├── reconciling/               订单共享表协作
├── admin/                     管理后台
├── protocols/                 工单语义协议 JSON 与 Schema
├── scripts/                   导出、同步、回放和运维脚本
├── docs/                      业务规则、架构与流程文档
└── data/                      本地数据库和群配置，不提交仓库
```

## 语法检查

项目当前只执行 Python 语法检查：

```bash
python -m compileall -q .
```

## 相关文档

- [LangGraph 运行流程设计](./docs/superpowers/specs/2026-09-15-langgraph-runtime-design.md)
- [LangGraph 运行流程说明](./docs/langgraph-runtime-flow.md)
- [业务流程与工单生命周期](./docs/业务流程与工单生命周期.md)
- [SLA 架构与绩效扩展](./docs/SLA架构设计与绩效考核扩展.md)
- [Docker 部署说明](./README-Docker.md)
- [业务使用须知](./使用须知.txt)

## 当前边界

- RAG 只有接口和空实现，尚未接入真实知识库。
- 钉钉消息监听、通知和 AI 表格同步依赖本机已登录的 `dws` CLI。
- 项目采用单进程 asyncio 和 SQLite，适合当前门店群规模，不面向多实例同时写入。
- 原有业务状态、协议和执行器保持不变，LangGraph 主要负责流程规范化和按工单保存 Agent 上下文。
