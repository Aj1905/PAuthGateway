"""``SuiteSpec`` backed by an OpenAPI document.

This adapter reflects a machine-readable API specification into PAuth's
``SuiteSpec`` boundary. It is intentionally conservative: it supports the
common OpenAPI 3.x shapes needed to turn HTTP operations into tools, while
leaving authentication and complex content negotiation to later deployment
adapters.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from pauth.codegen import ToolDoc
from pauth.suites.base import SuiteSpec, ToolSpec


class OpenAPIError(RuntimeError):
    """Raised when an OpenAPI document cannot be reflected safely."""


@dataclasses.dataclass(frozen=True)
class _ParamBinding:
    """How one tool parameter maps onto an HTTP request."""

    name: str
    location: str  # path | query | header | body | raw_body
    wire_name: str
    required: bool = False


@dataclasses.dataclass(frozen=True)
class _Operation:
    tool_name: str
    method: str
    path: str
    bindings: list[_ParamBinding]
    request_body_required: bool = False


_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Only these URL schemes may be dereferenced. urllib.request.urlopen also
# honours file://, ftp://, data://, etc.; without this gate an untrusted
# OpenAPI document could set servers[0].url (-> base_url) to file:///etc/passwd
# and every reflected tool call would read local files (SSRF).
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _is_link_local_host(host: str) -> bool:
    """True iff ``host`` is a literal link-local IP (the cloud-metadata range).

    Only link-local (169.254.0.0/16, fe80::/10) is blocked -- that covers the
    IMDS endpoint 169.254.169.254, which is never a legitimate API backend.
    Loopback/private ranges are intentionally NOT blocked: this tool is designed
    to front localhost and internal SaaS (see config.py examples). Literal IPs
    only; DNS-rebinding to a link-local address is a documented residual risk.
    """
    candidate = host.strip("[]")  # bracketed IPv6 literal
    try:
        return ipaddress.ip_address(candidate).is_link_local
    except ValueError:
        return False


def _require_http_url(url: str, context: str) -> None:
    """Reject a URL that is not http/https or points at a link-local host."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise OpenAPIError(
            f"{context}: refusing non-http(s) URL {url!r} "
            f"(scheme {scheme or '<none>'!r} is not allowed)"
        )
    if parts.hostname and _is_link_local_host(parts.hostname):
        raise OpenAPIError(
            f"{context}: refusing link-local host {parts.hostname!r} "
            "(cloud-metadata/SSRF target)"
        )


def load_openapi_document(path: str | Path | None = None, url: str | None = None) -> dict[str, Any]:
    """Load an OpenAPI document from a local path or URL."""
    if bool(path) == bool(url):
        raise OpenAPIError("openapi suite requires exactly one of 'spec_path' or 'spec_url'")
    if url:
        _require_http_url(url, "openapi spec_url")
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 -- scheme gated above
            raw = resp.read().decode("utf-8")
    else:
        raw = Path(path or "").read_text()
    return _parse_document(raw, str(url or path))


def _parse_document(raw: str, source: str) -> dict[str, Any]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-untyped]
        except Exception as exc:  # noqa: BLE001
            raise OpenAPIError(
                f"{source}: not JSON, and PyYAML is unavailable for YAML parsing"
            ) from exc
        loaded = yaml.safe_load(raw)
        if not isinstance(loaded, dict):
            raise OpenAPIError(f"{source}: YAML document is not an object")
        doc = loaded
    if not isinstance(doc, dict):
        raise OpenAPIError(f"{source}: OpenAPI document is not an object")
    if "paths" not in doc or not isinstance(doc["paths"], dict):
        raise OpenAPIError(f"{source}: OpenAPI document has no object 'paths'")
    return doc


def _resolve_ref(doc: dict[str, Any], value: Any) -> Any:
    """Resolve local JSON-pointer refs inside an OpenAPI document."""
    if not isinstance(value, dict) or "$ref" not in value:
        return value
    ref = value["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise OpenAPIError(f"only local OpenAPI refs are supported, got {ref!r}")
    cur: Any = doc
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or key not in cur:
            raise OpenAPIError(f"unresolvable OpenAPI ref {ref!r}")
        cur = cur[key]
    return _resolve_ref(doc, cur)


def _schema_type(doc: dict[str, Any], schema: Any) -> str:
    schema = _resolve_ref(doc, schema)
    if not isinstance(schema, dict):
        return "any"
    if "oneOf" in schema or "anyOf" in schema:
        variants = schema.get("oneOf") or schema.get("anyOf") or []
        return "|".join(_schema_type(doc, s) for s in variants) or "any"
    if "allOf" in schema:
        return "object"
    t = schema.get("type")
    if t == "string":
        return "string"
    if t == "integer":
        return "integer"
    if t == "number":
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        return f"list of {_schema_type(doc, schema.get('items', {}))}"
    if t == "object" or "properties" in schema:
        props = schema.get("properties")
        if isinstance(props, dict):
            inner = ", ".join(
                f"{name}: {_schema_type(doc, prop)}" for name, prop in props.items()
            )
            return f"object {{{inner}}}"
        return "object"
    if isinstance(t, list):
        return "|".join(_schema_type(doc, {"type": item}) for item in t)
    return "any"


def _json_schema(content: dict[str, Any]) -> dict[str, Any]:
    media = content.get("application/json")
    if isinstance(media, dict) and isinstance(media.get("schema"), dict):
        return media["schema"]
    return {}


def _response_schema(doc: dict[str, Any], operation: dict[str, Any]) -> str:
    responses = operation.get("responses") or {}
    if not isinstance(responses, dict):
        return "object"
    for status in ("200", "201", "202", "default"):
        response = _resolve_ref(doc, responses.get(status))
        if not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        if isinstance(content, dict):
            schema = _json_schema(content)
            if schema:
                return _schema_type(doc, schema)
    return "object"


def _safe_tool_name(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_").lower()
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        safe = "operation"
    if safe[0].isdigit():
        safe = f"op_{safe}"
    return safe


def _operation_name(method: str, path: str, operation: dict[str, Any]) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        return _safe_tool_name(operation_id)
    path_part = path.replace("{", "").replace("}", "")
    return _safe_tool_name(f"{method}_{path_part}")


def _parameter_entries(
    doc: dict[str, Any],
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = list(path_item.get("parameters") or []) + list(operation.get("parameters") or [])
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        param = _resolve_ref(doc, entry)
        if not isinstance(param, dict):
            continue
        name = param.get("name")
        location = param.get("in")
        if not isinstance(name, str) or location not in {"path", "query", "header"}:
            continue
        key = (location, name)
        if key in seen:
            continue
        seen.add(key)
        out.append(param)
    return out


def _body_schema(doc: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    body = _resolve_ref(doc, operation.get("requestBody"))
    if not isinstance(body, dict):
        return {}
    content = body.get("content") or {}
    if not isinstance(content, dict):
        return {}
    return _resolve_ref(doc, _json_schema(content))


def _body_required(doc: dict[str, Any], operation: dict[str, Any]) -> bool:
    body = _resolve_ref(doc, operation.get("requestBody"))
    return bool(isinstance(body, dict) and body.get("required"))


def _tool_from_operation(
    doc: dict[str, Any],
    method: str,
    path: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
    signer: str,
) -> tuple[ToolSpec, _Operation]:
    tool_name = _operation_name(method, path, operation)
    params: list[dict[str, str]] = []
    bindings: list[_ParamBinding] = []

    used_names: set[str] = set()
    for param in _parameter_entries(doc, path_item, operation):
        wire_name = str(param["name"])
        name = _safe_tool_name(wire_name)
        if name in used_names:
            name = _safe_tool_name(f"{param['in']}_{wire_name}")
        used_names.add(name)
        schema = _resolve_ref(doc, param.get("schema") or {})
        params.append(
            {
                "name": name,
                "type": _schema_type(doc, schema),
                "desc": (param.get("description") or "").strip(),
            }
        )
        bindings.append(
            _ParamBinding(
                name=name,
                location=str(param["in"]),
                wire_name=wire_name,
                required=bool(param.get("required") or param["in"] == "path"),
            )
        )

    body_schema = _body_schema(doc, operation)
    body_required = _body_required(doc, operation)
    body_schema = _resolve_ref(doc, body_schema)
    if isinstance(body_schema, dict) and (
        body_schema.get("type") == "object" or isinstance(body_schema.get("properties"), dict)
    ):
        properties = body_schema.get("properties") or {}
        required = set(body_schema.get("required") or [])
        for prop_name, prop_schema in properties.items():
            if not isinstance(prop_name, str):
                continue
            name = _safe_tool_name(prop_name)
            if name in used_names:
                name = _safe_tool_name(f"body_{prop_name}")
            used_names.add(name)
            prop_schema = _resolve_ref(doc, prop_schema)
            params.append(
                {
                    "name": name,
                    "type": _schema_type(doc, prop_schema),
                    "desc": (prop_schema.get("description") or "").strip()
                    if isinstance(prop_schema, dict)
                    else "",
                }
            )
            bindings.append(
                _ParamBinding(
                    name=name,
                    location="body",
                    wire_name=prop_name,
                    required=body_required or prop_name in required,
                )
            )
    elif body_schema:
        name = "body"
        if name in used_names:
            name = "request_body"
        params.append(
            {
                "name": name,
                "type": _schema_type(doc, body_schema),
                "desc": "JSON request body",
            }
        )
        bindings.append(
            _ParamBinding(name=name, location="raw_body", wire_name=name, required=body_required)
        )

    summary = operation.get("summary") or ""
    description = operation.get("description") or ""
    doc_string = " ".join(str(v).strip() for v in (summary, description) if str(v).strip())
    if not doc_string:
        doc_string = f"{method.upper()} {path}"
    tool_doc = ToolDoc(
        name=tool_name,
        description=doc_string,
        parameters=params,
        returns=_response_schema(doc, operation),
    )
    spec = ToolSpec(
        name=tool_name,
        params=[p["name"] for p in params],
        doc=tool_doc,
        signer=signer,
    )
    return spec, _Operation(
        tool_name=tool_name,
        method=method.upper(),
        path=path,
        bindings=bindings,
        request_body_required=body_required,
    )


@dataclasses.dataclass
class _OpenAPIEnv:
    base_url: str
    operations: dict[str, _Operation]
    headers: dict[str, str]


def build_openapi_suite(
    name: str,
    *,
    spec_path: str | Path | None = None,
    spec_url: str | None = None,
    base_url: str | None = None,
    signer: str | None = None,
    headers: dict[str, str] | None = None,
) -> SuiteSpec:
    """Reflect an OpenAPI 3.x document into a ``SuiteSpec``."""
    doc = load_openapi_document(spec_path, spec_url)
    signer = signer or name
    resolved_base = base_url or _base_url_from_doc(doc)
    if not resolved_base:
        raise OpenAPIError(
            f"openapi suite {name!r} requires 'base_url' or a non-empty servers[0].url"
        )
    # servers[0].url comes from the (possibly untrusted) spec; pin it to http(s)
    # so it cannot redirect reflected tool calls to file:// or a metadata IP.
    _require_http_url(resolved_base, f"openapi suite {name!r} base_url")
    tool_specs: dict[str, ToolSpec] = {}
    operations: dict[str, _Operation] = {}
    for path, path_item in doc["paths"].items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            spec, op = _tool_from_operation(
                doc, method.lower(), path, path_item, operation, signer
            )
            if spec.name in tool_specs:
                raise OpenAPIError(f"duplicate OpenAPI tool name {spec.name!r}")
            tool_specs[spec.name] = spec
            operations[spec.name] = op
    if not tool_specs:
        raise OpenAPIError(f"openapi suite {name!r} exposed no supported operations")

    def make_env() -> _OpenAPIEnv:
        return _OpenAPIEnv(
            base_url=resolved_base.rstrip("/"),
            operations=operations,
            headers=dict(headers or {}),
        )

    def runner_factory(env: _OpenAPIEnv) -> Callable[[str, dict[str, Any]], Any]:
        def run(tool: str, kwargs: dict[str, Any]) -> Any:
            op = env.operations.get(tool)
            if op is None:
                raise ValueError(f"unknown OpenAPI tool {tool!r} on suite {name!r}")
            return _execute_operation(env, op, kwargs)

        return run

    return SuiteSpec(
        name=name,
        tools=tool_specs,
        make_env=make_env,
        runner_factory=runner_factory,
        tasks=[],
    )


def _base_url_from_doc(doc: dict[str, Any]) -> str | None:
    servers = doc.get("servers") or []
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and isinstance(first.get("url"), str):
            return first["url"]
    return None


def _execute_operation(env: _OpenAPIEnv, op: _Operation, kwargs: dict[str, Any]) -> Any:
    path = op.path
    query: dict[str, Any] = {}
    headers = dict(env.headers)
    body_obj: dict[str, Any] = {}
    raw_body: Any = None

    for binding in op.bindings:
        value = kwargs.get(binding.name)
        if value is None and not binding.required:
            continue
        if value is None and binding.required:
            raise ValueError(f"missing required parameter {binding.name!r}")
        if binding.location == "path":
            path = path.replace(
                "{" + binding.wire_name + "}",
                urllib.parse.quote(str(value), safe=""),
            )
        elif binding.location == "query":
            query[binding.wire_name] = value
        elif binding.location == "header":
            headers[binding.wire_name] = str(value)
        elif binding.location == "body":
            body_obj[binding.wire_name] = value
        elif binding.location == "raw_body":
            raw_body = value

    url = env.base_url + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    # Defense in depth: base_url was gated at build time, but a path segment
    # must never be able to switch the scheme of the dereferenced URL.
    _require_http_url(url, f"{op.method} {op.path}")

    data = None
    if raw_body is not None or body_obj or op.request_body_required:
        data = json.dumps(raw_body if raw_body is not None else body_obj).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")

    req = urllib.request.Request(url, data=data, headers=headers, method=op.method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise OpenAPIError(f"{op.method} {op.path} failed: HTTP {exc.code}: {detail}") from exc

    text = raw.decode("utf-8", "replace")
    if "application/json" in content_type.lower():
        return json.loads(text) if text else None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
