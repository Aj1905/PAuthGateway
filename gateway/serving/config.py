"""Config-driven suite registration for the gateway.

A gateway deployment registers one or more tool sources (suites) to put
under PAuth enforcement. Suite backends are pluggable via the small
adapter table below.

Config schema (JSON for now -- swap to TOML if dependency budget allows)::

    {
      "merged_suite_name": "user_default",
      "suites": [
        { "name": "shopping",  "kind": "shopping" },
        { "name": "gmail",     "kind": "mcp",      "url": "http://127.0.0.1:8090" },
        { "name": "billing",   "kind": "openapi",  "spec_path": "billing.openapi.json" },
        { "name": "banking",   "kind": "agentdojo", "suite": "banking" }
      ]
    }

``kind``s currently supported:

* ``shopping``   -- the self-contained demo suite.
* ``mcp``        -- any MCP server reachable at ``url``.
* ``openapi``    -- an HTTP API described by an OpenAPI 3.x document.
* ``agentdojo``  -- an AgentDojo suite by name (``banking``/``slack``/...).

The gateway loads the config and builds one ``SuiteSpec`` per entry,
then folds them with :func:`gateway.registry.merge_suites`. The merged
suite is what the runtime gateway / ``AgentChannel`` operates over.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

from pauth.suites.base import SuiteSpec
from pauth.suites.shopping import build_suite as build_shopping_suite

from gateway.providers.mcp_suite import build_mcp_suite, build_mcp_suite_stdio
from gateway.providers.openapi_suite import build_openapi_suite
from gateway.runtime.policy import PolicySpec
from gateway.providers.registry import merge_suites
from gateway.providers.suite_filter import SuiteFilter


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------

def _build_shopping(entry: dict[str, Any]) -> SuiteSpec:
    return build_shopping_suite()


def _build_mcp(entry: dict[str, Any]) -> SuiteSpec:
    url = entry.get("url")
    command = entry.get("command")
    if url and command:
        raise ValueError(f"mcp suite {entry.get('name')!r}: pick 'url' OR 'command', not both")
    if url:
        if not isinstance(url, str):
            raise ValueError(f"mcp suite {entry.get('name')!r}: 'url' must be a string")
        return build_mcp_suite(
            name=entry["name"], url=url, signer=entry.get("signer", entry["name"])
        )
    if command:
        if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
            raise ValueError(f"mcp suite {entry.get('name')!r}: 'command' must be list[str]")
        return build_mcp_suite_stdio(
            name=entry["name"], command=command, signer=entry.get("signer", entry["name"])
        )
    raise ValueError(f"mcp suite {entry.get('name')!r} requires 'url' or 'command'")


def _build_agentdojo(entry: dict[str, Any]) -> SuiteSpec:
    # Deferred import so the adapter is optional.
    from benchmarks.agentdojo_adapter import load_suite
    suite_name = entry.get("suite") or entry["name"]
    return load_suite(suite_name)


def _build_openapi(entry: dict[str, Any]) -> SuiteSpec:
    headers = entry.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError(f"openapi suite {entry.get('name')!r}: 'headers' must be an object")
    return build_openapi_suite(
        name=entry["name"],
        spec_path=entry.get("spec_path"),
        spec_url=entry.get("spec_url"),
        base_url=entry.get("base_url"),
        signer=entry.get("signer", entry["name"]),
        headers={str(k): str(v) for k, v in headers.items()},
    )


_BUILDERS: dict[str, Callable[[dict[str, Any]], SuiteSpec]] = {
    "shopping": _build_shopping,
    "mcp": _build_mcp,
    "openapi": _build_openapi,
    "agentdojo": _build_agentdojo,
}


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

@dataclasses.dataclass
class LoadedConfig:
    merged_name: str
    sources: dict[str, SuiteSpec]
    merged: SuiteSpec
    # Operand policy declared per source: ``{source_name: {tool_name: [param_name, ...]}}``.
    # Resolved to ``PolicySpec`` against the merged tool schema.
    policy: PolicySpec
    # Plan-time suite selection.
    suite_filter: SuiteFilter


def load_config(path: str | Path) -> LoadedConfig:
    """Load a gateway config and return the merged SuiteSpec.

    Raises ``ValueError`` on schema problems and ``MCPError`` /
    ``RuntimeError`` from the underlying suite builders.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"gateway config not found: {path}")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("gateway config must be a JSON object")

    merged_name = raw.get("merged_suite_name", "default")
    if not isinstance(merged_name, str) or not merged_name:
        raise ValueError("merged_suite_name must be a non-empty string")
    suite_entries = raw.get("suites", [])
    if not isinstance(suite_entries, list) or not suite_entries:
        raise ValueError("config must contain a non-empty 'suites' list")

    sources: dict[str, SuiteSpec] = {}
    raw_policy: dict[str, list[str]] = {}  # merged: tool_name -> [param_names]
    for entry in suite_entries:
        if not isinstance(entry, dict):
            raise ValueError(f"each suite entry must be an object, got {entry!r}")
        name = entry.get("name")
        kind = entry.get("kind")
        if not isinstance(name, str) or not name or not isinstance(kind, str) or not kind:
            raise ValueError(f"suite entry missing 'name' or 'kind': {entry!r}")
        builder = _BUILDERS.get(kind)
        if builder is None:
            raise ValueError(
                f"unknown suite kind {kind!r} (known: {sorted(_BUILDERS)})"
            )
        if name in sources:
            raise ValueError(f"duplicate suite name {name!r}")
        sources[name] = builder(entry)

        # Collect operand-policy declarations: per-suite ``{tool: [param,...]}``
        operand_policy = entry.get("operand_policy") or {}
        if not isinstance(operand_policy, dict):
            raise ValueError(f"operand_policy for {name!r} must be an object")
        for tool_name, free_params in operand_policy.items():
            if (
                not isinstance(tool_name, str)
                or not isinstance(free_params, list)
                or any(not isinstance(param, str) for param in free_params)
            ):
                raise ValueError(
                    f"operand_policy for {name!r}/{tool_name!r} must be a list of param names"
                )
            if tool_name in raw_policy:
                raise ValueError(
                    f"operand_policy for tool {tool_name!r} already declared elsewhere; "
                    f"tool names are gateway-global after merge"
                )
            raw_policy[tool_name] = list(free_params)

    merged = merge_suites(merged_name, sources)
    policy = PolicySpec.from_param_names(raw_policy, merged.tool_params()) if raw_policy else PolicySpec({})

    # Suite filter knobs are top-level (not per source).
    filter_cfg = raw.get("suite_filter") or {}
    if not isinstance(filter_cfg, dict):
        raise ValueError("suite_filter must be an object")
    top_k = filter_cfg.get("top_k")
    min_score = filter_cfg.get("min_score", 1)
    if top_k is not None and (
        isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1
    ):
        raise ValueError("suite_filter.top_k must be a positive integer or null")
    if (
        isinstance(min_score, bool)
        or not isinstance(min_score, int)
        or min_score < 0
    ):
        raise ValueError("suite_filter.min_score must be a non-negative integer")
    suite_filter = SuiteFilter(
        top_k=top_k,
        min_score=min_score,
    )

    return LoadedConfig(
        merged_name=merged_name,
        sources=sources,
        merged=merged,
        policy=policy,
        suite_filter=suite_filter,
    )


# --------------------------------------------------------------------------
# Convenience: a single suite_loader the existing Gateway interface accepts.
# --------------------------------------------------------------------------

def suite_loader_for(loaded: LoadedConfig) -> Callable[[str], SuiteSpec]:
    """Return a suite_loader that resolves to the merged or any source suite.

    Asking for ``loaded.merged_name`` returns the merged suite. Asking for
    a source name (``shopping`` / ``gmail`` / ...) returns that source
    suite. This lets the existing single-suite ``Gateway`` work over the
    merged universe without changes.
    """
    def loader(name: str) -> SuiteSpec:
        if name == loaded.merged_name:
            return loaded.merged
        source = loaded.sources.get(name)
        if source is None:
            raise ValueError(
                f"suite {name!r} is not in the loaded config "
                f"(known: {sorted([loaded.merged_name, *loaded.sources])})"
            )
        return source
    return loader


def prompt_suite_loader_for(
    loaded: LoadedConfig,
) -> Callable[[str, str], SuiteSpec]:
    """Return a prompt-scoped loader that actually applies ``suite_filter``.

    Directly named source suites remain explicit operator choices.  Only the
    merged universe is narrowed, which is the surface whose tool-schema growth
    the filter is designed to control.
    """
    cache: dict[tuple[str, tuple[str, ...]], SuiteSpec] = {}

    def loader(prompt: str, name: str) -> SuiteSpec:
        if name != loaded.merged_name:
            return suite_loader_for(loaded)(name)
        selection = loaded.suite_filter.filter(prompt, loaded.sources)
        selected_names = tuple(selection.selected)
        if set(selected_names) == set(loaded.sources):
            return loaded.merged
        key = (loaded.merged_name, selected_names)
        if key not in cache:
            cache[key] = merge_suites(
                loaded.merged_name,
                {source: loaded.sources[source] for source in selected_names},
            )
        return cache[key]

    return loader
