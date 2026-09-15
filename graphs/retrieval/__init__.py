"""Knowledge retrieval ports used by the ticket agent."""

from graphs.retrieval.base import KnowledgeRetriever, RetrievedContext
from graphs.retrieval.null import NullKnowledgeRetriever

__all__ = ["KnowledgeRetriever", "RetrievedContext", "NullKnowledgeRetriever"]
