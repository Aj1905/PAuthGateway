"""Session persistence (B1): survive a gateway restart.

In-memory sessions are lost on ``systemctl restart``. This store persists, per
session, the inputs needed to *reconstruct* it -- the clean user prompt and the
planner config -- to a JSON file. On startup a deployment can replay them to
rebuild the plans. It deliberately does NOT serialize live enforcer/envelope
state (HMAC keys, compiled rules, mid-task observations): those are ephemeral,
and a task whose observations are lost is simply re-driven by
re-submitting the prompt.

This is the file-backed near-term store; the cloud topology swaps the backend
for a managed KV (DynamoDB / Cosmos) behind the same interface.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class SessionStore:
    """Append/update JSON map of ``session_id -> {prompt, config}``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def record(self, session_id: str, prompt: str, config: dict[str, Any] | None = None) -> None:
        """Persist the inputs needed to reconstruct a session."""
        self._data[session_id] = {"prompt": prompt, "config": dict(config or {})}
        self._flush()

    def remove(self, session_id: str) -> None:
        if self._data.pop(session_id, None) is not None:
            self._flush()

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._data.get(session_id)

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def _flush(self) -> None:
        """Atomic write (tmp + rename) so a crash never leaves a torn file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
