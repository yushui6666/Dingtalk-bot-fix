# LangGraph Runtime Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic message-processing control flow with explicit LangGraph state, nodes, and conditional edges while preserving the existing asyncio runtime, database model, business rules, and public pipeline API.

**Architecture:** `MessageProcessingPipeline` remains the compatibility boundary used by `InboxWorker`, but delegates orchestration to a compiled Dispatcher Graph. Each ticket uses the same compiled Ticket Agent Graph with `thread_id=ticket:{ticket_id}` and an async SQLite checkpointer; existing classifier, router, validator, executor, repository, pending-action service, notifier, and scheduler remain authoritative.

**Tech Stack:** Python 3.13+, asyncio, LangGraph 1.2, langgraph-checkpoint-sqlite 3.1, SQLite/WAL, pytest, pytest-asyncio.

---

## File map

**Create**

- `graphs/__init__.py`: graph package exports.
- `graphs/state.py`: JSON-safe dispatcher and ticket state contracts plus message conversion.
- `graphs/outcomes.py`: graph stage and edge outcome enums.
- `graphs/retrieval/base.py`: RAG port and retrieved-context contract.
- `graphs/retrieval/null.py`: disabled RAG implementation.
- `graphs/runtime.py`: compiled graph ownership, checkpoint lifecycle, and `process()` entry point.
- `graphs/dispatcher_graph.py`: per-message graph nodes and conditional edges.
- `graphs/ticket_agent_graph.py`: per-ticket checkpointed workflow and troubleshooting loop.
- `graphs/troubleshooting.py`: graph-only feedback classification and guidance generation.
- `tests/test_graph_state.py`: serialization and stage derivation tests.
- `tests/test_null_retriever.py`: disabled RAG contract tests.
- `tests/test_graph_runtime.py`: dispatcher compatibility, edge, retry, and lifecycle tests.
- `tests/test_ticket_agent_graph.py`: thread isolation, checkpoint resume, and troubleshooting-loop tests.

**Modify**

- `requirements.txt`: LangGraph dependencies.
- `config.py`: checkpoint path and feature switch.
- `pipeline.py`: retain public API, expose existing processing steps to graph nodes, and delegate `process()` to runtime.
- `main.py`: create and close graph runtime/checkpointer with application lifecycle.
- `semantics/classifier.py`: expose the existing model client through a focused JSON-classification helper used by troubleshooting.
- `.env.example`: document graph and empty-RAG switches.
- `README.md`: document the new orchestration boundary and RAG-disabled behavior.

## Task 1: Add dependencies and graph contracts

**Files:**

- Modify: `requirements.txt`
- Create: `graphs/__init__.py`
- Create: `graphs/outcomes.py`
- Create: `graphs/state.py`
- Create: `tests/test_graph_state.py`

- [ ] **Step 1: Write state-contract tests**

```python
# tests/test_graph_state.py
from datetime import datetime

from graphs.outcomes import AgentStage, DispatchOutcome
from graphs.state import message_from_state, message_to_state, stage_from_ticket
from models import NormalizedMessage


def test_normalized_message_round_trip_is_json_safe():
    original = NormalizedMessage(
        message_id="m1", group_id="g1", sender_id="u1", sender_name="店长",
        content="空调不制冷", sender_role="MANAGER",
        sent_at=datetime(2026, 9, 15, 10, 0, 0),
    )
    payload = message_to_state(original)
    assert payload["sent_at"] == "2026-09-15T10:00:00"
    assert message_from_state(payload).message_id == "m1"


def test_stage_is_derived_from_business_ticket_status():
    assert stage_from_ticket({"status": "PENDING_CONFIRM"}) == AgentStage.WAITING_CONFIRM
    assert stage_from_ticket({"status": "COMPLETED"}) == AgentStage.CLOSED
    assert stage_from_ticket({"status": "ACTIVE"}) == AgentStage.SELF_SERVICE


def test_graph_outcomes_are_stable_strings():
    assert DispatchOutcome.INVOKE_TICKET.value == "INVOKE_TICKET"
```

- [ ] **Step 2: Run the contract tests and verify failure**

Run: `python -m pytest tests/test_graph_state.py -q`

Expected: collection fails because the `graphs` package does not exist.

- [ ] **Step 3: Add dependency ranges**

Append to `requirements.txt`:

```text

# ─── LangGraph 运行编排与 SQLite checkpoint ───
langgraph>=1.2.11,<2.0
langgraph-checkpoint-sqlite>=3.1.1,<4.0
```

- [ ] **Step 4: Implement enums and JSON-safe state conversion**

```python
# graphs/outcomes.py
from enum import StrEnum


class AgentStage(StrEnum):
    TRIAGE = "TRIAGE"
    SELF_SERVICE = "SELF_SERVICE"
    WAITING_MANAGER = "WAITING_MANAGER"
    ENGINEER_TRACKING = "ENGINEER_TRACKING"
    WAITING_PARTS = "WAITING_PARTS"
    WAITING_CONFIRM = "WAITING_CONFIRM"
    CLOSED = "CLOSED"


class DispatchOutcome(StrEnum):
    CONTINUE = "CONTINUE"
    INVOKE_TICKET = "INVOKE_TICKET"
    COMPLETE = "COMPLETE"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


class TicketRunOutcome(StrEnum):
    WAITING = "WAITING"
    EXECUTED = "EXECUTED"
    IGNORED = "IGNORED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
```

```python
# graphs/state.py
from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from graphs.outcomes import AgentStage
from models import NormalizedMessage, TICKET_CANCELLED, TICKET_COMPLETED, TICKET_PENDING_CONFIRM, TICKET_STOPPED


class DispatcherState(TypedDict, total=False):
    inbox_item: dict[str, Any]
    message: dict[str, Any]
    target_ticket_id: int
    ticket_thread_id: str
    dispatch_outcome: str
    processed_result: str
    error: str


class TicketAgentState(TypedDict, total=False):
    ticket_id: int
    ticket_no: str
    group_id: str
    agent_stage: str
    incoming_event: dict[str, Any]
    conversation_summary: str
    retrieval_query: str
    retrieved_context: dict[str, Any]
    guidance: str
    run_outcome: str
    error: str


def message_to_state(message: NormalizedMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "group_id": message.group_id,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "content": message.content,
        "message_type": message.message_type,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "sender_role": message.sender_role,
        "is_self": message.is_self,
        "reply_to_message_id": message.reply_to_message_id,
        "attachments": [a.__dict__ for a in message.attachments],
    }


def message_from_state(payload: dict[str, Any]) -> NormalizedMessage:
    from models import ImageAttachment
    return NormalizedMessage(
        message_id=payload["message_id"], group_id=payload["group_id"],
        sender_id=payload["sender_id"], sender_name=payload["sender_name"],
        content=payload.get("content", ""), message_type=payload.get("message_type", "text"),
        sent_at=datetime.fromisoformat(payload["sent_at"]) if payload.get("sent_at") else None,
        sender_role=payload.get("sender_role", "UNKNOWN"), is_self=payload.get("is_self", False),
        reply_to_message_id=payload.get("reply_to_message_id"),
        attachments=[ImageAttachment(**a) for a in payload.get("attachments", [])],
    )


def stage_from_ticket(ticket: dict[str, Any]) -> AgentStage:
    status = ticket.get("status")
    if status == TICKET_PENDING_CONFIRM:
        return AgentStage.WAITING_CONFIRM
    if status in {TICKET_COMPLETED, TICKET_CANCELLED, TICKET_STOPPED}:
        return AgentStage.CLOSED
    return AgentStage.SELF_SERVICE
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_graph_state.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt graphs/__init__.py graphs/outcomes.py graphs/state.py tests/test_graph_state.py
git commit -m "feat: add LangGraph state contracts"
```

## Task 2: Add the disabled RAG port

**Files:**

- Create: `graphs/retrieval/__init__.py`
- Create: `graphs/retrieval/base.py`
- Create: `graphs/retrieval/null.py`
- Create: `tests/test_null_retriever.py`

- [ ] **Step 1: Write the async contract test**

```python
# tests/test_null_retriever.py
import pytest

from graphs.retrieval.null import NullKnowledgeRetriever


@pytest.mark.asyncio
async def test_null_retriever_returns_explicit_disabled_context():
    result = await NullKnowledgeRetriever().retrieve(
        query="空调不制冷", ticket_context={"ticket_id": 7}, limit=5,
    )
    assert result == {
        "documents": [], "query": "空调不制冷", "provider": "none", "enabled": False,
    }
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m pytest tests/test_null_retriever.py -q`

Expected: import fails because `graphs.retrieval` does not exist.

- [ ] **Step 3: Implement the port and null adapter**

```python
# graphs/retrieval/base.py
from typing import Any, Protocol, TypedDict


class RetrievedContext(TypedDict):
    documents: list[dict[str, Any]]
    query: str
    provider: str
    enabled: bool


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self, *, query: str, ticket_context: dict[str, Any], limit: int = 5,
    ) -> RetrievedContext:
        raise NotImplementedError
```

```python
# graphs/retrieval/null.py
from typing import Any

from graphs.retrieval.base import RetrievedContext


class NullKnowledgeRetriever:
    async def retrieve(
        self, *, query: str, ticket_context: dict[str, Any], limit: int = 5,
    ) -> RetrievedContext:
        return {"documents": [], "query": query, "provider": "none", "enabled": False}
```

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_null_retriever.py -q`

Expected: `1 passed`.

```bash
git add graphs/retrieval tests/test_null_retriever.py
git commit -m "feat: add disabled RAG adapter"
```

## Task 3: Introduce a LangGraph dispatcher without changing behavior

**Files:**

- Create: `graphs/dispatcher_graph.py`
- Create: `graphs/runtime.py`
- Create: `tests/test_graph_runtime.py`
- Modify: `pipeline.py`

- [ ] **Step 1: Write a compatibility test**

```python
# tests/test_graph_runtime.py
import pytest

from graphs.runtime import GraphRuntime


class StubPipeline:
    def __init__(self):
        self.items = []

    async def _process_without_graph(self, item):
        self.items.append(item)
        return "COMPLETED"


@pytest.mark.asyncio
async def test_dispatcher_graph_preserves_pipeline_result():
    pipeline = StubPipeline()
    runtime = GraphRuntime(pipeline)
    item = {"message_id": "m1"}
    assert await runtime.process(item) == "COMPLETED"
    assert pipeline.items == [item]
```

- [ ] **Step 2: Run the test and verify failure**

Run: `python -m pytest tests/test_graph_runtime.py::test_dispatcher_graph_preserves_pipeline_result -q`

Expected: fails because `GraphRuntime` is not defined.

- [ ] **Step 3: Move the current public process body behind a compatibility method**

In `pipeline.py`, rename the existing `process` method at line 169 to `_process_without_graph` without changing its body. Add this new public method immediately before it:

```python
async def process(self, item: dict[str, Any]) -> str:
    if self._graph_runtime is None:
        from graphs.runtime import GraphRuntime
        self._graph_runtime = GraphRuntime(self)
    return await self._graph_runtime.process(item)
```

Initialize `self._graph_runtime = None` in `__init__`.

- [ ] **Step 4: Implement the first dispatcher graph**

```python
# graphs/dispatcher_graph.py
from langgraph.graph import END, START, StateGraph

from graphs.outcomes import DispatchOutcome
from graphs.state import DispatcherState


def build_dispatcher_graph(pipeline):
    async def run_existing_flow(state: DispatcherState):
        result = await pipeline._process_without_graph(state["inbox_item"])
        return {"processed_result": result, "dispatch_outcome": DispatchOutcome.COMPLETE.value}

    builder = StateGraph(DispatcherState)
    builder.add_node("run_existing_flow", run_existing_flow)
    builder.add_edge(START, "run_existing_flow")
    builder.add_edge("run_existing_flow", END)
    return builder.compile()
```

```python
# graphs/runtime.py
from graphs.dispatcher_graph import build_dispatcher_graph


class GraphRuntime:
    def __init__(self, pipeline):
        self._dispatcher = build_dispatcher_graph(pipeline)

    async def process(self, item):
        result = await self._dispatcher.ainvoke({"inbox_item": dict(item)})
        return result["processed_result"]

    async def aclose(self):
        return None
```

- [ ] **Step 5: Run focused and existing integration tests**

Run: `python -m pytest tests/test_graph_runtime.py tests/test_pipeline_integration.py -q`

Expected: all selected tests pass with unchanged business results.

- [ ] **Step 6: Commit**

```bash
git add graphs/dispatcher_graph.py graphs/runtime.py pipeline.py tests/test_graph_runtime.py
git commit -m "refactor: route pipeline through LangGraph"
```

## Task 4: Extract message preparation and failure edges

**Files:**

- Modify: `graphs/dispatcher_graph.py`
- Modify: `graphs/state.py`
- Modify: `pipeline.py`
- Modify: `tests/test_graph_runtime.py`
- Modify: `tests/test_pipeline_integration.py`

- [ ] **Step 1: Add edge tests**

Extend the existing failure tests in `tests/test_pipeline_integration.py` with graph-path assertions:

```python
# At the end of test_shadow_audit_failure_is_retried
assert "handle_failure" in pipeline.last_graph_path

# At the end of test_model_failure_retries_then_dead_letter
assert "handle_failure" in pl.last_graph_path
```

- [ ] **Step 2: Run tests and verify the expected edge assertions fail**

Run: `python -m pytest tests/test_graph_runtime.py tests/test_pipeline_integration.py::test_shadow_audit_failure_is_retried tests/test_pipeline_integration.py::test_model_failure_retries_then_dead_letter -q`

Expected: new edge metadata assertions fail while legacy results remain correct.

- [ ] **Step 3: Extract preparation methods from `pipeline.py`**

Implement focused compatibility methods using the existing code:

```python
def graph_prepare_message(self, item):
    msg = _row_to_message(item)
    self._restore_attachments(msg)
    self._db.inbox_set_status(msg.message_id, "PROCESSING")
    return msg


async def graph_archive_attachments(self, msg):
    if self._mode != RuntimeMode.SHADOW:
        await self._archive_attachments(msg)


async def graph_handle_message(self, item, msg):
    return await self._handle(msg, item)


def graph_handle_exception(self, item, msg, exc):
    return self._retry_or_dead(item, msg, str(exc))
```

- [ ] **Step 4: Replace the single graph node with explicit nodes and conditional edges**

```text
START -> prepare_message -> archive_attachments -> process_business
process_business -> finalize
archive_attachments/process_business exception -> handle_failure -> finalize
finalize -> END
```

Each node returns only state updates. `handle_failure` writes the existing retry/dead-letter state through `graph_handle_exception`.

- [ ] **Step 5: Run regression tests and commit**

Run: `python -m pytest tests/test_graph_runtime.py tests/test_pipeline_integration.py tests/test_inbox_worker.py -q`

Expected: all selected tests pass.

```bash
git add graphs/dispatcher_graph.py graphs/state.py pipeline.py tests/test_graph_runtime.py
git commit -m "refactor: graph message preparation and failures"
```

## Task 5: Extract semantic, pending, routing, validation, and execution nodes

**Files:**

- Create: `graphs/nodes/__init__.py`
- Create: `graphs/nodes/semantic.py`
- Create: `graphs/nodes/routing.py`
- Create: `graphs/nodes/execution.py`
- Modify: `graphs/dispatcher_graph.py`
- Modify: `pipeline.py`
- Modify: `tests/test_graph_runtime.py`
- Modify: `tests/test_pipeline_integration.py`

- [ ] **Step 1: Add one test for every conditional route**

Add parameterized cases covering these outcomes:

```python
@pytest.mark.parametrize(
    ("text", "expected_node"),
    [
        ("普通闲聊", "finish_ignored"),
        ("#查询工单", "handle_query"),
        ("#选择工单 001", "handle_select"),
        ("#报修\n主题：门锁\n位置：二楼\n问题描述：打不开\n时效：3天", "execute_command"),
        ("确认", "resolve_pending"),
    ],
)
async def test_dispatcher_records_expected_route(env, text, expected_node):
    pipeline = env.make_pipeline()
    await env.process(text, f"route-{expected_node}", pipeline=pipeline)
    assert expected_node in pipeline.last_graph_path
```

Also retain the existing wrong-number, multi-candidate, confirmation, assisted-mode and shadow-mode tests as black-box assertions.

- [ ] **Step 2: Run tests and verify path assertions fail**

Run: `python -m pytest tests/test_graph_runtime.py tests/test_select_number_guard.py -q`

Expected: path assertions fail because `_handle` is still one node.

- [ ] **Step 3: Extract node-sized methods without changing their internals**

Split `_handle` at its existing section boundaries into methods returning typed step results. Use these existing blocks as the cut boundaries:

| Existing `_handle` block | New method | Returned outcome |
|---|---|---|
| Start through `normalize_semantic_decision` and SHADOW handling | `graph_decide` | `SHADOW`, `RETRY`, `CONTINUE` |
| Pending lookup through model fallback | `graph_resolve_pending` | terminal processed result or `CONTINUE` |
| Semantic audit, confirm-window archive, ignore and `system.clarify` | `graph_apply_pre_route_rules` | `IGNORE`, `CLARIFY`, `CONTINUE` |
| Candidate snapshots through explicit-number guard | `graph_prepare_candidates` | `REJECT`, `CONTINUE` |
| Select/query special cases through the `self._router.route` call | `graph_route_ticket` | `SELECT`, `QUERY`, `CLARIFY`, `ROUTED` |
| Manager-complete fallback and special-case guards | `graph_apply_post_route_rules` | `REJECT`, `CLARIFY`, `ROUTED` |
| The `validate_decision` call through assisted/confirmation policy | `graph_validate` | `IGNORE`, `REJECT`, `WAIT_CONFIRM`, `EXECUTE` |
| Executor call through notifier flush | `graph_execute` | `EXECUTED`, executor rejection, or `FAILURE` |

Each method returns this concrete type:

```python
@dataclass(frozen=True)
class PipelineStepResult:
    outcome: str
    value: Any = None
    processed_result: str | None = None
```

`value` carries the existing immutable `SemanticDecision`, candidate tuple, `RouteResult`, or `ValidatedCommand` needed by the next node. `processed_result` is set only when that step has already completed the Inbox message. The bodies are moved from `_handle`; business conditions and notification text are not rewritten.

- [ ] **Step 4: Wire conditional edges**

```text
decide -> shadow | resolve_pending | retry | audit_decision
audit_decision -> ignore | clarify | prepare_candidates
prepare_candidates -> number_guard
number_guard -> reject | select | query | route_ticket
route_ticket -> clarify | validate
validate -> reject | create_pending | execute_command
execute_command -> finalize
```

Append each node name to a test-only `graph_path` state field; expose the last completed path from runtime for assertions without affecting business behavior.

- [ ] **Step 5: Run broad pipeline regression tests**

Run: `python -m pytest tests/test_pipeline_integration.py tests/test_select_number_guard.py tests/test_four_improvements.py tests/test_negotiating_routing.py tests/test_special_case.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add graphs/nodes graphs/dispatcher_graph.py pipeline.py tests/test_graph_runtime.py
git commit -m "refactor: model ticket processing as graph nodes"
```

## Task 6: Add persistent per-ticket agents

**Files:**

- Modify: `config.py`
- Create: `graphs/ticket_agent_graph.py`
- Modify: `graphs/runtime.py`
- Create: `tests/test_ticket_agent_graph.py`

- [ ] **Step 1: Write thread-isolation and restart tests**

```python
@pytest.mark.asyncio
async def test_two_tickets_keep_separate_agent_state(ticket_graph_env):
    await ticket_graph_env.invoke(1, {"content": "空调仍然不制冷"})
    await ticket_graph_env.invoke(2, {"content": "门锁仍然打不开"})
    first = await ticket_graph_env.state(1)
    second = await ticket_graph_env.state(2)
    assert first.values["ticket_id"] == 1
    assert second.values["ticket_id"] == 2
    assert "空调" in first.values["conversation_summary"]
    assert "门锁" in second.values["conversation_summary"]


@pytest.mark.asyncio
async def test_ticket_agent_state_survives_runtime_restart(ticket_graph_env):
    await ticket_graph_env.invoke(1, {"content": "压缩机有异响"})
    await ticket_graph_env.restart()
    restored = await ticket_graph_env.state(1)
    assert "压缩机" in restored.values["conversation_summary"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_ticket_agent_graph.py -q`

Expected: import or state lookup fails because the ticket graph and checkpointer do not exist.

- [ ] **Step 3: Add checkpoint configuration**

```python
# config.py
LANGGRAPH_ENABLED = _os.environ.get("LANGGRAPH_ENABLED", "true").lower() not in {"0", "false", "no"}
LANGGRAPH_CHECKPOINT_PATH = Path(
    _os.environ.get("LANGGRAPH_CHECKPOINT_PATH", str(BASE_DIR / "data" / "langgraph-checkpoints.sqlite"))
)
```

- [ ] **Step 4: Build the per-ticket graph**

```python
# graphs/ticket_agent_graph.py
from langgraph.graph import END, START, StateGraph

from graphs.state import TicketAgentState


def build_ticket_agent_graph(*, load_ticket):
    async def load_ticket_state(state):
        ticket = load_ticket(state["ticket_id"])
        return {
            "ticket_no": ticket["ticket_no"],
            "group_id": ticket["group_id"],
            "agent_stage": stage_from_ticket(ticket).value,
        }

    def record_event(state):
        content = str(state.get("incoming_event", {}).get("content", "")).strip()
        prior = state.get("conversation_summary", "")
        summary = "\n".join(part for part in (prior, content) if part)[-2000:]
        return {"conversation_summary": summary, "run_outcome": "EXECUTED"}

    builder = StateGraph(TicketAgentState)
    builder.add_node("load_ticket_state", load_ticket_state)
    builder.add_node("record_event", record_event)
    builder.add_edge(START, "load_ticket_state")
    builder.add_edge("load_ticket_state", "record_event")
    builder.add_edge("record_event", END)
    return builder
```

Compile it with `AsyncSqliteSaver` and invoke it with:

```python
config = {"configurable": {"thread_id": f"ticket:{ticket_id}"}}
await ticket_graph.ainvoke(
    {"ticket_id": ticket_id, "incoming_event": incoming_event}, config=config,
)
```

Set strict msgpack deserialization through the saver configuration or `LANGGRAPH_STRICT_MSGPACK=true`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_ticket_agent_graph.py tests/test_graph_runtime.py -q`

Expected: all selected tests pass.

```bash
git add config.py graphs/ticket_agent_graph.py graphs/runtime.py tests/test_ticket_agent_graph.py
git commit -m "feat: persist one graph thread per ticket"
```

## Task 7: Add the manager troubleshooting loop with empty RAG

**Files:**

- Create: `graphs/troubleshooting.py`
- Modify: `graphs/ticket_agent_graph.py`
- Modify: `graphs/runtime.py`
- Modify: `semantics/classifier.py`
- Modify: `tests/test_ticket_agent_graph.py`

- [ ] **Step 1: Write loop tests using fake feedback and guidance services**

```python
@pytest.mark.asyncio
async def test_unsolved_feedback_repeats_guidance(ticket_graph_env):
    first = await ticket_graph_env.start_ticket(1, "空调不制冷")
    assert first.values["agent_stage"] == "WAITING_MANAGER"
    assert first.values["guidance"] == "检查空调滤网"

    second = await ticket_graph_env.resume_ticket(1, "清理了还是不制冷")
    assert second.values["agent_stage"] == "WAITING_MANAGER"
    assert ticket_graph_env.guidance.calls == 2


@pytest.mark.asyncio
async def test_manager_can_finish_or_handoff(ticket_graph_env):
    await ticket_graph_env.start_ticket(1, "空调不制冷")
    finished = await ticket_graph_env.resume_ticket(1, "已经修好了")
    assert finished.values["agent_stage"] == "CLOSED"

    await ticket_graph_env.start_ticket(2, "门锁打不开")
    handed_off = await ticket_graph_env.resume_ticket(2, "联系工程师吧")
    assert handed_off.values["agent_stage"] == "ENGINEER_TRACKING"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_ticket_agent_graph.py -q`

Expected: tests fail because feedback classification, retrieval, guidance, and waiting edges are absent.

- [ ] **Step 3: Implement graph-only feedback and guidance contracts**

```python
# graphs/troubleshooting.py
from enum import StrEnum
from typing import Any, Protocol


class ManagerFeedback(StrEnum):
    UNSOLVED = "UNSOLVED"
    RESOLVED = "RESOLVED"
    HANDOFF = "HANDOFF"
    UNCLEAR = "UNCLEAR"


class FeedbackClassifier(Protocol):
    async def classify_feedback(self, *, message: dict[str, Any], ticket: dict[str, Any]) -> ManagerFeedback:
        raise NotImplementedError


class GuidanceGenerator(Protocol):
    async def generate(
        self, *, ticket: dict[str, Any], message: dict[str, Any], retrieved_context: dict[str, Any],
    ) -> str:
        raise NotImplementedError
```

Production feedback classification first checks explicit phrases such as `修好了`、`还是不行`、`联系工程师`; otherwise it calls the existing model client with a four-value JSON schema. Guidance generation uses a JSON schema with one required string field named `guidance`.

- [ ] **Step 4: Add ticket graph nodes and edges**

Implement:

```text
classify_feedback
build_retrieval_query
retrieve_knowledge
generate_guidance
persist_and_notify_guidance
wait_for_manager
handoff_engineer
complete_from_manager
```

`retrieve_knowledge` calls `NullKnowledgeRetriever` by default. `wait_for_manager` calls `interrupt()` with a JSON-safe payload containing `ticket_id`, `ticket_no`, `stage`, and `expected_sender_role`.

Conditional feedback edges are:

```text
UNSOLVED -> build_retrieval_query
RESOLVED -> complete_from_manager
HANDOFF -> handoff_engineer
UNCLEAR -> persist_and_notify_guidance
```

- [ ] **Step 5: Run loop and pipeline tests**

Run: `python -m pytest tests/test_ticket_agent_graph.py tests/test_pipeline_integration.py -q`

Expected: all selected tests pass; fake classifiers without troubleshooting capabilities continue using the compatibility path.

- [ ] **Step 6: Commit**

```bash
git add graphs/troubleshooting.py graphs/ticket_agent_graph.py graphs/runtime.py semantics/classifier.py tests/test_ticket_agent_graph.py
git commit -m "feat: add checkpointed manager troubleshooting loop"
```

## Task 8: Route engineer tracking through the ticket agent

**Files:**

- Modify: `graphs/ticket_agent_graph.py`
- Modify: `graphs/dispatcher_graph.py`
- Modify: `pipeline.py`
- Modify: `tests/test_ticket_agent_graph.py`

- [ ] **Step 1: Write lifecycle tests**

```python
@pytest.mark.asyncio
async def test_engineer_completion_waits_for_manager(ticket_graph_env):
    await ticket_graph_env.handoff(1)
    result = await ticket_graph_env.engineer_message(1, "维修完成", intent="ticket.complete")
    assert result.values["agent_stage"] == "WAITING_CONFIRM"

    rejected = await ticket_graph_env.manager_message(1, "没修好", intent="ticket.reject_complete")
    assert rejected.values["agent_stage"] == "ENGINEER_TRACKING"

    await ticket_graph_env.engineer_message(1, "已经重新修好", intent="ticket.complete")
    confirmed = await ticket_graph_env.manager_message(1, "确认修好", intent="ticket.confirm_complete")
    assert confirmed.values["agent_stage"] == "CLOSED"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_ticket_agent_graph.py::test_engineer_completion_waits_for_manager -q`

Expected: edge or stage assertions fail.

- [ ] **Step 3: Add engineer lifecycle routing**

The ticket graph receives the already normalized and validated existing intent from Dispatcher. Implement conditional edges:

```text
ticket.diagnosis.submit -> execute_existing_command -> ENGINEER_TRACKING
ticket.repair_plan.submit -> execute_existing_command -> ENGINEER_TRACKING or WAITING_PARTS
ticket.complete -> execute_existing_command -> WAITING_CONFIRM
ticket.reject_complete -> execute_existing_command -> ENGINEER_TRACKING
ticket.confirm_complete -> execute_existing_command -> CLOSED
ticket.cancel/ticket.stop -> execute_existing_command -> CLOSED
```

After every executor call, reload the ticket from SQLite and derive Agent stage from the resulting business status rather than trusting the model output.

- [ ] **Step 4: Preserve pending-confirm archive behavior**

Move the existing `CONFIRM_WINDOW` message-linking block into a graph node that runs before intent routing whenever the database reports a pending-confirm ticket in the group. Keep the existing rule that `chat.ignore` messages in this window are archived.

- [ ] **Step 5: Run lifecycle regressions and commit**

Run: `python -m pytest tests/test_four_improvements.py tests/test_negotiating_status.py tests/test_negotiating_routing.py tests/test_pipeline_integration.py -q`

Expected: all selected tests pass.

```bash
git add graphs/ticket_agent_graph.py graphs/dispatcher_graph.py pipeline.py tests/test_ticket_agent_graph.py
git commit -m "refactor: graph engineer and completion lifecycle"
```

## Task 9: Integrate runtime lifecycle and configuration

**Files:**

- Modify: `main.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_graph_runtime.py`

- [ ] **Step 1: Write lifecycle test**

```python
@pytest.mark.asyncio
async def test_runtime_close_closes_checkpoint_connection(tmp_path):
    runtime = GraphRuntime(StubPipeline(), checkpoint_path=tmp_path / "checkpoints.sqlite")
    await runtime.process({"message_id": "close-1"})
    await runtime.aclose()
    assert runtime.closed is True
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_graph_runtime.py::test_runtime_close_closes_checkpoint_connection -q`

Expected: fails because runtime close state is not exposed or the saver remains open.

- [ ] **Step 3: Connect runtime startup and shutdown**

Expose `pipeline.graph_runtime` and implement idempotent `aclose()`. In `main.py` close it before closing the business database:

```python
if pipeline.graph_runtime is not None:
    await pipeline.graph_runtime.aclose()
db.close()
```

- [ ] **Step 4: Document environment controls**

Add to `.env.example`:

```text
LANGGRAPH_ENABLED=true
LANGGRAPH_CHECKPOINT_PATH=data/langgraph-checkpoints.sqlite
RAG_ENABLED=false
```

Update `README.md` architecture and startup sections to state that RAG currently uses `NullKnowledgeRetriever` and needs no external service.

- [ ] **Step 5: Run startup-focused tests and commit**

Run: `python -m pytest tests/test_config_dotenv.py tests/test_graph_runtime.py tests/test_subprocess_cleanup.py -q`

Expected: all selected tests pass.

```bash
git add main.py config.py .env.example README.md tests/test_graph_runtime.py
git commit -m "feat: integrate LangGraph runtime lifecycle"
```

## Task 10: Full regression, simulation, and legacy cleanup

**Files:**

- Modify: `pipeline.py`
- Modify: `graphs/dispatcher_graph.py`
- Modify: `graphs/ticket_agent_graph.py`
- Modify: `scripts/simulate_flow.py`
- Modify: `README.md`

- [ ] **Step 1: Run syntax and protocol checks**

Run:

```bash
python -m compileall -q .
python -c "import json, pathlib; [json.loads(p.read_text()) for p in pathlib.Path('protocols').glob('*.json')]"
```

Expected: both commands exit `0`.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest tests -q`

Expected: all tests pass except the documented pre-existing desensitized fixture case if its punctuation remains unchanged.

- [ ] **Step 3: Run the offline flow simulation**

Run: `python scripts/simulate_flow.py`

Expected: the script completes, prints the existing ticket lifecycle, and never calls an external RAG provider.

- [ ] **Step 4: Remove the migration fallback**

After all graph paths cover the old orchestration, remove the feature-flag branch that calls `_process_without_graph` directly. Keep `MessageProcessingPipeline.process()` as the stable public adapter and retain private helpers still used by image-task cleanup tests.

- [ ] **Step 5: Repeat focused and full validation**

Run:

```bash
python -m pytest tests/test_graph_runtime.py tests/test_ticket_agent_graph.py tests/test_pipeline_integration.py -q
python -m pytest tests -q
```

Expected: both commands complete with the same accepted result as Step 2.

- [ ] **Step 6: Commit**

```bash
git add pipeline.py graphs scripts/simulate_flow.py README.md tests
git commit -m "refactor: complete LangGraph runtime migration"
```

## Final verification checklist

- [ ] `MessageProcessingPipeline.process()` remains the Inbox Worker entry point.
- [ ] Existing business tables and ticket status strings remain unchanged.
- [ ] Dispatcher execution is visible as named LangGraph nodes and conditional edges.
- [ ] Every active ticket uses `thread_id=ticket:{ticket_id}`.
- [ ] Two tickets in one group retain separate checkpoint state.
- [ ] Manager feedback resumes the correct troubleshooting loop.
- [ ] Engineering updates and manager confirmation use existing commands and executors.
- [ ] Scheduler remains outside LangGraph and continues using business SQLite.
- [ ] RAG is represented by `NullKnowledgeRetriever` and requires no external configuration.
- [ ] Model failures, retries, dead letters, Outbox dedupe, and image task cleanup retain existing behavior.
