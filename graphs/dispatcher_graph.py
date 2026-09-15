"""Per-message dispatcher graph around the existing business capabilities."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from graphs.state import DispatcherState, message_from_state, message_to_state


TicketInvoker = Callable[[int, dict[str, Any]], Awaitable[dict[str, Any]]]


def build_dispatcher_graph(*, pipeline: Any, invoke_ticket: TicketInvoker) -> StateGraph:
    async def prepare_message(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "prepare_message"]
        try:
            message = pipeline.graph_prepare_message(state["inbox_item"])
            return {"message": message_to_state(message), "graph_path": path, "error": ""}
        except Exception as exc:
            return {"error": str(exc), "failed_node": "prepare_message", "graph_path": path}

    async def archive_attachments(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "archive_attachments"]
        try:
            await pipeline.graph_archive_attachments(message_from_state(state["message"]))
            return {"graph_path": path, "error": ""}
        except Exception as exc:
            return {"error": str(exc), "failed_node": "archive_attachments", "graph_path": path}

    async def load_dispatch_context(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "load_dispatch_context"]
        try:
            context = pipeline.graph_load_dispatch_context(message_from_state(state["message"]))
            return {"dispatch_context": context, "graph_path": path, "error": ""}
        except Exception as exc:
            return {"error": str(exc), "failed_node": "load_dispatch_context", "graph_path": path}

    async def process_business(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "process_business"]
        try:
            status = await pipeline.graph_process_business(
                state["inbox_item"], message_from_state(state["message"])
            )
            return {"processed_status": status, "graph_path": path, "error": ""}
        except Exception as exc:
            return {"error": str(exc), "failed_node": "process_business", "graph_path": path}

    async def resolve_ticket_agent(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "resolve_ticket_agent"]
        try:
            message = message_from_state(state["message"])
            ticket_id = pipeline.graph_resolve_agent_ticket(message)
            if ticket_id is None:
                return {"graph_path": path, "error": ""}
            return {
                "target_ticket_id": ticket_id,
                "ticket_thread_id": f"ticket:{ticket_id}",
                "graph_path": path,
                "error": "",
            }
        except Exception as exc:
            return {"agent_error": str(exc), "graph_path": path, "error": ""}

    async def invoke_ticket_agent(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "invoke_ticket_agent"]
        try:
            message = state["message"]
            event = pipeline.graph_build_ticket_event(message)
            result = await invoke_ticket(state["target_ticket_id"], event)
            return {"ticket_agent_result": result, "graph_path": path, "error": ""}
        except Exception as exc:
            return {"agent_error": str(exc), "graph_path": path, "error": ""}

    async def handle_failure(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "handle_failure"]
        message = message_from_state(state["message"]) if state.get("message") else None
        status = pipeline.graph_handle_exception(
            state["inbox_item"], message, RuntimeError(state.get("error") or "graph failure")
        )
        return {"processed_status": status, "graph_path": path}

    async def finalize_inbox(state: DispatcherState) -> dict[str, Any]:
        path = [*state.get("graph_path", []), "finalize_inbox"]
        pipeline.graph_record_completion(state)
        return {"graph_path": path}

    def after_step(state: DispatcherState) -> str:
        return "failure" if state.get("error") else "continue"

    def after_business(state: DispatcherState) -> str:
        if state.get("error"):
            return "failure"
        return "continue" if state.get("processed_status") == "COMPLETED" else "finish"

    def after_resolve(state: DispatcherState) -> str:
        if state.get("error"):
            return "failure"
        return "ticket" if state.get("target_ticket_id") is not None else "finish"

    builder = StateGraph(DispatcherState)
    builder.add_node("prepare_message", prepare_message)
    builder.add_node("archive_attachments", archive_attachments)
    builder.add_node("load_dispatch_context", load_dispatch_context)
    builder.add_node("process_business", process_business)
    builder.add_node("resolve_ticket_agent", resolve_ticket_agent)
    builder.add_node("invoke_ticket_agent", invoke_ticket_agent)
    builder.add_node("handle_failure", handle_failure)
    builder.add_node("finalize_inbox", finalize_inbox)

    builder.add_edge(START, "prepare_message")
    builder.add_conditional_edges(
        "prepare_message", after_step,
        {"continue": "archive_attachments", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "archive_attachments", after_step,
        {"continue": "load_dispatch_context", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "load_dispatch_context", after_step,
        {"continue": "process_business", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "process_business", after_business,
        {
            "continue": "resolve_ticket_agent",
            "finish": "finalize_inbox",
            "failure": "handle_failure",
        },
    )
    builder.add_conditional_edges(
        "resolve_ticket_agent", after_resolve,
        {"ticket": "invoke_ticket_agent", "finish": "finalize_inbox", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "invoke_ticket_agent", after_step,
        {"continue": "finalize_inbox", "failure": "handle_failure"},
    )
    builder.add_edge("handle_failure", "finalize_inbox")
    builder.add_edge("finalize_inbox", END)
    return builder
