"""OpenAPI reflection smoke tests.

These tests keep the API-spec auto-reflection boundary honest without hitting
external network services.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gateway.providers.api_spec_monitor import check_config
from gateway.providers.openapi_suite import (
    OpenAPIError,
    _parameter_entries,
    _tool_from_operation,
    build_openapi_suite,
    load_openapi_document,
)


OPENAPI_DOC = {
    "openapi": "3.0.0",
    "info": {"title": "Billing", "version": "1.0.0"},
    "servers": [{"url": "http://127.0.0.1:0"}],
    "paths": {
        "/customers/{customer_id}/charges": {
            "post": {
                "operationId": "createCharge",
                "summary": "Create a charge",
                "parameters": [
                    {
                        "name": "customer_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "dry_run",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["amount"],
                                "properties": {
                                    "amount": {"type": "number"},
                                    "memo": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "status": {"type": "string"},
                                        "amount": {"type": "number"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value))


def test_openapi_suite_reflects_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "openapi.json"
        _write_json(spec_path, OPENAPI_DOC)

        suite = build_openapi_suite("billing", spec_path=spec_path, base_url="http://127.0.0.1:1")
        assert suite.tool_params()["createcharge"] == [
            "customer_id",
            "dry_run",
            "amount",
            "memo",
        ]
        doc = suite.tools["createcharge"].doc
        assert doc.returns == "object {status: string, amount: number}"


def test_operation_parameter_overrides_path_parameter() -> None:
    path_item = {
        "parameters": [
            {"name": "limit", "in": "query", "description": "path default"}
        ]
    }
    operation = {
        "parameters": [
            {"name": "limit", "in": "query", "description": "operation override"}
        ]
    }
    entries = _parameter_entries({}, path_item, operation)
    assert len(entries) == 1
    assert entries[0]["description"] == "operation override"


def test_required_request_body_does_not_make_every_property_required() -> None:
    path = "/charges"
    operation = OPENAPI_DOC["paths"]["/customers/{customer_id}/charges"]["post"]
    _spec, reflected = _tool_from_operation(
        OPENAPI_DOC,
        "post",
        path,
        {},
        operation,
        "billing",
    )
    required_by_name = {
        binding.name: binding.required for binding in reflected.bindings
    }
    assert required_by_name["amount"] is True
    assert required_by_name["memo"] is False


def test_non_string_openapi_descriptions_are_ignored() -> None:
    operation = {
        "summary": {"not": "text"},
        "description": ["not", "text"],
        "parameters": [
            {
                "name": "limit",
                "in": "query",
                "description": {"not": "text"},
                "schema": {"type": "integer"},
            }
        ],
        "responses": {},
    }
    spec, _reflected = _tool_from_operation(
        {"paths": {}}, "get", "/records", {}, operation, "api"
    )
    assert spec.doc.description == "GET /records"
    assert spec.doc.parameters[0]["desc"] == "(optional; pass None to omit)"


def test_openapi_spec_url_rejects_non_http_scheme() -> None:
    # file:// (and any non-http scheme) must be refused before urlopen runs,
    # so an operator cannot be tricked into reading a local file as a "spec".
    with pytest.raises(OpenAPIError):
        load_openapi_document(url="file:///etc/passwd")


def test_openapi_base_url_from_untrusted_spec_cannot_be_link_local() -> None:
    # An untrusted spec whose servers[0].url points at the cloud-metadata IP
    # must be rejected: otherwise every reflected tool call is an SSRF.
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "openapi.json"
        doc = dict(OPENAPI_DOC)
        doc["servers"] = [{"url": "http://169.254.169.254/latest/"}]
        _write_json(spec_path, doc)
        with pytest.raises(OpenAPIError):
            build_openapi_suite("evil", spec_path=spec_path)


def test_openapi_base_url_from_untrusted_spec_cannot_be_file_scheme() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "openapi.json"
        doc = dict(OPENAPI_DOC)
        doc["servers"] = [{"url": "file:///etc/"}]
        _write_json(spec_path, doc)
        with pytest.raises(OpenAPIError):
            build_openapi_suite("evil", spec_path=spec_path)


def test_openapi_localhost_base_url_still_allowed() -> None:
    # Loopback/internal backends are legitimate for this tool and must NOT be
    # blocked by the SSRF guard (only link-local is refused).
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "openapi.json"
        _write_json(spec_path, OPENAPI_DOC)
        suite = build_openapi_suite("billing", spec_path=spec_path, base_url="http://127.0.0.1:1")
        assert "createcharge" in suite.tools


def test_api_spec_monitor_detects_and_updates_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec_path = root / "openapi.json"
        config_path = root / "gateway.json"
        state_path = root / "state.json"
        _write_json(spec_path, OPENAPI_DOC)
        _write_json(
            config_path,
            {
                "suites": [
                    {
                        "name": "billing",
                        "kind": "openapi",
                        "spec_path": str(spec_path),
                        "base_url": "http://127.0.0.1:1",
                    }
                ]
            },
        )

        first = check_config(config_path, state_path, update=True)
        assert first["changed"] is True
        second = check_config(config_path, state_path, update=False)
        assert second["changed"] is False

        changed = dict(OPENAPI_DOC)
        changed["paths"] = dict(OPENAPI_DOC["paths"])
        changed["paths"]["/refunds"] = {
            "post": {
                "operationId": "createRefund",
                "responses": {"200": {"description": "OK"}},
            }
        }
        _write_json(spec_path, changed)
        third = check_config(config_path, state_path, update=False)
        assert third["changed"] is True
        assert third["openapi_suites"][0]["tool_diff"]["added"] == ["createrefund"]


def main() -> int:
    test_openapi_suite_reflects_tools()
    test_api_spec_monitor_detects_and_updates_snapshot()
    print("RESULT: OpenAPI suite reflection tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
