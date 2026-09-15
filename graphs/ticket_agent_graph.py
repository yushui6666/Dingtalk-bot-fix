"""Checkpointed logical agent used independently by every ticket."""

from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from graphs.outcomes import AgentStage, TicketEvent
from graphs.state import TicketAgentState
from graphs.troubleshooting import HybridTroubleshootingService, ManagerFeedback
from models import (
    ROLE_ENGINEER,
    ROLE_MANAGER,
    TICKET_CANCELLED,
    TICKET_COMPLETED,
    TICKET_NEGOTIATING,
    TICKET_PENDING_CONFIRM,
    TICKET_STOPPED,
)


_TERMINAL_STATUSES = {TICKET_COMPLETED, TICKET_CANCELLED, TICKET_STOPPED}


def _ticket_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "ticket_no", "group_id", "store_name", "subject", "location",
        "problem_description", "status", "waiting_side", "sla_days",
        "current_deadline_at", "version",
    )
    return {key: ticket.get(key) for key in keys}


def build_ticket_agent_graph(
    *,
    load_ticket: Callable[[int], dict[str, Any] | None],
    retriever: Any,
    troubleshooting: HybridTroubleshootingService,
    notify: Callable[[str, str, str], None],
    persist_event: Callable[[int, dict[str, Any]], None],
) -> StateGraph:
    async def load_ticket_state(state: TicketAgentState) -> dict[str, Any]:
        ticket = load_ticket(state["ticket_id"])
        if ticket is None:
            return {"error": f"ticket {state['ticket_id']} not found", "run_outcome": "NOT_FOUND"}
        snapshot = _ticket_snapshot(ticket)
        status = str(ticket.get("status") or "")
        prior = str(state.get("agent_stage") or AgentStage.TRIAGE)
        if status in _TERMINAL_STATUSES:
            stage = AgentStage.CLOSED
        elif status == TICKET_PENDING_CONFIRM:
            stage = AgentStage.WAITING_CONFIRM
        elif status == TICKET_NEGOTIATING:
            stage = AgentStage.WAITING_PARTS
        elif prior in {
            AgentStage.SELF_SERVICE,
            AgentStage.WAITING_MANAGER,
            AgentStage.ENGINEER_TRACKING,
            AgentStage.WAITING_PARTS,
        }:
            stage = AgentStage(prior)
        else:
            stage = AgentStage.TRIAGE
        return {
            "ticket_no": str(ticket.get("ticket_no") or ""),
            "group_id": str(ticket.get("group_id") or ""),
            "ticket_snapshot": snapshot,
            "agent_stage": stage.value,
            "event_kind": "",
            "feedback": "",
            "guidance": "",
            "retrieved_context": {},
            "retrieval_query": "",
            "run_outcome": "RUNNING",
            "error": "",
        }

    async def detect_ticket_event(state: TicketAgentState) -> dict[str, Any]:
        ticket = state["ticket_snapshot"]
        message = state.get("incoming_event", {})
        role = str(message.get("sender_role") or "")
        intent = str(message.get("semantic_intent") or "")
        status = str(ticket.get("status") or "")

        if status in _TERMINAL_STATUSES:
            return {"event_kind": TicketEvent.CLOSED.value}
        if intent == "ticket.create" or message.get("link_type") == "CREATE":
            return {"event_kind": TicketEvent.NEW_ISSUE.value}
        if status == TICKET_PENDING_CONFIRM or intent == "ticket.complete":
            return {"event_kind": TicketEvent.ENGINEER_COMPLETED.value}
        if intent == "ticket.reject_complete":
            return {"event_kind": TicketEvent.MANAGER_REJECTED.value}
        if role == ROLE_ENGINEER:
            return {"event_kind": TicketEvent.ENGINEER_UPDATE.value}
        if role != ROLE_MANAGER:
            return {"event_kind": TicketEvent.PASSIVE.value}
        if state.get("agent_stage") in {
            AgentStage.ENGINEER_TRACKING,
            AgentStage.WAITING_PARTS,
        }:
            return {"event_kind": TicketEvent.PASSIVE.value}

        feedback = await troubleshooting.classify_feedback(message=message, ticket=ticket)
        mapping = {
            ManagerFeedback.UNSOLVED: TicketEvent.UNSOLVED,
            ManagerFeedback.RESOLVED: TicketEvent.RESOLVED,
            ManagerFeedback.HANDOFF: TicketEvent.HANDOFF,
            ManagerFeedback.UNCLEAR: TicketEvent.PASSIVE,
        }
        return {"feedback": feedback.value, "event_kind": mapping[feedback].value}

    async def build_retrieval_query(state: TicketAgentState) -> dict[str, Any]:
        ticket = state["ticket_snapshot"]
        event = state.get("incoming_event", {})
        query = "；".join(
            item for item in (
                str(ticket.get("subject") or ""),
                str(ticket.get("location") or ""),
                str(ticket.get("problem_description") or ""),
                str(event.get("content") or ""),
            ) if item
        )
        return {"retrieval_query": query[-1000:], "agent_stage": AgentStage.SELF_SERVICE.value}

    async def retrieve_knowledge(state: TicketAgentState) -> dict[str, Any]:
        context = await retriever.retrieve(
            query=state.get("retrieval_query", ""),
            ticket_context=state["ticket_snapshot"],
            limit=5,
        )
        return {"retrieved_context": dict(context)}

    async def generate_guidance(state: TicketAgentState) -> dict[str, Any]:
        guidance = await troubleshooting.generate_guidance(
            ticket=state["ticket_snapshot"],
            message=state.get("incoming_event", {}),
            retrieved_context=state.get("retrieved_context", {}),
        )
        return {"guidance": guidance}

    async def persist_and_notify_guidance(state: TicketAgentState) -> dict[str, Any]:
        event = state.get("incoming_event", {})
        persist_event(state["ticket_id"], event)
        ticket_no = state.get("ticket_no") or f"#{state['ticket_id']}"
        text = f"🔧 {ticket_no} 自助排障建议：\n{state.get('guidance', '')}"
        notify(state["group_id"], text, str(event.get("message_id") or ""))
        prior = str(state.get("conversation_summary") or "")
        current = str(event.get("content") or "").strip()
        summary = "\n".join(part for part in (prior, current) if part)[-2000:]
        return {
            "conversation_summary": summary,
            "agent_stage": AgentStage.WAITING_MANAGER.value,
            "run_outcome": "WAITING_MANAGER",
        }

    async def handoff_engineer(state: TicketAgentState) -> dict[str, Any]:
        event = state.get("incoming_event", {})
        persist_event(state["ticket_id"], event)
        ticket_no = state.get("ticket_no") or f"#{state['ticket_id']}"
        notify(
            state["group_id"],
            f"👷 {ticket_no} 已转入工程师跟进，后续处理、SLA 和完工确认仍由本工单 Agent 持续记录。",
            str(event.get("message_id") or ""),
        )
        return {
            "agent_stage": AgentStage.ENGINEER_TRACKING.value,
            "run_outcome": "ENGINEER_TRACKING",
        }

    async def sync_lifecycle(state: TicketAgentState) -> dict[str, Any]:
        event = state.get("incoming_event", {})
        status = str(state["ticket_snapshot"].get("status") or "")
        kind = state.get("event_kind")
        if status in _TERMINAL_STATUSES or kind == TicketEvent.CLOSED:
            stage = AgentStage.CLOSED
        elif status == TICKET_PENDING_CONFIRM or kind == TicketEvent.ENGINEER_COMPLETED:
            stage = AgentStage.WAITING_CONFIRM
        elif kind == TicketEvent.MANAGER_REJECTED:
            stage = AgentStage.ENGINEER_TRACKING
        elif status == TICKET_NEGOTIATING:
            stage = AgentStage.WAITING_PARTS
        elif kind == TicketEvent.ENGINEER_UPDATE:
            stage = AgentStage.ENGINEER_TRACKING
        else:
            stage = AgentStage(state.get("agent_stage") or AgentStage.TRIAGE)
        prior = str(state.get("conversation_summary") or "")
        current = str(event.get("content") or "").strip()
        summary = "\n".join(part for part in (prior, current) if part)[-2000:]
        return {
            "conversation_summary": summary,
            "agent_stage": stage.value,
            "run_outcome": stage.value,
        }

    def after_load(state: TicketAgentState) -> str:
        return "failure" if state.get("error") else "continue"

    def route_event(state: TicketAgentState) -> str:
        kind = state.get("event_kind")
        if kind in {TicketEvent.NEW_ISSUE, TicketEvent.UNSOLVED}:
            return "troubleshoot"
        if kind == TicketEvent.HANDOFF:
            return "handoff"
        return "lifecycle"

    builder = StateGraph(TicketAgentState)
    builder.add_node("load_ticket_state", load_ticket_state)
    builder.add_node("detect_ticket_event", detect_ticket_event)
    builder.add_node("build_retrieval_query", build_retrieval_query)
    builder.add_node("retrieve_knowledge", retrieve_knowledge)
    builder.add_node("generate_guidance", generate_guidance)
    builder.add_node("persist_and_notify_guidance", persist_and_notify_guidance)
    builder.add_node("handoff_engineer", handoff_engineer)
    builder.add_node("sync_lifecycle", sync_lifecycle)

    builder.add_edge(START, "load_ticket_state")
    builder.add_conditional_edges(
        "load_ticket_state", after_load,
        {"continue": "detect_ticket_event", "failure": END},
    )
    builder.add_conditional_edges(
        "detect_ticket_event", route_event,
        {
            "troubleshoot": "build_retrieval_query",
            "handoff": "handoff_engineer",
            "lifecycle": "sync_lifecycle",
        },
    )
    builder.add_edge("build_retrieval_query", "retrieve_knowledge")
    builder.add_edge("retrieve_knowledge", "generate_guidance")
    builder.add_edge("generate_guidance", "persist_and_notify_guidance")
    builder.add_edge("persist_and_notify_guidance", END)
    builder.add_edge("handoff_engineer", END)
    builder.add_edge("sync_lifecycle", END)
    return builder
