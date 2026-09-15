"""RAG boundary. A real provider can replace the null implementation later."""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class RetrievedContext(TypedDict):
    documents: list[dict[str, Any]]
    query: str
    provider: str
    enabled: bool


class KnowledgeRetriever(Protocol):
    async def retrieve(
        self,
        *,
        query: str,
        ticket_context: dict[str, Any],
        limit: int = 5,
    ) -> RetrievedContext: ...
