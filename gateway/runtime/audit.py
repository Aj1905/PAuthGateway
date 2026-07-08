"""Structured audit events (docs/plan.md observability / audit).

Every plan submission and tool-call decision the gateway makes is recorded as a
structured :class:`AuditEvent` -- permit/deny plus a reason -- so an operator or
SIEM can see what happened without scraping log lines. This is OPERATOR-facing
(unlike agent feedback, S16), so it may carry the internal reason; do not send
these events back into the agent's model context.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
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
    """Append-only audit trail for one gateway/session.

    In-memory by default. When ``path`` is given, every recorded event is also
    appended as one JSON line (JSONL) so the trail survives a restart and can be
    tailed by a SIEM. The file is operator-facing (it carries the internal
    reason, which may quote an operand value); keep it off any path the agent
    can read, exactly like the events themselves.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._events: list[AuditEvent] = []
        self._path = Path(path) if path else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, kind: str, decision: str, *, tool: str | None = None,
        reason_code: str = "", reason: str = "",
    ) -> AuditEvent:
        event = AuditEvent(
            seq=len(self._events), kind=kind, decision=decision,
            tool=tool, reason_code=reason_code, reason=reason,
        )
        self._events.append(event)
        self._persist(event)
        return event

    def _persist(self, event: AuditEvent) -> None:
        if self._path is None:
            return
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        # Append-only; O_APPEND writes are atomic for a single line, so a crash
        # never tears an event and concurrent sessions never interleave one.
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)
