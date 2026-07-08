"""Side-channel policy and protection-level reporting (#4 / B5).

Two honest-reporting duties the design demands (DESIGN_STATUS):

1. **Side channels.** The gateway can only enforce actions it observes on a
   controlled route. In localhost mode it CANNOT prevent an agent from reaching
   a SaaS through a side channel (a Bash ``curl``, a subprocess, direct
   network). It must (a) DENY the side-channel tools it can see, and (b) report
   honestly that out-of-band execution remains possible without an isolated
   runtime. This encodes the Stage 1 "no raw side channels" precondition (raw Bash assumed absent)
   as an enforced default, not just a documented assumption.

2. **Protection level L0-L3.** The strength of the guarantee depends on what
   the integration actually feeds the gateway (clean prompt? tool routing?
   gateway-executed tools?) and whether the agent runtime is isolated. The
   gateway must report its effective level rather than claim protection it does
   not have -- a smooth setup that silently degrades to L0 is worse than an
   explicit one.
"""

from __future__ import annotations

import dataclasses
import enum
import functools


class ProtectionLevel(enum.IntEnum):
    L0 = 0  # network destination only -- no PAuth guarantee
    L1 = 1  # tool calls only -- can deny unknown tools, cannot infer task intent
    L2 = 2  # clean prompt + tool calls -- PAuth plan enforcement is meaningful
    L3 = 3  # + the gateway executes the tools itself -- strongest


# Common names for tools that give the agent raw, un-modelled outbound I/O.
# Matched case-insensitively. A deployment can extend or allowlist.
_DEFAULT_SIDE_CHANNELS = frozenset({
    "bash", "shell", "sh", "zsh", "fish", "cmd", "powershell", "pwsh",
    "exec", "execute", "subprocess", "spawn", "system", "eval",
    "run_command", "runcommand", "run_shell", "command", "terminal", "process",
})


@dataclasses.dataclass(frozen=True)
class SideChannelPolicy:
    """Which tools are raw side channels the gateway cannot reason about.

    Side-channel tools are DENIED by default (Stage 1 禁止前提). ``allowlist``
    exempts specific tool names a deployment has decided are safe.
    """

    denied: frozenset[str] = _DEFAULT_SIDE_CHANNELS
    allowlist: frozenset[str] = frozenset()

    @functools.cached_property
    def _denied_lower(self) -> frozenset[str]:
        return frozenset(d.lower() for d in self.denied)

    @functools.cached_property
    def _allowlist_lower(self) -> frozenset[str]:
        return frozenset(a.lower() for a in self.allowlist)

    def is_denied(self, tool: str) -> bool:
        t = (tool or "").lower()
        if t in self._allowlist_lower:
            return False
        if t in self._denied_lower:
            return True
        # A merged suite (D2) renames a tool to ``<source>__<tool>``; match the
        # trailing segment so a namespaced ``billing__bash`` cannot slip a raw
        # side channel past the exact-name gate. Over-denial here is recoverable
        # via ``allowlist``; an under-denied side channel is a silent bypass.
        return "__" in t and t.rsplit("__", 1)[-1] in self._denied_lower


@dataclasses.dataclass(frozen=True)
class ProtectionInputs:
    """What the current integration actually provides."""

    captures_clean_prompt: bool = True
    routes_tool_calls: bool = True
    gateway_executes_tools: bool = True
    side_channels_denied: bool = True
    isolated_runtime: bool = False


@dataclasses.dataclass(frozen=True)
class ProtectionReport:
    level: ProtectionLevel
    caveats: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"level": self.level.name, "caveats": list(self.caveats)}


def assess(inputs: ProtectionInputs) -> ProtectionReport:
    """Compute the effective protection level and its honest caveats."""
    if inputs.routes_tool_calls and inputs.captures_clean_prompt:
        level = ProtectionLevel.L3 if inputs.gateway_executes_tools else ProtectionLevel.L2
    elif inputs.routes_tool_calls:
        level = ProtectionLevel.L1
    else:
        level = ProtectionLevel.L0

    caveats: list[str] = []
    if not inputs.captures_clean_prompt:
        caveats.append(
            "clean prompt is not captured before model/tool contamination; "
            "task intent cannot be enforced (degraded from L2/L3)"
        )
    if not inputs.isolated_runtime:
        if inputs.side_channels_denied:
            caveats.append(
                "known side-channel tools are denied at the gateway, but "
                "out-of-band execution (subprocess, direct network) is NOT "
                "preventable without an isolated agent runtime"
            )
        else:
            caveats.append(
                "side channels (Bash, direct network) can bypass the gateway "
                "entirely; not preventable in localhost mode"
            )
    if level >= ProtectionLevel.L2 and not inputs.gateway_executes_tools:
        caveats.append(
            "gateway authorizes but does not execute tools; a TOCTOU gap "
            "exists between authorization and the agent's execution"
        )
    return ProtectionReport(level=level, caveats=tuple(caveats))
