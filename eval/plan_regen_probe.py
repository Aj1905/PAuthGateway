"""Probe why the 15 plan-unavailable tasks yielded empty Planner outputs.

The frozen best-of-3 candidate files for 15 AgentDojo tasks are all empty
(0 bytes).  The generation path (``gateway.planning.agentic_planner``) caches
the raw model text verbatim, so an empty file means the model response itself
carried no text block.  This probe re-issues the *first-attempt* generation
request for each of those tasks exactly as the funnel did -- same system
prompt, same user prompt built from the suite tool docs, ``max_tokens=4096``,
no ``thinking`` parameter -- and records the ``stop_reason`` and
``stop_details`` that the original path discarded.

Control tasks that previously produced non-empty plans are probed the same
way to show that the request shape itself still yields code.

The probe never touches the frozen candidate files.

Example::

    .venv/bin/python -m eval.plan_regen_probe \
        --output tests/experiment/results/plan_regen_probe_20260804.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from gateway.planning.agentic_planner import load_me_env
from pauth.codegen import SYSTEM_PROMPT, build_user_prompt

MODEL = "claude-fable-5"
MAX_TOKENS = 4096  # identical to gateway.planning.agentic_planner._call_generator

MISSING_TASKS: tuple[tuple[str, str], ...] = (
    ("banking", "user_task_11"),
    ("banking", "user_task_12"),
    ("slack", "user_task_1"),
    ("slack", "user_task_2"),
    ("slack", "user_task_4"),
    ("slack", "user_task_11"),
    ("slack", "user_task_16"),
    ("slack", "user_task_17"),
    ("slack", "user_task_18"),
    ("slack", "user_task_19"),
    ("slack", "user_task_20"),
    ("workspace", "user_task_13"),
    ("workspace", "user_task_19"),
    ("workspace", "user_task_38"),
    ("workspace", "user_task_39"),
)

# Tasks whose frozen candidates are non-empty; same suites as the failures.
CONTROL_TASKS: tuple[tuple[str, str], ...] = (
    ("banking", "user_task_0"),
    ("slack", "user_task_0"),
    ("workspace", "user_task_0"),
)


def probe_task(client: Any, suite_name: str, task_id: str) -> dict[str, Any]:
    spec = augment_with_structuring(load_suite(suite_name))
    prompt = get_suites("v1")[suite_name].user_tasks[task_id].PROMPT
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": build_user_prompt(prompt, spec.tool_docs())}
        ],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    stop_details = getattr(response, "stop_details", None)
    return {
        "suite": suite_name,
        "user_task_id": task_id,
        "stop_reason": response.stop_reason,
        "stop_details_category": getattr(stop_details, "category", None),
        "stop_details_explanation": getattr(stop_details, "explanation", None),
        "content_block_types": [
            getattr(block, "type", "?") for block in response.content
        ],
        "text_length": len(text),
        "text_head": text[:200],
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "resolved_model": response.model,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="probe only the first N tasks")
    args = parser.parse_args(argv)

    load_me_env()
    import anthropic

    client = anthropic.Anthropic()

    targets = [("missing", s, t) for s, t in MISSING_TASKS] + [
        ("control", s, t) for s, t in CONTROL_TASKS
    ]
    if args.limit is not None:
        targets = targets[: args.limit]

    rows: list[dict[str, Any]] = []
    for index, (group, suite_name, task_id) in enumerate(targets, start=1):
        print(f"[{index}/{len(targets)}] {group} {suite_name}/{task_id}", flush=True)
        try:
            row = probe_task(client, suite_name, task_id)
        except Exception as exc:  # noqa: BLE001 -- record and continue; calls are paid
            row = {
                "suite": suite_name,
                "user_task_id": task_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["group"] = group
        rows.append(row)
        print(
            "  ->",
            row.get("error")
            or f"stop_reason={row['stop_reason']} text_length={row['text_length']}",
            flush=True,
        )

    result = {
        "schema": "plan_regen_probe_v1",
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "thinking_param": None,
        "probed_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "One first-attempt generation request per task, mirroring "
            "gateway.planning.agentic_planner._call_generator. The frozen "
            "candidate files are not modified."
        ),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
