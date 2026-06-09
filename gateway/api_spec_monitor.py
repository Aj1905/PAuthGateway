"""Detect API-spec changes for configured gateway tool sources.

This module is the notification/update foundation for real API integrations:
it reads gateway config, fetches every OpenAPI suite's spec, compares it with
the last stored snapshot, and emits a machine-readable change report.

The gateway reflects OpenAPI specs at load time. This monitor provides the
missing operational loop: detect when upstream specs changed so the user can be
notified and the gateway can be restarted/reloaded by the deployment wrapper.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from gateway.openapi_suite import build_openapi_suite, load_openapi_document


@dataclasses.dataclass
class SpecSnapshot:
    name: str
    fingerprint: str
    tools: dict[str, list[str]]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fingerprint(doc: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()


def _openapi_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    entries = config.get("suites") or []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("kind") == "openapi"]


def _snapshot(entry: dict[str, Any]) -> SpecSnapshot:
    name = str(entry.get("name") or "")
    if not name:
        raise ValueError(f"openapi suite entry missing name: {entry!r}")
    doc = load_openapi_document(entry.get("spec_path"), entry.get("spec_url"))
    suite = build_openapi_suite(
        name=name,
        spec_path=entry.get("spec_path"),
        spec_url=entry.get("spec_url"),
        base_url=entry.get("base_url"),
        signer=entry.get("signer", name),
        headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
    )
    return SpecSnapshot(
        name=name,
        fingerprint=_fingerprint(doc),
        tools=suite.tool_params(),
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "specs": {}}
    return json.loads(path.read_text())


def _write_state(path: Path, snapshots: list[SpecSnapshot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "specs": {
            snap.name: {
                "fingerprint": snap.fingerprint,
                "tools": snap.tools,
            }
            for snap in snapshots
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _diff_tools(old: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, Any]:
    old_names = set(old)
    new_names = set(new)
    changed = sorted(
        name for name in (old_names & new_names)
        if list(old.get(name) or []) != list(new.get(name) or [])
    )
    return {
        "added": sorted(new_names - old_names),
        "removed": sorted(old_names - new_names),
        "changed_params": {
            name: {"old": old.get(name, []), "new": new.get(name, [])}
            for name in changed
        },
    }


def check_config(
    config_path: str | Path,
    state_path: str | Path,
    *,
    update: bool = False,
) -> dict[str, Any]:
    """Return a change report for every OpenAPI suite in a config."""
    config_path = Path(config_path)
    state_path = Path(state_path)
    config = json.loads(config_path.read_text())
    snapshots = [_snapshot(entry) for entry in _openapi_entries(config)]
    state = _load_state(state_path)
    old_specs = state.get("specs") or {}

    changes: list[dict[str, Any]] = []
    for snap in snapshots:
        old = old_specs.get(snap.name) or {}
        old_fingerprint = old.get("fingerprint")
        changed = old_fingerprint != snap.fingerprint
        changes.append(
            {
                "name": snap.name,
                "changed": changed,
                "old_fingerprint": old_fingerprint,
                "new_fingerprint": snap.fingerprint,
                "tool_diff": _diff_tools(old.get("tools") or {}, snap.tools),
            }
        )

    removed = sorted(set(old_specs) - {snap.name for snap in snapshots})
    report = {
        "config": str(config_path),
        "state": str(state_path),
        "changed": any(c["changed"] for c in changes) or bool(removed),
        "openapi_suites": changes,
        "removed_suites": removed,
    }
    if update:
        _write_state(state_path, snapshots)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect OpenAPI spec changes for gateway config.")
    parser.add_argument("--config", required=True, help="gateway JSON config path")
    parser.add_argument(
        "--state",
        default=".gateway/api-spec-state.json",
        help="snapshot state path (default: .gateway/api-spec-state.json)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="write the current snapshot after checking",
    )
    args = parser.parse_args()

    report = check_config(args.config, args.state, update=args.update)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["changed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
