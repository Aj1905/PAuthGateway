"""Structured audit events (plan.md observability / audit).

Every plan submission and tool-call decision the gateway makes is recorded as a
structured :class:`AuditEvent` -- permit/deny plus a reason -- so an operator or
SIEM can see what happened without scraping log lines. This is OPERATOR-facing
(unlike agent feedback, S16), so it may carry the internal reason; do not send
these events back into the agent's model context.
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class AuditEvent:
    seq: int                 # per-session ordinal (deterministic; no wall clock)
    kind: str                # "submit" | "tool_call"
    decision: str            # "accept" | "reject" | "permit" | "deny" | "pending"
    tool: str | None         # tool name for tool_call events
    reason_code: str         # a feedback.ReasonCode value, or "accepted"/"rejected"
    reason: str              # the internal, human-readable reason (operator-facing)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class AuditLog:
    """Append-only, in-memory audit trail for one gateway/session."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self, kind: str, decision: str, *, tool: str | None = None,
        reason_code: str = "", reason: str = "",
    ) -> AuditEvent:
        event = AuditEvent(
            seq=len(self._events), kind=kind, decision=decision,
            tool=tool, reason_code=reason_code, reason=reason,
        )
        self._events.append(event)
        return event

    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)
