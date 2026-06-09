"""OpenAPI reflection smoke tests.

These tests keep the API-spec auto-reflection boundary honest without hitting
external network services.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from gateway.api_spec_monitor import check_config
from gateway.openapi_suite import build_openapi_suite


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
