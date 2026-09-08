"""PreToolUse policy for an agent's own tools (Claude Code, Codex, ...).

The gateway plans over the tool surface it *owns* (the MCP servers behind the
facade). An agent also has tools of its own; this module decides what the
``PreToolUse`` hook does with each of them before anything reaches the gateway.

Decisions
---------
* ``allow``   -- local, non-egress work (read/search/edit files in the
                 workspace). Not PAuth's concern; logged, never planned.
* ``allow``   -- the facade's own tools (``mcp__<facade>__*``): the facade
                 already enforces and executes them, so the hook must not send
                 them to the gateway a second time.
* ``deny``    -- raw egress the gateway cannot see (Bash, web fetch/search, MCP
                 servers registered outside the facade). ``strict`` blocks;
                 ``log`` allows and records. Bash can be allowed explicitly by
                 the deployment that has verified OS egress lockdown.
* ``forward`` -- anything else is sent to the gateway as a tool call (the
                 legacy hook-only deployments, e.g. the shopping demo).

Environment
-----------
``GATEWAY_FACADE_NAME``   the MCP server name the facade is registered under
                          (default ``pauth``).
``GATEWAY_LOCAL_TOOLS``   comma-separated extra tool names to treat as local.
``GATEWAY_BASH_POLICY``   ``deny`` (default) or ``allow``. Allow only when the
                          agent runs as the egress-locked user
                          (``gateway/deploy/egress_lockdown.sh``); otherwise
                          Bash is an unobserved side channel.
``GATEWAY_EGRESS_TOOLS``  comma-separated extra tool names to treat as egress.
"""

from __future__ import annotations

import dataclasses
import os
import sys

LOCAL_TOOLS = frozenset({
    # Claude Code
    "Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit",
    "NotebookRead", "TodoWrite", "TodoRead", "Task", "AskUserQuestion", "Skill",
    "ToolSearch", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "LSP", "Monitor", "TaskOutput", "TaskStop", "ListAgents", "SendMessage",
    "ReportFindings", "ScheduleWakeup", "SendFeedback", "Artifact",
    # Codex
    "apply_patch", "read_file", "list_dir", "grep_files",
})

EGRESS_TOOLS = frozenset({
    "Bash", "shell", "exec_command", "WebFetch", "WebSearch", "RemoteTrigger",
    "PushNotification", "SendUserFile", "Workflow",
})


@dataclasses.dataclass(frozen=True)
class Decision:
    action: str   # allow | deny | forward
    reason: str


def _csv(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def decide(tool_name: str, env: dict[str, str] | None = None) -> Decision:
    env = dict(os.environ if env is None else env)
    facade = env.get("GATEWAY_FACADE_NAME", "pauth")
    local = LOCAL_TOOLS | frozenset(
        t.strip() for t in env.get("GATEWAY_LOCAL_TOOLS", "").split(",") if t.strip()
    )
    egress = EGRESS_TOOLS | frozenset(
        t.strip() for t in env.get("GATEWAY_EGRESS_TOOLS", "").split(",") if t.strip()
    )
    bash_policy = env.get("GATEWAY_BASH_POLICY", "deny").strip().lower()

    if tool_name.startswith(f"mcp__{facade}__"):
        return Decision("allow", "facade tool: the gateway enforces and executes it")
    if tool_name.startswith("mcp__"):
        return Decision("deny", "MCP server outside the gateway: unobserved egress")
    if tool_name in ("Bash", "shell", "exec_command"):
        if bash_policy == "allow":
            return Decision("allow", "Bash allowed by GATEWAY_BASH_POLICY=allow (egress lockdown assumed)")
        return Decision("deny", "raw shell is an unobserved side channel (GATEWAY_BASH_POLICY=deny)")
    if tool_name in egress:
        return Decision("deny", "egress tool the gateway cannot see")
    if tool_name in local:
        return Decision("allow", "local workspace tool")
    return Decision("forward", "unknown tool: ask the gateway")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: local_policy.py <tool_name>", file=sys.stderr)
        return 2
    decision = decide(argv[0])
    print(f"{decision.action}\t{decision.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
