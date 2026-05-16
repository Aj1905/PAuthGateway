"""Framework-neutral suite interface.

Both the self-contained shopping suite and the AgentDojo adapter produce
:class:`SuiteSpec` objects, so the experiment runner is agnostic to where the
tasks come from.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from ..codegen import ToolDoc


@dataclasses.dataclass
class Call:
    """A concrete tool call: an operator and positional operand values."""

    tool: str
    args: list[Any]

    def __str__(self) -> str:
        return f"{self.tool}({', '.join(repr(a) for a in self.args)})"


@dataclasses.dataclass
class ToolSpec:
    """A tool: its ordered parameters, codegen schema and signing server."""

    name: str
    params: list[str]
    doc: ToolDoc
    signer: str


@dataclasses.dataclass
class TaskSpec:
    """One user task plus the forced injections used to probe it."""

    id: str
    prompt: str
    forced_injections: list[Call]
    reference_code: str | None = None  # offline suites ship hand-written A1 output


@dataclasses.dataclass
class SuiteSpec:
    """A complete task suite."""

    name: str
    tools: dict[str, ToolSpec]
    make_env: Callable[[], Any]
    runner_factory: Callable[[Any], Callable[[str, dict[str, Any]], Any]]
    tasks: list[TaskSpec]

    def tool_names(self) -> set[str]:
        return set(self.tools)

    def tool_params(self) -> dict[str, list[str]]:
        return {name: spec.params for name, spec in self.tools.items()}

    def tool_signer(self) -> dict[str, str]:
        return {name: spec.signer for name, spec in self.tools.items()}

    def tool_docs(self) -> list[ToolDoc]:
        return [spec.doc for spec in self.tools.values()]
