# LangGraph 重写后的系统运行流程

```mermaid
flowchart TD
    %% ────────────── 钉钉接入与异步运行骨架 ──────────────
    subgraph Runtime["现有 asyncio 运行骨架（保留）"]
        USER["店长 / 工程师发送群消息"]
        LISTENER["event_listener<br/>监听多个钉钉群"]
        NORMALIZER["event_normalizer<br/>消息、角色、引用、图片标准化"]
        INBOX[("SQLite inbox_messages")]
        WORKER["InboxWorker<br/>群内串行 · 跨群并行"]
        SCHEDULER["SchedulerWorker<br/>SLA、订单、过期任务"]

        USER --> LISTENER --> NORMALIZER --> INBOX --> WORKER
    end

    %% ────────────── LangGraph 外层调度 ──────────────
    subgraph Dispatcher["LangGraph：群级消息调度图"]
        ENTRY["message_entry<br/>读取 Inbox 消息"]
        PREPARE["prepare_context<br/>加载角色、引用、附件、待确认动作"]
        IMAGE{"包含图片？"}
        VISION["既有图片归档与视觉解析"]
        ROUTE["ticket_router<br/>关键词 + 编号 + 引用 + 上下文 + LLM"]
        ROUTE_RESULT{"路由结果"}

        NEW_TICKET["调用既有 Executor 建单<br/>创建 ticket_id"]
        EXISTING["定位已有 ticket_id"]
        CLARIFY["保存 PendingAction<br/>提示选择具体工单"]
        IGNORE["记录审计并静默完成"]
        THREAD["生成或恢复<br/>thread_id = ticket:{ticket_id}"]

        ENTRY --> PREPARE --> IMAGE
        IMAGE -->|是| VISION --> ROUTE
        IMAGE -->|否| ROUTE

        ROUTE --> ROUTE_RESULT
        ROUTE_RESULT -->|新故障| NEW_TICKET --> THREAD
        ROUTE_RESULT -->|已有工单| EXISTING --> THREAD
        ROUTE_RESULT -->|多工单歧义| CLARIFY
        ROUTE_RESULT -->|闲聊或无业务动作| IGNORE
    end

    WORKER --> ENTRY

    %% ────────────── 每张工单独立 Agent ──────────────
    subgraph TicketAgent["LangGraph：工单 Agent（每张工单独立 checkpoint）"]
        LOAD["load_ticket_state<br/>加载工单及 Agent 阶段"]
        DETECT["detect_event<br/>关键词快路径 + LLM 语义识别"]
        NORMALIZE["normalize_and_validate<br/>协议、角色、编号、状态校验"]
        DECIDE{"agent.stage + 事件类型"}

        subgraph SelfService["店长与 LLM 自助排障循环"]
            RETRIEVE["RAG 检索<br/>故障、设备、历史方案"]
            GENERATE["LLM 生成下一步维修指导"]
            GUIDE["保存记录并通知店长"]
            WAIT_MANAGER[["interrupt<br/>等待店长反馈"]]
            FEEDBACK{"店长反馈"}

            RETRIEVE --> GENERATE --> GUIDE --> WAIT_MANAGER
            WAIT_MANAGER --> FEEDBACK
            FEEDBACK -->|未解决 / 新现象| RETRIEVE
        end

        subgraph EngineerFlow["工程师跟进流程"]
            HANDOFF["联系并指派工程师"]
            WAIT_ACCEPT["等待接单"]
            ENGINEER_EVENT["关键词 + LLM<br/>识别工程师状态"]
            ENGINEER_STATE{"处理状态"}

            PROCESSING["处理中"]
            WAIT_PARTS["等待配件 / 订单"]
            REPAIR_DONE["工程师报告完工"]
            WAIT_CONFIRM[["等待店长确认"]]
            CONFIRM{"店长确认结果"}

            HANDOFF --> WAIT_ACCEPT --> ENGINEER_EVENT --> ENGINEER_STATE
            ENGINEER_STATE -->|已接单 / 处理中| PROCESSING
            PROCESSING --> ENGINEER_EVENT
            ENGINEER_STATE -->|等待配件| WAIT_PARTS
            WAIT_PARTS --> ENGINEER_EVENT
            ENGINEER_STATE -->|报告完工| REPAIR_DONE --> WAIT_CONFIRM
            WAIT_CONFIRM --> CONFIRM
            CONFIRM -->|没修好| PROCESSING
        end

        COMPLETE["调用既有 Executor<br/>完成工单"]
        CANCEL["调用既有 Executor<br/>取消 / 停修"]
        QUERY["执行查询 / 补充 / 诊断 / 维修方案"]
        PENDING["创建确认或澄清 PendingAction"]
        GRAPH_END(("本轮结束"))

        LOAD --> DETECT --> NORMALIZE --> DECIDE

        DECIDE -->|新建后开始排障| RETRIEVE
        DECIDE -->|店长继续反馈| FEEDBACK
        DECIDE -->|要求联系工程师| HANDOFF
        DECIDE -->|工程师消息| ENGINEER_EVENT
        DECIDE -->|确认修好| COMPLETE
        DECIDE -->|取消或停修| CANCEL
        DECIDE -->|其他既有工单动作| QUERY
        DECIDE -->|需要确认或澄清| PENDING

        FEEDBACK -->|已经解决| COMPLETE
        FEEDBACK -->|联系工程师| HANDOFF
        CONFIRM -->|确认修好| COMPLETE

        COMPLETE --> GRAPH_END
        CANCEL --> GRAPH_END
        QUERY --> GRAPH_END
        PENDING --> GRAPH_END
        GUIDE --> GRAPH_END
        PROCESSING --> GRAPH_END
        WAIT_PARTS --> GRAPH_END
    end

    THREAD --> LOAD

    %% ────────────── 既有业务组件和数据 ──────────────
    subgraph Existing["现有业务组件（内部规则不改）"]
        PROTOCOL["ticket_semantics.v4.json"]
        CLASSIFIER["SemanticClassifier"]
        ROUTER_COMPONENT["TicketRouter"]
        VALIDATOR["validator"]
        EXECUTOR["TicketCommandExecutor"]
        REPOSITORY["TicketRepository"]
        DB[("SQLite 业务数据")]
        OUTBOX[("notification_deliveries")]
        NOTIFIER["Notifier / dws sender"]
        AITABLE["钉钉 AI 表格与管理后台"]

        EXECUTOR --> REPOSITORY --> DB
        EXECUTOR --> OUTBOX --> NOTIFIER
        DB --> AITABLE
    end

    DETECT -.调用.-> CLASSIFIER
    ROUTE -.调用.-> ROUTER_COMPONENT
    NORMALIZE -.读取.-> PROTOCOL
    NORMALIZE -.调用.-> VALIDATOR
    NEW_TICKET -.调用.-> EXECUTOR
    COMPLETE -.调用.-> EXECUTOR
    CANCEL -.调用.-> EXECUTOR
    QUERY -.调用.-> EXECUTOR
    GUIDE -.写入通知.-> OUTBOX
    HANDOFF -.写入通知.-> OUTBOX

    %% ────────────── SLA 和结果回收 ──────────────
    SCHEDULER --> DB
    SCHEDULER --> OUTBOX
    NOTIFIER --> USER

    GRAPH_END --> RESULT{"Graph 执行结果"}
    CLARIFY --> RESULT
    IGNORE --> RESULT
    RESULT -->|成功| DONE["Inbox 标记 COMPLETED"]
    RESULT -->|可重试错误| RETRY["RETRY_PENDING<br/>到期重新进入 Graph"]
    RESULT -->|超过重试次数| DEAD["DEAD_LETTER"]
```

## 设计约束

- 一个群使用一个消息入口和群级路由器。
- 每张工单复用同一份编译后的 LangGraph，以 `ticket:{ticket_id}` 隔离 checkpoint 和对话状态。
- 店长排障主循环为 `RAG → LLM → 指导 → 等待反馈 → 再检索`。
- 转交工程师后，同一个工单 Agent 继续跟踪接单、处理、配件、完工与店长确认。
- SLA 继续由现有 `SchedulerWorker` 扫描 SQLite，不在 LangGraph 节点内长时间等待。
- SQLite 保存业务事实，LangGraph checkpoint 保存流程位置。
- 现有执行器、协议、数据库、Outbox 和通知机制保持复用。
