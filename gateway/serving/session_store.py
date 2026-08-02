"""Durable session persistence for restart-safe call interception.

The store keeps the clean prompt and planner configuration needed to rebuild a
session, plus the Gateway's durable execution state.  Execution state is safety
critical: malformed data must never be treated as an empty ledger because that
would make an already-dispatched tool call eligible for replay.

This file backend is for one Gateway process. Active-active deployments require
a managed KV with conditional writes/CAS; sharing this JSON file across processes
does not provide an at-most-once reservation.
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class SessionStoreCorruptionError(ValueError):
    """The persisted session document is not valid, trusted runtime state."""


_ENTRY_REQUIRED_FIELDS = frozenset({"prompt", "config"})
_ENTRY_OPTIONAL_FIELDS = frozenset({"owner", "execution_state"})
_ENTRY_FIELDS = _ENTRY_REQUIRED_FIELDS | _ENTRY_OPTIONAL_FIELDS


def _clone_json_value(value: Any, *, context: str, active: set[int] | None = None) -> Any:
    """Validate and clone a strict JSON value without coercing dictionary keys."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{context} contains a non-finite number")
        return value

    if active is None:
        active = set()
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise TypeError(f"{context} contains a reference cycle")
        active.add(identity)
        try:
            return [
                _clone_json_value(item, context=f"{context}[{index}]", active=active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise TypeError(f"{context} contains a reference cycle")
        active.add(identity)
        try:
            cloned: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{context} contains a non-string key")
                cloned[key] = _clone_json_value(
                    item,
                    context=f"{context}.{key}",
                    active=active,
                )
            return cloned
        finally:
            active.remove(identity)
    raise TypeError(f"{context} contains unsupported value {type(value).__name__}")


def _validated_document(raw: Any) -> dict[str, dict[str, Any]]:
    """Return a detached, structurally validated session document."""
    if not isinstance(raw, dict):
        raise SessionStoreCorruptionError("session store root must be a JSON object")

    validated: dict[str, dict[str, Any]] = {}
    for session_id, raw_entry in raw.items():
        if not isinstance(session_id, str) or not session_id:
            raise SessionStoreCorruptionError("session ids must be non-empty strings")
        if not isinstance(raw_entry, dict):
            raise SessionStoreCorruptionError(
                f"session {session_id!r} entry must be a JSON object"
            )

        fields = set(raw_entry)
        missing = _ENTRY_REQUIRED_FIELDS - fields
        unknown = fields - _ENTRY_FIELDS
        if missing:
            raise SessionStoreCorruptionError(
                f"session {session_id!r} is missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise SessionStoreCorruptionError(
                f"session {session_id!r} has unknown fields: {', '.join(sorted(unknown))}"
            )
        if not isinstance(raw_entry["prompt"], str):
            raise SessionStoreCorruptionError(
                f"session {session_id!r} prompt must be a string"
            )
        if not isinstance(raw_entry["config"], dict):
            raise SessionStoreCorruptionError(
                f"session {session_id!r} config must be a JSON object"
            )
        if "owner" in raw_entry and not isinstance(raw_entry["owner"], str):
            raise SessionStoreCorruptionError(
                f"session {session_id!r} owner must be a string"
            )
        if "execution_state" in raw_entry and raw_entry["execution_state"] is not None:
            if not isinstance(raw_entry["execution_state"], dict):
                raise SessionStoreCorruptionError(
                    f"session {session_id!r} execution_state must be an object or null"
                )

        # A legacy entry may omit execution_state.  Preserve that absence so the
        # restore layer can distinguish and quarantine it instead of silently
        # treating it as a fresh execution ledger.
        try:
            validated[session_id] = _clone_json_value(
                raw_entry,
                context=f"session {session_id!r}",
            )
        except TypeError as exc:
            raise SessionStoreCorruptionError(str(exc)) from exc
    return validated


class SessionStore:
    """Durable JSON map of ``session_id -> session reconstruction state``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SessionStoreCorruptionError(
                    f"invalid session store JSON at {self._path}: {exc}"
                ) from exc
            self._data = _validated_document(raw)

    def record(
        self,
        session_id: str,
        prompt: str,
        config: dict[str, Any] | None = None,
        owner: str | None = None,
        execution_state: dict[str, Any] | None = None,
    ) -> None:
        """Persist the inputs and execution state needed to restore a session.

        Re-recording a session without an ``owner`` or ``execution_state`` does
        not erase either existing value.  Erasing a durable execution ledger by
        accident would re-enable already-dispatched calls.
        """
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("session_id must be a non-empty string")
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if config is not None and not isinstance(config, dict):
            raise TypeError("config must be a dictionary")
        if owner is not None and not isinstance(owner, str):
            raise TypeError("owner must be a string or None")
        if execution_state is not None and not isinstance(execution_state, dict):
            raise TypeError("execution_state must be a dictionary or None")

        with self._lock:
            previous = self._data.get(session_id, {})
            entry: dict[str, Any] = {
                "prompt": prompt,
                "config": _clone_json_value(config or {}, context="config"),
            }
            if owner is not None:
                entry["owner"] = owner
            elif "owner" in previous:
                entry["owner"] = previous["owner"]

            if execution_state is not None:
                entry["execution_state"] = _clone_json_value(
                    execution_state,
                    context="execution_state",
                )
            elif "execution_state" in previous:
                entry["execution_state"] = copy.deepcopy(previous["execution_state"])
            else:
                # New-format records always carry this field.  Only data loaded
                # from a legacy file may lack it.
                entry["execution_state"] = None

            candidate = copy.deepcopy(self._data)
            candidate[session_id] = entry
            self._commit(candidate)

    def update_execution_state(
        self,
        session_id: str,
        state: dict[str, Any],
    ) -> None:
        """Durably replace an existing session's execution safety state."""
        if not isinstance(state, dict):
            raise TypeError("execution state must be a dictionary")
        state_copy = _clone_json_value(state, context="execution_state")
        with self._lock:
            if session_id not in self._data:
                raise KeyError(session_id)
            candidate = copy.deepcopy(self._data)
            candidate[session_id]["execution_state"] = state_copy
            self._commit(candidate)

    def remove(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._data:
                return
            candidate = copy.deepcopy(self._data)
            del candidate[session_id]
            self._commit(candidate)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data.get(session_id)
            return copy.deepcopy(entry) if entry is not None else None

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _commit(self, candidate: dict[str, dict[str, Any]]) -> None:
        """Durably publish ``candidate`` with file and directory fsyncs."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(candidate, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            replaced = True

            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self._path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if not replaced:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
        self._data = candidate
