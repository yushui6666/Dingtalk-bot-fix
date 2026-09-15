"""Disabled RAG provider used until the knowledge source is designed."""

from __future__ import annotations

from typing import Any

from graphs.retrieval.base import RetrievedContext


class NullKnowledgeRetriever:
    async def retrieve(
        self,
        *,
        query: str,
        ticket_context: dict[str, Any],
        limit: int = 5,
    ) -> RetrievedContext:
        return {
            "documents": [],
            "query": query,
            "provider": "none",
            "enabled": False,
        }
