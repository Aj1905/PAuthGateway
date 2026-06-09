"""Extract real user prompts that triggered MCP/external-service tool calls.

Scans ``~/.claude/projects/*/<session>.jsonl`` Claude Code transcripts across
all projects. For every top-level ``user`` text message it records the set of
``mcp__*`` tool names invoked by the assistant *before the next user message*,
and emits one JSONL record per (prompt, triggered-tools) pair where the
triggered set is non-empty.

Output: ``tests/fixtures/real_external_prompts.jsonl`` (gitignored).

Re-run after new sessions to refresh. Deterministic, order-preserving, idempotent
(overwrites the output file).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

TRANSCRIPT_ROOT = pathlib.Path.home() / ".claude" / "projects"
OUTPUT = pathlib.Path(__file__).parent / "real_external_prompts.jsonl"

# An "external tool" is an MCP-namespaced tool call. Built-in Read/Write/Bash/
# Edit/Grep/etc. are intentionally excluded -- those are local IO, not the
# SaaS-style integrations PAuth targets.
EXTERNAL_PREFIX = "mcp__"


def _extract_user_text(msg_content) -> str | None:
    """Return the plain user text if this is a genuine user prompt, else None.

    Filters out: tool_result blocks (synthetic), system-reminder-only messages,
    hook-injected content, and anything not containing real natural-language text.
    """
    if isinstance(msg_content, str):
        text = msg_content
    elif isinstance(msg_content, list):
        text_parts = []
        for block in msg_content:
            if not isinstance(block, dict):
                continue
            # tool_result blocks are synthetic responses, never real user input
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        text = "\n".join(text_parts)
    else:
        return None

    stripped = text.strip()
    if not stripped:
        return None
    # Pure system-reminder messages are synthetic; skip if there's no user text
    # outside the reminder tags.
    if stripped.startswith("<") and stripped.endswith(">"):
        # Strip all tagged sections and see if anything remains.
        import re as _re
        residue = _re.sub(r"<[^>]+>.*?</[^>]+>", "", stripped, flags=_re.DOTALL).strip()
        if not residue:
            return None
    return text


def _extract_tool_calls(assistant_content) -> list[str]:
    """Return the names of any ``mcp__*`` tool_use blocks in this assistant turn."""
    if not isinstance(assistant_content, list):
        return []
    names: list[str] = []
    for block in assistant_content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
            if isinstance(name, str) and name.startswith(EXTERNAL_PREFIX):
                names.append(name)
    return names


def _project_label(project_dir_name: str) -> str:
    """Turn ``-Users-aj-Documents-PAuthGateway`` into a readable label."""
    # Strip the leading ``-Users-aj-Documents-`` prefix when present; otherwise
    # just drop the leading dash.
    name = project_dir_name.lstrip("-")
    for prefix in ("Users-aj-Documents-", "Users-aj-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name or project_dir_name


def process_transcript(path: pathlib.Path) -> list[dict]:
    """Walk one JSONL transcript and emit (prompt -> mcp tools) records."""
    out: list[dict] = []
    current_prompt: str | None = None
    current_ts: str | None = None
    current_tools: list[str] = []
    seen_in_window: set[str] = set()

    def flush():
        if current_prompt is not None and current_tools:
            out.append({
                "project": _project_label(path.parent.name),
                "session_id": path.stem,
                "timestamp": current_ts,
                "prompt": current_prompt,
                "triggered_tools": list(current_tools),
                "n_tool_calls": len(current_tools),
            })

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            # Skip sub-agent (sidechain) traffic -- those prompts come from the
            # parent agent, not from the human user.
            if rec.get("isSidechain"):
                continue

            msg = rec.get("message") or {}
            content = msg.get("content")

            if rtype == "user":
                text = _extract_user_text(content)
                if text is None:
                    # Synthetic user record (tool_result, system reminder).
                    # Don't reset the window -- assistant tool calls following
                    # a tool_result still belong to the most recent real prompt.
                    continue
                # New real prompt: flush previous window, start a new one.
                flush()
                current_prompt = text
                current_ts = rec.get("timestamp")
                current_tools = []
                seen_in_window = set()
            elif rtype == "assistant" and current_prompt is not None:
                for name in _extract_tool_calls(content):
                    if name not in seen_in_window:
                        seen_in_window.add(name)
                        current_tools.append(name)

    flush()
    return out


def main() -> int:
    if not TRANSCRIPT_ROOT.exists():
        print(f"transcript root not found: {TRANSCRIPT_ROOT}", file=sys.stderr)
        return 1

    total_records = 0
    total_prompts = 0
    by_project: dict[str, int] = {}
    by_tool: dict[str, int] = {}

    with OUTPUT.open("w", encoding="utf-8") as out_fh:
        for project_dir in sorted(TRANSCRIPT_ROOT.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl in sorted(project_dir.glob("*.jsonl")):
                try:
                    records = process_transcript(jsonl)
                except Exception as exc:  # noqa: BLE001 -- never let one bad file kill the sweep
                    print(f"skip {jsonl}: {exc}", file=sys.stderr)
                    continue
                for rec in records:
                    out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    total_records += 1
                    by_project[rec["project"]] = by_project.get(rec["project"], 0) + 1
                    for tool in rec["triggered_tools"]:
                        by_tool[tool] = by_tool.get(tool, 0) + 1
                total_prompts += sum(1 for _ in records)

    print(f"wrote {total_records} records -> {OUTPUT}")
    print()
    print("top projects:")
    for proj, n in sorted(by_project.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {n:5d}  {proj}")
    print()
    print("top external tools:")
    for tool, n in sorted(by_tool.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {n:5d}  {tool}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
