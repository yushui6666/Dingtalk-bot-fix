"""Owns compiled graphs and the async SQLite checkpoint lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from graphs.dispatcher_graph import build_dispatcher_graph
from graphs.retrieval.null import NullKnowledgeRetriever
from graphs.ticket_agent_graph import build_ticket_agent_graph
from graphs.troubleshooting import HybridTroubleshootingService
from logger import get_logger

logger = get_logger(__name__)


class GraphRuntime:
    def __init__(
        self,
        *,
        pipeline: Any,
        checkpoint_path: str | Path,
        model_client: Any | None = None,
        retriever: Any | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._checkpoint_path = Path(checkpoint_path)
        self._model_client = model_client
        self._retriever = retriever or NullKnowledgeRetriever()
        self._saver_context: Any | None = None
        self._checkpointer: Any | None = None
        self._ticket_graph: Any | None = None
        self._dispatcher_graph: Any | None = None
        self._ticket_locks: dict[int, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        self.closed = False
        self.last_graph_path: tuple[str, ...] = ()

    async def start(self) -> None:
        if self._dispatcher_graph is not None:
            return
        async with self._start_lock:
            if self._dispatcher_graph is not None:
                return
            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._saver_context = AsyncSqliteSaver.from_conn_string(str(self._checkpoint_path))
            self._checkpointer = await self._saver_context.__aenter__()
            troubleshooting = HybridTroubleshootingService(self._model_client)
            ticket_builder = build_ticket_agent_graph(
                load_ticket=self._pipeline.graph_load_ticket,
                retriever=self._retriever,
                troubleshooting=troubleshooting,
                notify=self._pipeline.graph_notify_agent,
                persist_event=self._pipeline.graph_persist_agent_event,
            )
            self._ticket_graph = ticket_builder.compile(checkpointer=self._checkpointer)
            dispatcher_builder = build_dispatcher_graph(
                pipeline=self._pipeline,
                invoke_ticket=self.invoke_ticket,
            )
            self._dispatcher_graph = dispatcher_builder.compile()
            self.closed = False
            logger.info("LangGraph runtime ready checkpoint=%s", self._checkpoint_path)

    async def process(self, item: dict[str, Any]) -> str:
        await self.start()
        result = await self._dispatcher_graph.ainvoke(
            {"inbox_item": dict(item), "graph_path": [], "error": ""}
        )
        self.last_graph_path = tuple(result.get("graph_path", ()))
        return str(result.get("processed_status") or "COMPLETED")

    async def invoke_ticket(self, ticket_id: int, event: dict[str, Any]) -> dict[str, Any]:
        if self._ticket_graph is None:
            await self.start()
        # 不同工单并行；同一工单在群内即使相邻提交也严格串行恢复 checkpoint。
        lock = self._ticket_locks.setdefault(ticket_id, asyncio.Lock())
        async with lock:
            config = {"configurable": {"thread_id": f"ticket:{ticket_id}"}}
            result = await self._ticket_graph.ainvoke(
                {"ticket_id": ticket_id, "incoming_event": dict(event)},
                config=config,
            )
            return dict(result)

    async def aclose(self) -> None:
        if self.closed:
            return
        if self._saver_context is not None:
            await self._saver_context.__aexit__(None, None, None)
        self._saver_context = None
        self._checkpointer = None
        self._ticket_graph = None
        self._dispatcher_graph = None
        self._ticket_locks.clear()
        self.closed = True
        logger.info("LangGraph runtime closed")
