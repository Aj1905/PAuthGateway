"""Confirmation-gated sinks: the primary dangerous-flow closure.

When a side-effecting call's CONTROL operand (recipient / amount) carries a
value that DERIVES from an UNTRUSTED source (an email body, a forum reply), the
gateway does not silently authorize it. It holds the call pending human
confirmation: the actual value is surfaced to the user on a side channel (never
back into the agent's model context -- ``feedback.py`` / S16), and the call
proceeds only after the user approves.

Untrusted data flowing to a CONTENT operand (a message body) is NOT gated -- the
content/control rule (S15): poisoned content reaches an already-approved
destination, which is bounded. Only untrusted data reaching a CONTROL operand is
gated.

Taint is STATIC and provenance-based (S20). Because the restricted grammar has
single assignment, no loops, and explicit tool-result-to-variable dataflow, we
can compute -- from the code plus per-tool trust labels -- which control
operands derive from an untrusted source, regardless of any transformation on
the way (``amount = msg.amount * 2`` is still tainted). This closes the
laundering hole of value-matching taint: we track the operand's dependency, not
its value.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Iterator

from pauth.codegen import ToolDoc

from gateway.planning.prechecks import PrecheckPolicy, _classify_param
from gateway.runtime.sanitize import describe_hidden, type_violation


@dataclasses.dataclass(frozen=True)
class SourceTrust:
    """Per-tool provenance labels: which tools return untrusted data.

    ``default_untrusted`` controls the fail mode for tools that are in neither
    set. Fail-CLOSED (``default_untrusted=True``, via :meth:`fail_closed`) is
    the safe production mode: a newly added source that nobody labelled is
    treated as untrusted, so forgetting to label it over-gates (recoverable
    over-rejection) rather than silently disabling protection. The default here
    is fail-OPEN only for backward compatibility of the off-by-default gate.
    """

    untrusted_tools: frozenset[str] = frozenset()
    trusted_tools: frozenset[str] = frozenset()
    default_untrusted: bool = False
    # "Trust the human" policy: gate a side-effecting call when ANY operand
    # (not only recipient/amount) derives from an untrusted source. This covers
    # DECISION operands -- e.g. book_table(best.id) where best was selected from
    # untrusted web-extracted data -- so a human confirms actions taken on data
    # whose truth the gateway cannot verify. Off by default (narrow S15 gate).
    confirm_untrusted_decisions: bool = False

    def is_untrusted(self, tool: str) -> bool:
        if tool in self.trusted_tools:
            return False
        if tool in self.untrusted_tools:
            return True
        return self.default_untrusted

    @classmethod
    def fail_closed(cls, trusted_tools: frozenset[str] | set[str] = frozenset()) -> "SourceTrust":
        """Fail-closed mode: everything is untrusted except the declared trusted
        (own-data) reads. Recommended for real deployments."""
        return cls(trusted_tools=frozenset(trusted_tools), default_untrusted=True)


@dataclasses.dataclass
class PendingConfirmation:
    """One held call awaiting human approval.

    ``value`` is the actual (possibly poisoned) operand value, for the HUMAN
    side channel only. It must never be routed back into the agent's model
    context (agent feedback stays value-free, S16). ``source`` names the
    untrusted tool(s) the value derives from -- the provenance a human needs
    ("this recipient came from read_email").
    """

    confirmation_id: str
    tool: str
    param_index: int
    param_name: str
    value: object
    source: tuple[str, ...] = ()
    param_type: str = ""   # the operand's declared schema type (constrained extraction)

    def human_warning(self) -> str:
        """Caution to show the human alongside the value. Two parts, in order of how
        much the gateway is really adding: (1) HIDDEN characters in the value that
        the human cannot see -- the gateway's job, since the human physically cannot
        catch these; (2) untrusted provenance -- the human should verify. Visible
        content the human could judge for themselves is deliberately not second-
        guessed here (that is the human's call, not the gateway's)."""
        hidden = describe_hidden(self.value) if isinstance(self.value, str) else ""
        bad_type = type_violation(self.value, self.param_type)
        if not self.source and not hidden and not bad_type:
            return ""
        parts = []
        if hidden:
            parts.append(
                f"this value contains {hidden} you CANNOT SEE -- almost certainly a "
                "hidden prompt injection"
            )
        if bad_type:
            parts.append(
                f"this {self.param_name} is {bad_type} -- free text smuggled through a "
                "numeric field (constrained extraction violated)"
            )
        if self.source:
            src = ", ".join(self.source)
            parts.append(
                f"this {self.param_name} came from untrusted data ({src}) and may "
                "have been altered"
            )
        return ("WARNING: " + "; ".join(parts) + ". The gateway cannot verify the "
                "value is genuine -- check it against a trusted source before approving.")


def control_operands(
    tool: str,
    docs_by_name: dict[str, ToolDoc],
    policy: PrecheckPolicy | None,
) -> Iterator[tuple[int, str]]:
    """Yield ``(param_index, param_name)`` for control (recipient/amount) params."""
    doc = docs_by_name.get(tool)
    if doc is None:
        return
    for i, p in enumerate(doc.parameters):
        roles = _classify_param(tool, p["name"], p.get("desc", ""), policy or PrecheckPolicy())
        if "recipient" in roles or "amount" in roles:
            yield i, p["name"]


# ---------------------------------------------------------------------------
# Static provenance taint (S20): which control operands derive from an
# untrusted source, computed from the restricted-grammar code + trust labels.
# ---------------------------------------------------------------------------

def _run_function(code: str) -> ast.FunctionDef | None:
    try:
        module = ast.parse(code)
    except SyntaxError:
        return None
    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    return funcs[0] if funcs else None


def _ordered_statements(func: ast.FunctionDef) -> Iterator[ast.stmt]:
    """Statements in def-before-use order, descending into the single if-body."""
    for stmt in func.body:
        yield stmt
        if isinstance(stmt, ast.If):
            for inner in stmt.body:
                yield inner


def _expr_sources(node: ast.expr, var_sources: dict[str, set], tool_names: set[str]) -> set:
    """Set of source tool names a value expression depends on (over-approximate)."""
    if isinstance(node, ast.Constant):
        return set()
    if isinstance(node, ast.Name):
        return set(var_sources.get(node.id, set()))
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _expr_sources(node.value, var_sources, tool_names)
    if isinstance(node, ast.Call):
        s: set = set()
        if isinstance(node.func, ast.Name) and node.func.id in tool_names:
            s.add(node.func.id)
        for a in node.args:
            s |= _expr_sources(a, var_sources, tool_names)
        for kw in node.keywords:
            s |= _expr_sources(kw.value, var_sources, tool_names)
        return s
    # Arithmetic / boolean / comparison / anything else: union of children, so a
    # transformed tainted value (amount * 2, min(...), etc.) stays tainted.
    s = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            s |= _expr_sources(child, var_sources, tool_names)
    return s


def _tool_calls(node: ast.AST, tool_names: set[str]) -> Iterator[ast.Call]:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in tool_names:
            yield n


def static_taint_map(
    code: str,
    docs_by_name: dict[str, ToolDoc],
    source_trust: SourceTrust,
    policy: PrecheckPolicy | None = None,
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Map each untrusted-derived control operand to its untrusted source tools.

    ``{(tool, param_index): (source_tool, ...)}``. Provenance-based, so a
    transformation cannot launder taint. The source list is the provenance a
    human confirmation dialog can display.
    """
    func = _run_function(code)
    if func is None:
        return {}
    tool_names = set(docs_by_name)
    var_sources: dict[str, set] = {}
    gated: dict[tuple[str, int], tuple[str, ...]] = {}

    for stmt in _ordered_statements(func):
        # Record variable provenance (single-assignment => defined before use).
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            var_sources[stmt.targets[0].id] = _expr_sources(
                stmt.value, var_sources, tool_names
            )
        # Check every tool call's control operands against current provenance.
        for call in _tool_calls(stmt, tool_names):
            tool = call.func.id  # type: ignore[union-attr]
            for i, _name in control_operands(tool, docs_by_name, policy):
                if i >= len(call.args):
                    continue
                src = _expr_sources(call.args[i], var_sources, tool_names)
                untrusted = tuple(sorted(t for t in src if source_trust.is_untrusted(t)))
                if untrusted:
                    gated[(tool, i)] = untrusted
    return gated


def static_taint(
    code: str,
    docs_by_name: dict[str, ToolDoc],
    source_trust: SourceTrust,
    policy: PrecheckPolicy | None = None,
) -> set[tuple[str, int]]:
    """Return ``{(tool, param_index)}`` for untrusted-derived control operands."""
    return set(static_taint_map(code, docs_by_name, source_trust, policy))


# ---------------------------------------------------------------------------
# Broad taint ("trust the human", S15+): gate EVERY untrusted-derived operand
# of a side-effecting call, not just recipient/amount. This is what makes a
# human the truth oracle for decisions taken on structured-but-untrusted data
# (e.g. picking the "best" item from an extracted web page).
# ---------------------------------------------------------------------------

# A tool is treated as a pure read (never gated) only if its name marks it one;
# everything else is assumed side-effecting, so a mislabel over-gates (safe)
# rather than under-gates. Mirrors eval.schema_scope's getter prefixes.
_READ_PREFIXES = (
    "get_", "list_", "read_", "search_", "find_", "retrieve_", "show_",
    "lookup_", "fetch_", "check_", "view_",
)


def is_side_effecting(tool: str) -> bool:
    """True unless the tool's name marks it a pure read (fail-safe: unknown -> gated)."""
    return not tool.startswith(_READ_PREFIXES)


def broad_taint_map(
    code: str,
    docs_by_name: dict[str, ToolDoc],
    source_trust: SourceTrust,
    policy: PrecheckPolicy | None = None,
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Like :func:`static_taint_map`, but gate ANY untrusted-derived operand of a
    side-effecting call (decision operands included), reusing the same provenance
    tracking so a transformation cannot launder taint."""
    func = _run_function(code)
    if func is None:
        return {}
    tool_names = set(docs_by_name)
    var_sources: dict[str, set] = {}
    gated: dict[tuple[str, int], tuple[str, ...]] = {}

    for stmt in _ordered_statements(func):
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            var_sources[stmt.targets[0].id] = _expr_sources(
                stmt.value, var_sources, tool_names
            )
        for call in _tool_calls(stmt, tool_names):
            tool = call.func.id  # type: ignore[union-attr]
            if not is_side_effecting(tool):
                continue
            for i, arg in enumerate(call.args):
                src = _expr_sources(arg, var_sources, tool_names)
                untrusted = tuple(sorted(t for t in src if source_trust.is_untrusted(t)))
                if untrusted:
                    gated[(tool, i)] = untrusted
    return gated


def taint_map(
    code: str,
    docs_by_name: dict[str, ToolDoc],
    source_trust: SourceTrust,
    policy: PrecheckPolicy | None = None,
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Dispatch to the broad ("trust the human") or narrow (recipient/amount) gate
    per ``source_trust.confirm_untrusted_decisions``."""
    if source_trust.confirm_untrusted_decisions:
        return broad_taint_map(code, docs_by_name, source_trust, policy)
    return static_taint_map(code, docs_by_name, source_trust, policy)
