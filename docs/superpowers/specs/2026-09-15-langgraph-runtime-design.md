# LangGraph 运行流程重构设计

日期：2026-09-15

## 1. 目标

在不改变现有报修业务规则和外围运行骨架的前提下，用 LangGraph 重写单条消息的处理编排，使处理步骤、条件分支、多轮等待和失败恢复拥有明确的 state、node 与 edge。

本次重构必须保持以下行为兼容：

- `event_listener` 继续监听钉钉群并标准化消息。
- `InboxWorker` 支持跨群并行、同群识别并行；群级闸门按消息顺序提交，不同工单并行、同一工单有序。
- SQLite 业务表继续是工单事实的唯一来源。
- 现有语义协议、关键词匹配、模型分类、路由、角色权限、校验器和 `TicketCommandExecutor` 继续决定业务结果。
- `SchedulerWorker` 继续处理 SLA、订单状态、待确认过期和 AI 表格同步。
- `Notifier` 与事务型 Outbox 继续负责外部通知。
- `SHADOW`、`ASSISTED`、`PRODUCTION` 三种运行模式保持原有语义。
- `MessageProcessingPipeline.process(item) -> str` 保持兼容，调用方不需要修改接口。

## 2. 不在本次范围内的事项

- 不重新设计工单生命周期、角色权限、编号规则和 SLA 口径。
- 不把钉钉监听、Inbox 轮询或 Scheduler 改成 LangGraph 节点。
- 不为每张工单启动独立进程、线程或长期存活的 asyncio Task。
- 不替换 SQLite、Outbox、管理后台或 AI 表格同步。
- 不实现向量库、文档切分、召回排序或知识库管理。
- 不修改现有 LLM 供应商接口。

## 3. 总体架构

系统分为三层：

1. **异步运行层**：`event_listener`、`InboxWorker`、`SchedulerWorker`。这一层管理长驻任务、消息顺序和定时扫描。
2. **LangGraph 编排层**：群级 Dispatcher Graph 和工单级 Ticket Agent Graph。它们负责把现有处理步骤显式连接起来。
3. **业务能力层**：classifier、router、validator、pending service、executor、repository、notifier。LangGraph 节点调用这些既有组件，不在节点中复制业务规则。

一张工单对应一个逻辑 Agent。所有工单复用同一个已经编译的 Ticket Agent Graph，通过不同的 `thread_id` 隔离 checkpoint 与多轮上下文：

```text
thread_id = ticket:{ticket_id}
```

“独立 Agent”表示状态和上下文独立，不表示创建独立模型对象或常驻进程。

## 4. 状态所有权

### 4.1 业务状态

工单状态、消息、诊断、维修方案、订单、责任周期、SLA 和通知继续写入现有业务 SQLite。任何需要展示、统计或驱动外部动作的数据都必须进入现有业务表。

LangGraph checkpoint 不作为业务数据源，也不替代业务数据库。

### 4.2 Agent 阶段

Agent 阶段描述图下一次应该从哪里继续：

```text
TRIAGE
SELF_SERVICE
WAITING_MANAGER
ENGINEER_TRACKING
WAITING_PARTS
WAITING_CONFIRM
CLOSED
```

Agent 阶段保存在 LangGraph checkpoint 中。每次恢复时，`load_ticket_state` 都会重新读取业务数据库并校正阶段，避免 checkpoint 与真实工单状态漂移。

首版不向 `tickets` 表增加 `agent_stage` 字段。管理后台和报表继续读取原有 `ticket.status`。

### 4.3 短期与长期上下文

Ticket Agent State 只保存当前工单需要的精简上下文：

- 当前 Inbox 消息标识及标准化内容；
- `ticket_id`、`ticket_no`、`group_id`；
- 当前 Agent 阶段；
- 当前结构化语义事件；
- 当前路由、校验和执行结果；
- 最近一次维修指导及店长反馈摘要；
- 当前 Graph 运行结果和错误分类。

完整消息历史、工单版本和附件记录继续从 SQLite 按需读取，不把整个群的聊天历史持续追加进 checkpoint。

## 5. 群级 Dispatcher Graph

Dispatcher Graph 每条 Inbox 消息运行一次，不承担长期对话记忆。其职责是确定消息属于哪张工单，然后调用或恢复对应的 Ticket Agent Graph。

### 5.1 Dispatcher State

状态使用可 JSON 序列化的 `TypedDict`，主要字段如下：

```text
inbox_item
message
attachments
pending_action
candidate_ticket_ids
route_decision
target_ticket_id
ticket_thread_id
dispatch_outcome
error
```

数据库连接、Repository、模型客户端等运行时依赖通过构造函数或 LangGraph runtime context 注入，不写进 state 和 checkpoint。

### 5.2 Dispatcher Nodes

1. `message_entry`：把 Inbox row 转成 `NormalizedMessage`，设置处理状态并恢复附件元数据。
2. `archive_attachments`：在非 SHADOW 模式下调用现有附件归档和视觉解析能力；无附件时直接通过。
3. `load_dispatch_context`：加载待确认动作、引用、用户选单上下文和候选工单。
4. `classify_intent`：关键词快路径优先，未命中或信息不足时调用现有 `SemanticClassifier`。
5. `normalize_decision`：调用现有确定性后修正逻辑。
6. `resolve_pending_action`：优先处理已有确认、纠正、补充和选单回复。
7. `route_ticket`：复用现有编号、引用、用户上下文、语义评分和单候选规则。
8. `create_ticket`：新故障通过现有 validator 和 executor 建单，取得 `ticket_id`。
9. `clarify_ticket`：多候选或归属不明确时写入现有 PendingAction 并发送选单提示。
10. `invoke_ticket_agent`：使用 `ticket:{ticket_id}` 调用或恢复 Ticket Agent Graph。
11. `finalize_inbox`：把图结果转换为现有 Inbox 状态和 `processed_result`。
12. `handle_failure`：分类为可重试、校验拒绝或死信。

### 5.3 Dispatcher Edges

```text
START
  -> message_entry
  -> archive_attachments
  -> load_dispatch_context
  -> classify_intent
  -> normalize_decision
  -> resolve_pending_action

resolve_pending_action
  -> finalize_inbox                 已解决 Pending
  -> route_ticket                   没有 Pending 或回复不属于 Pending

route_ticket
  -> create_ticket                  新故障
  -> invoke_ticket_agent            已定位工单
  -> clarify_ticket                 多候选或歧义
  -> finalize_inbox                 忽略、查询等已在 Dispatcher 完成的消息

create_ticket
  -> invoke_ticket_agent            创建成功
  -> handle_failure                 创建失败

invoke_ticket_agent
  -> finalize_inbox
  -> handle_failure

handle_failure
  -> END

finalize_inbox
  -> END
```

显式错误编号仍然硬拒绝，不能因为 LangGraph 条件边而落入单候选或上下文工单。

## 6. 工单级 Ticket Agent Graph

Ticket Agent Graph 负责一张工单从自助排障到工程师跟进和店长确认的多轮过程。

### 6.1 Ticket Agent State

```text
ticket_id
ticket_no
group_id
agent_stage
incoming_event
semantic_decision
ticket_snapshot
conversation_summary
retrieval_query
retrieved_context
guidance
validated_command
command_result
pending_kind
run_outcome
error
```

所有字段必须可 JSON 序列化。`ticket_snapshot` 只保留当前决策需要的字段，不保存数据库 Row、连接对象或任意服务实例。

### 6.2 统一事件识别

`detect_ticket_event` 使用“关键词 + LLM”的混合方式：

1. 明确关键词和结构化命令走现有关键词快路径。
2. 未命中、口语化或字段不完整的消息交给现有 LLM classifier。
3. 两条路径都输出现有 `SemanticDecision` 结构。
4. 调用 `normalize_semantic_decision` 做确定性修正。
5. 调用协议 validator 验证意图、角色、工单状态和字段。

LLM 只能理解和抽取事件，不能直接修改工单或决定绕过协议。

### 6.3 自助排障循环

新建工单后进入 `SELF_SERVICE`：

```text
load_ticket_state
  -> detect_ticket_event
  -> select_stage_route
  -> build_retrieval_query
  -> retrieve_knowledge
  -> generate_guidance
  -> persist_and_notify_guidance
  -> wait_for_manager
```

`wait_for_manager` 使用 LangGraph interrupt 保存 checkpoint 并暂停。下一条被路由到该工单的店长消息使用相同 `thread_id` 恢复图。

恢复后的反馈分为：

- 未解决或出现新现象：更新摘要，回到 `build_retrieval_query`。
- 已解决：生成现有完成命令并调用 executor。
- 要求联系工程师：转入 `handoff_engineer`。
- 信息不足：生成澄清消息后再次等待。

当前 Inbox 消息在指导成功发出并保存 checkpoint 后视为处理完成。Agent 保持暂停，不占用常驻协程。

### 6.4 工程师跟进

`handoff_engineer` 调用现有通知与责任周期能力，并把 Agent 阶段切换为 `ENGINEER_TRACKING`。

后续工程师消息同样经过关键词和 LLM 识别，并映射到既有动作：

- 已接单、处理中：记录消息和责任方变化；
- 提交故障判断：调用现有 `ticket.diagnosis.submit`；
- 提交维修方式或订单号：调用现有 `ticket.repair_plan.submit`；
- 等待配件：继续使用订单监控和现有 SLA 规则；
- 报告完工：调用现有 `ticket.complete`，进入现有 `PENDING_CONFIRM`；
- 取消、停修、特殊情况、待商榷：继续走现有协议和 executor。

工程师完工后进入 `WAITING_CONFIRM`：

- 店长确认修好：调用现有 `ticket.confirm_complete`，Agent 进入 `CLOSED`。
- 店长反馈没修好：调用现有 `ticket.reject_complete`，Agent 返回 `ENGINEER_TRACKING`。

### 6.5 Ticket Agent Edges

```text
START
  -> load_ticket_state
  -> detect_ticket_event
  -> normalize_and_validate
  -> select_stage_route

select_stage_route
  -> build_retrieval_query           新建、未解决、新现象
  -> handoff_engineer                请求工程师
  -> execute_ticket_command          工程师更新、确认、取消等既有动作
  -> create_pending_action           需要确认或澄清
  -> finish_ticket_run               忽略或仅归档

build_retrieval_query
  -> retrieve_knowledge
  -> generate_guidance
  -> persist_and_notify_guidance
  -> wait_for_manager

wait_for_manager
  -> detect_ticket_event             收到恢复输入

handoff_engineer
  -> wait_for_ticket_event

wait_for_ticket_event
  -> detect_ticket_event             收到工程师或店长消息

execute_ticket_command
  -> apply_command_result

apply_command_result
  -> wait_for_ticket_event           工单仍在处理中
  -> wait_for_manager_confirmation   工程师已完工
  -> finish_ticket_run               查询或一次性动作完成
  -> close_ticket_agent              工单进入终态

wait_for_manager_confirmation
  -> detect_ticket_event

close_ticket_agent
  -> END
```

## 7. RAG 接口占位设计

本次只定义稳定端口，不实现真实 RAG。

```python
class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        *,
        query: str,
        ticket_context: dict[str, object],
        limit: int = 5,
    ) -> RetrievedContext: ...


class RetrievedContext(TypedDict):
    documents: list[dict[str, object]]
    query: str
    provider: str
    enabled: bool
```

首版注入 `NullKnowledgeRetriever`：

```text
documents = []
provider = "none"
enabled = false
```

`generate_guidance` 在 `enabled=false` 时仍可使用工单信息和当前消息调用现有 LLM，但不得声称引用了知识库内容。未来接入向量库时只替换 Retriever 实现，不修改 Graph 的 state、node 和 edge。

## 8. Checkpoint 与恢复

- Dispatcher Graph 不保存跨消息 checkpoint。
- Ticket Agent Graph 使用持久化异步 checkpointer。
- checkpoint 使用单独的 SQLite 文件，例如 `data/langgraph-checkpoints.sqlite`，不与 `tickets.db` 混用连接和事务。
- 每张工单固定使用 `ticket:{ticket_id}` 作为 `thread_id`。
- 节点启动时总是重新读取业务数据库，因此服务重启后可以校正 Agent 阶段。
- 终态工单保留 checkpoint 用于审计，但正常消息不再恢复其排障循环；重开动作通过现有 executor 后重新进入有效阶段。
- `interrupt` 节点前的副作用必须幂等；外部通知继续先写 Outbox，再发送。

## 9. SLA 与定时任务

LangGraph 节点不通过长时间 `sleep` 等待 SLA。

现有 `SchedulerWorker` 继续扫描业务数据库并直接生成去重通知：

- 响应 SLA；
- 时效 SLA；
- 店长确认超时；
- 订单到货及每日提醒；
- 待确认动作过期；
- AI 表格同步。

Scheduler 读取的是业务状态，因此不依赖某个 Ticket Agent 当前是否处于运行、暂停或服务重启状态。

## 10. 错误处理和幂等

- 模型临时失败继续映射为 `RETRY_PENDING`，由 Inbox Worker 作为唯一重试所有者。
- 达到最大尝试次数后继续进入 `DEAD_LETTER`。
- 协议和权限拒绝属于业务终态，不重试。
- LangGraph 节点返回结构化错误类型，不通过字符串判断 edge。
- 业务写入继续由 executor 和 repository 事务完成。
- 外部消息继续通过 Outbox `dedupe_key` 保证幂等。
- checkpoint 恢复可能重放节点，因此任何节点不得在 Outbox 之外直接执行不可去重的外部副作用。
- 图片分析的后台任务继续使用现有归档状态和 message ID 去重。

## 11. 代码组织

建议新增以下目录：

```text
graphs/
  __init__.py
  runtime.py                 # 构建和持有已编译 Graph
  state.py                   # DispatcherState、TicketAgentState
  outcomes.py                # edge 使用的枚举和结果类型
  dispatcher_graph.py        # 群级 Dispatcher Graph
  ticket_agent_graph.py      # 工单级 Graph
  nodes/
    dispatch.py
    semantic.py
    routing.py
    execution.py
    troubleshooting.py
    lifecycle.py
  retrieval/
    __init__.py
    base.py                  # KnowledgeRetriever、RetrievedContext
    null.py                  # NullKnowledgeRetriever
```

`pipeline.py` 保留为兼容外观，负责依赖注入并把 `process()` 转发给 Graph Runtime。迁移期间，原有私有辅助函数可以逐步移动到有明确输入输出的 node service；不在第一步删除仍被测试或脚本调用的兼容入口。

## 12. 迁移策略

采用逐段替换，避免一次性重写 1500 多行管道：

1. 引入 LangGraph 依赖、state 类型、结构化 outcome 和 Null Retriever。
2. 建立保持 `process()` 接口不变的 Graph Runtime。
3. 先迁移无副作用的语义识别、标准化和路由节点。
4. 再迁移 PendingAction、校验和 executor 节点。
5. 接入按工单 `thread_id` 隔离的 Ticket Agent Graph。
6. 接入自助排障循环和 interrupt；真实 RAG 仍关闭。
7. 接入工程师跟进、完工确认和现有状态动作。
8. 切换 `_build_pipeline()` 使用新 Runtime，同时保留受控的旧路径作为迁移期回退。
9. 全量行为测试通过后删除旧编排分支和失去引用的私有代码。

回退开关只用于迁移验证，不形成两套长期维护的业务实现。

## 13. 测试与验收

### 13.1 兼容性测试

现有测试应继续验证业务结果，重点覆盖：

- 建单、补充、诊断、维修方案、订单、完成、确认、驳回、取消、停修和重开；
- 显式错误编号硬拒绝；
- 多工单路由和选单上下文；
- PendingAction 确认、纠正和过期；
- SHADOW、ASSISTED、PRODUCTION 模式；
- 图片归档与视觉分析；
- 响应 SLA、时效 SLA 和订单提醒；
- 模型重试和死信。

### 13.2 新增 Graph 测试

- Dispatcher 各条件 edge 的路由结果；
- 两张工单使用不同 `thread_id`，checkpoint 不串线；
- 同一工单在店长多轮反馈中恢复相同 checkpoint；
- 自助排障未解决时循环，解决时关闭，请求工程师时切换阶段；
- 工程师更新、等待配件、完工和店长确认流程；
- `NullKnowledgeRetriever` 返回空上下文时仍能生成合规指导；
- 进程重启后从持久化 checkpoint 恢复；
- 节点重放不会重复建单、重复执行命令或重复发送通知；
- 不同群继续并行；同群识别并行，路由提交有序，不同工单并行、同一工单串行；
- Scheduler 不依赖 Graph 运行状态即可发送 SLA 通知。

### 13.3 验收标准

- `InboxWorker` 调用接口保持不变。
- 现有业务回归测试除已知脱敏样例问题外全部通过。
- 新增 Graph 流程测试全部通过。
- 同群两张活动工单能够维护独立的排障上下文。
- 服务重启后可以继续等待中的工单对话。
- RAG 未配置时不阻塞启动和消息处理。
- 管理后台、AI 表格同步和现有运维脚本不需要了解 LangGraph checkpoint。

## 14. 依赖和部署

实现阶段写入以下依赖范围：

```text
langgraph>=1.2.11,<2.0
langgraph-checkpoint-sqlite>=3.1.1,<4.0
```

SQLite checkpoint 包会安装其异步 SQLite 依赖。测试安装后锁定实际解析版本，并验证 Python 3.13/3.14 环境。

Docker 数据卷已经挂载 `data/`，因此单独的 checkpoint SQLite 文件可以随业务数据库一同持久化。部署时需要保证单实例继续持有业务 SQLite 写入权；本次不扩展为多进程共享写入架构。
