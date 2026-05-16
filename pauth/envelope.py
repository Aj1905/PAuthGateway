"""Envelopes: binding a concrete value to its symbolic provenance.

Paper sec. 3.4 / Figure 3: an envelope holds a concrete value together with a
symbolic value (the computation that produced it) and is *signed by the
producing server*.  A server that receives a call inspects the envelopes of
its operands to verify, in a tamper-resistant way, that each operand was
produced by the task-implied computation.

In the single-host AgentDojo setting the envelope store is shared memory
(paper sec. 4.1.3).  We still attach real HMAC signatures so the mechanism --
and an attacker's inability to forge an upstream value -- is faithfully
reproduced.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
from typing import Any


def _to_jsonable(value: Any) -> Any:
    """Best-effort stable serialisation for signing/comparison."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "model_dump"):  # pydantic v2 (AgentDojo tool returns)
        try:
            return _to_jsonable(value.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return repr(value)


def _digest(symbolic: str, concrete: Any) -> bytes:
    blob = json.dumps(
        {"symbolic": symbolic, "concrete": _to_jsonable(concrete)},
        sort_keys=True,
        default=str,
    )
    return blob.encode("utf-8")


@dataclasses.dataclass
class Envelope:
    """A signed binding ``<concrete, symbolic>`` produced by ``signer``."""

    concrete: Any
    symbolic: str
    signer: str
    signature: str

    def __repr__(self) -> str:  # mirrors the angle-bracket notation of Fig. 3
        return f"<{self.concrete!r}, {self.symbolic} | sig:{self.signer}>"


class KeyRing:
    """Per-server signing keys.

    Each server holds its own secret.  In a real multi-host deployment only
    the owning server knows its key; the others verify with shared public
    material.  HMAC with a per-server secret is a faithful single-host stand-in.
    """

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def key(self, signer: str) -> bytes:
        if signer not in self._keys:
            self._keys[signer] = secrets.token_bytes(32)
        return self._keys[signer]


def make_envelope(concrete: Any, symbolic: str, signer: str, keyring: KeyRing) -> Envelope:
    """Create a signed envelope (a server signing one of its results)."""
    sig = hmac.new(keyring.key(signer), _digest(symbolic, concrete), hashlib.sha256).hexdigest()
    return Envelope(concrete=concrete, symbolic=symbolic, signer=signer, signature=sig)


def verify(env: Envelope, keyring: KeyRing) -> bool:
    """Verify an envelope's signature against the producing server's key."""
    expected = hmac.new(
        keyring.key(env.signer), _digest(env.symbolic, env.concrete), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, env.signature)


class TamperedEnvelopeError(Exception):
    """Raised when an envelope fails signature verification."""


class EnvelopeStore:
    """Dictionary of envelopes indexed by symbolic value (paper sec. 4.1.3)."""

    def __init__(self, keyring: KeyRing) -> None:
        self._d: dict[str, Envelope] = {}
        self._keyring = keyring

    def put(self, env: Envelope) -> None:
        self._d[env.symbolic] = env

    def has(self, symbolic: str) -> bool:
        return symbolic in self._d

    def get(self, symbolic: str) -> Any:
        """Return the concrete value for ``symbolic``, verifying its signature.

        The verification step is where an agent-fabricated or tampered value
        is rejected: the agent cannot produce a valid signature for a server.
        """
        env = self._d.get(symbolic)
        if env is None:
            raise KeyError(symbolic)
        if not verify(env, self._keyring):
            raise TamperedEnvelopeError(f"signature check failed for {symbolic}")
        return env.concrete

    def envelopes(self) -> dict[str, Envelope]:
        return dict(self._d)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a structured output into stable field-path keys.

    Paper sec. 4.1.3: "the envelope handler flattens them into field paths and
    stores each leaf value under a stable key such as ``res.user.id`` ... and a
    list element field under ``items.0.price``."  Used for display / debugging;
    runtime evaluation walks the live object directly.
    """
    out: dict[str, Any] = {}
    js = _to_jsonable(value)

    def _rec(v: Any, p: str) -> None:
        if isinstance(v, dict):
            for k, sub in v.items():
                _rec(sub, f"{p}.{k}" if p else str(k))
        elif isinstance(v, list):
            for i, sub in enumerate(v):
                _rec(sub, f"{p}.{i}" if p else str(i))
        else:
            out[p or "<value>"] = v

    _rec(js, prefix)
    return out
