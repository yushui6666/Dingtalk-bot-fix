"""Stable values shared by graph state and conditional edges."""

from __future__ import annotations

from enum import StrEnum


class AgentStage(StrEnum):
    TRIAGE = "TRIAGE"
    SELF_SERVICE = "SELF_SERVICE"
    WAITING_MANAGER = "WAITING_MANAGER"
    ENGINEER_TRACKING = "ENGINEER_TRACKING"
    WAITING_PARTS = "WAITING_PARTS"
    WAITING_CONFIRM = "WAITING_CONFIRM"
    CLOSED = "CLOSED"


class TicketEvent(StrEnum):
    NEW_ISSUE = "NEW_ISSUE"
    UNSOLVED = "UNSOLVED"
    RESOLVED = "RESOLVED"
    HANDOFF = "HANDOFF"
    ENGINEER_UPDATE = "ENGINEER_UPDATE"
    ENGINEER_COMPLETED = "ENGINEER_COMPLETED"
    MANAGER_REJECTED = "MANAGER_REJECTED"
    PASSIVE = "PASSIVE"
    CLOSED = "CLOSED"
