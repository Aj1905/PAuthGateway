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
from pauth.envelope import flatten
from pauth.evaluator import Evaluator, wrap
from pauth.symbolic import canon

from gateway.planning.prechecks import PrecheckPolicy, _classify_param
from gateway.runtime.sanitize import describe_hidden, type_violation

# Reduction helpers whose result is a computed scalar a human cannot verify by
# eye -- for these, the confirmation surfaces the inputs (the summands / candidates).
_REDUCERS = frozenset({"sum", "min", "max", "len"})
_LABEL_FIELDS = ("name", "title", "label", "subject", "description", "id")
_LINK_FIELDS = ("url", "link", "website", "href", "page")


def _fmt_element(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


@dataclasses.dataclass
class BreakdownRow:
    """One row of a breakdown table: WHAT the value is (label), its number, an
    optional link, and whether it is the element the reducer selected."""

    label: str
    value: object
    link: str = ""
    selected: bool = False


def _label_and_link(element) -> tuple[str, str]:
    """Pull a human label and an optional link from a structured element, preferring
    the most human-friendly field (name > title > ... > id), not field order."""
    try:
        fields = flatten(wrap(element))
    except Exception:  # noqa: BLE001
        return "", ""
    by_key: dict[str, str] = {}
    for path, val in fields.items():
        if isinstance(val, str):
            by_key.setdefault(path.split(".")[-1], val)
    label = next((by_key[k] for k in _LABEL_FIELDS if k in by_key), "")
    link = next((by_key[k] for k in _LINK_FIELDS if k in by_key), "")
    return label, link


def _base_name(expr):
    """Strip trailing attribute/subscript access to the underlying variable, so
    ``best.id`` (a decision operand) resolves to the reducer that produced best."""
    while isinstance(expr, (ast.Attribute, ast.Subscript)):
        expr = expr.value
    return expr


def reduction_breakdown(rule: object, param_index: int, store: object):
    """If a gated operand derives (through the rule's lets) from a reduction over a
    collection, return ``(op_name, (BreakdownRow, ...))`` -- a LABELLED table of the
    inputs the value was computed/selected from, resolved from the SAME signed
    envelopes the enforcer used. Else ``None``.

    For ``sum`` every row contributes (all selected): the human reads item->amount.
    For ``min``/``max`` one row is ``selected``: the human sees the candidates and
    why this one won (and can flag an inflated-looking one). Handles a decision
    operand like ``book(best.id)`` where ``best = max(options, key=...)``.
    """
    try:
        expr = rule.arg_exprs[param_index]  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return None
    lets = getattr(rule, "lets", {}) or {}
    expr = _base_name(expr)
    seen: set[str] = set()
    while isinstance(expr, ast.Name) and expr.id in lets and expr.id not in seen:
        seen.add(expr.id)
        expr = _base_name(lets[expr.id])
    if not (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id in _REDUCERS
        and expr.args
    ):
        return None
    op = expr.func.id
    ev = Evaluator(store, lets)
    try:
        elements = list(ev.eval(expr.args[0]))
    except Exception:  # noqa: BLE001 -- a display aid must never break the gate
        return None
    kw = {k.arg: k.value for k in expr.keywords}
    lam = kw.get("key")
    keyfn = None
    if isinstance(lam, ast.Lambda):
        try:
            keyfn = ev._make_lambda(lam)  # same projection the enforcer applies
        except Exception:  # noqa: BLE001
            keyfn = None

    rows: list[BreakdownRow] = []
    values = []
    for el in elements:
        val = keyfn(el) if keyfn is not None else el
        values.append(val)
        label, link = _label_and_link(el)
        rows.append(BreakdownRow(label or _fmt_element(val), val, link))
    # mark the selected element for a selection reducer
    if op in ("min", "max") and values:
        pick = (min if op == "min" else max)(range(len(values)), key=lambda i: values[i])
        rows[pick].selected = True
    elif op == "sum":
        for r in rows:
            r.selected = True
    return (op, tuple(rows))


def _fmt_result(v) -> str:
    s = str(v).strip()
    return s if len(s) <= 240 else s[:237] + "..."


def provenance_reference(rule: object, param_index: int, store: object):
    """For a BARE (non-reduction) gated operand, surface WHERE it came from: the
    source tool call(s) it depends on and what each returned, resolved from the
    signed envelope store. Returns ``[(source_call_str, result), ...]`` or None.

    This is the 参照すべき情報 for a value with no breakdown table (e.g. a recipient
    read from get_iban, an email read from get_webpage): it gives the human the
    source to research -- itself UNTRUSTED, so verify via an independent channel.
    """
    try:
        expr = rule.arg_exprs[param_index]  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return None
    lets = getattr(rule, "lets", {}) or {}
    calls: dict[str, ast.Call] = {}
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, ast.Call):
            key = canon(node)
            if store.has(key):  # a tool call with a recorded result
                calls[key] = node
        if isinstance(node, ast.Name) and node.id in lets and node.id not in seen:
            seen.add(node.id)
            walk(lets[node.id])
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(expr)
    if not calls:
        return None
    ev = Evaluator(store, lets)
    out = []
    for key, node in calls.items():
        try:
            out.append((key, _fmt_result(ev.eval(node))))
        except Exception:  # noqa: BLE001 -- display aid must never break the gate
            out.append((key, "(unresolved)"))
    return out or None


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
    # Tools whose output the enforcer CANNOT re-derive (an LLM extractor that read
    # a file/page and produced a value by understanding, not by a pure function).
    # Such a value is untrusted AND unverifiable: unlike a deterministic parse, a
    # hidden instruction in the source could have steered it, and the gateway
    # cannot recompute it. It must be gated with a stronger warning and never
    # presented as proven. These are also untrusted (taint) by implication.
    unverifiable_tools: frozenset[str] = frozenset()
    # "Trust the human" policy: gate a side-effecting call when ANY operand
    # (not only recipient/amount) derives from an untrusted source. This covers
    # DECISION operands -- e.g. book_table(best.id) where best was selected from
    # untrusted web-extracted data -- so a human confirms actions taken on data
    # whose truth the gateway cannot verify. Off by default (narrow S15 gate).
    confirm_untrusted_decisions: bool = False
    # Amplification cap: a bounded-for loop (a plan-authorised bulk operation) that
    # exceeds this many iterations is held ONCE for human confirmation -- the loop
    # count is data-dependent, so an injected/oversized collection could blow it up
    # even though every call is FN=0-valid. Off-plan bulk is default-denied, not
    # gated, so the gate fires only when the excess is genuinely task-driven (in the
    # plan). None disables the cap.
    bulk_max_iterations: int | None = None

    def is_untrusted(self, tool: str) -> bool:
        if tool in self.unverifiable_tools:
            return True  # unverifiable implies untrusted
        if tool in self.trusted_tools:
            return False
        if tool in self.untrusted_tools:
            return True
        return self.default_untrusted

    def is_unverifiable(self, tool: str) -> bool:
        """True if this tool's output cannot be re-derived (an LLM extractor)."""
        return tool in self.unverifiable_tools

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
    bulk_rule: str | None = None   # set for an amplification (loop-cap) confirmation
    # For a COMPUTED operand (a reduction over an untrusted collection): the
    # ``(op, [elements])`` the value was reduced from, so the human can inspect the
    # summands instead of being asked to verify a total they cannot see. Populated
    # from the SAME signed envelopes the enforcer re-derived the value from.
    breakdown: tuple[str, tuple] | None = None
    # For a BARE (non-reduction) operand: [(source_call, result), ...] -- where the
    # value was read from, so the human has a source to research. Untrusted.
    provenance: tuple | None = None
    # True if the value came from a NON-re-derivable extraction (an LLM read the
    # source and produced it by understanding). Unlike a deterministic parse, the
    # gateway cannot recompute it and a hidden instruction could have steered the
    # extractor -- so it is gated with a stronger warning and never called proven.
    unverifiable: bool = False
    # Natural-language phrasing for a human-readable gate (not raw tool names):
    #   task_desc   -- what the action IS ("送金するタスク")
    #   source_desc -- where the value came from, in human terms ("Gmailのこのメール")
    # When empty, fall back to the tool name / raw provenance call.
    task_desc: str = ""
    source_desc: str = ""

    def structured_display(self, ground_truth: str = "") -> str:
        """The confirmation as a fixed 6-field template:

          何をするタスク / どの情報が必要 / どこから取得した / 取得情報一覧 /
          参照情報 / (ベンチマーク時のみ) ground truth

        Everything except the last line is what a PRODUCTION gate shows; the
        ground-truth line is added only when a benchmark passes it in."""
        lines = [f"【何をするタスク】{self.task_desc or self.tool}",
                 f"【どの情報が必要】{self.param_name} = {self.value!r}"]

        if self.source_desc:
            src = self.source_desc
        elif self.provenance:
            src = "; ".join(call for call, _ in self.provenance)
        elif self.breakdown:
            src = f"{self.breakdown[0]}（集約）" + (
                "・" + ", ".join(self.source) if self.source else "")
        else:
            src = ", ".join(self.source) if self.source else "（不明）"
        lines.append(f"【どこから取得した】{src}")

        lines.append("【取得情報一覧】")
        if self.breakdown:
            lines += ["  " + r for r in _breakdown_rows(self.breakdown)]
        elif self.provenance:
            for _call, result in self.provenance:
                lines.append(f"  {result}")
        else:
            lines.append(f"  {self.value!r}")

        ref = ["未信頼ソース由来。正しいとは限らない—独立したソースで検証せよ。"]
        if _has_link(self.breakdown):
            ref.append("リンクも改ざんされうる。クリックでなく別チャネルで確認。")
        if self.unverifiable:
            ref.append("LLM抽出（再導出不能）—証明されていない。")
        hidden = describe_hidden(self.value) if isinstance(self.value, str) else ""
        if hidden:
            ref.append(f"見えない文字 {hidden} あり—ほぼ確実に注入。")
        lines.append("【参照情報】" + " ".join(ref))

        if ground_truth:
            lines.append(f"【ground truth（ベンチマークのみ）】{ground_truth}")
        return "\n".join(lines)

    def human_warning(self) -> str:
        """Caution to show the human alongside the value. Ordered by how much the
        gateway is really adding: (1) HIDDEN characters in the value that the human
        cannot see -- the gateway's job, since the human physically cannot catch
        these; (2) a computed value's DECOMPOSITION -- a total is unverifiable by
        eye, so the gateway surfaces the summands (asking a human to check a sum
        they cannot see is asking the impossible); (3) untrusted provenance -- the
        human should verify. Visible content the human could judge for themselves
        is deliberately not second-guessed here (that is the human's call)."""
        hidden = describe_hidden(self.value) if isinstance(self.value, str) else ""
        bad_type = type_violation(self.value, self.param_type)
        if (not self.source and not hidden and not bad_type and not self.breakdown
                and not self.provenance and not self.unverifiable):
            return ""
        parts = []
        if self.unverifiable:
            parts.append(
                f"this {self.param_name} was read by an LLM extractor the gateway "
                "CANNOT re-derive -- an instruction hidden in the source could have "
                "steered it; this value is NOT proven, verify it directly against "
                "the source"
            )
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
        if self.breakdown:
            parts.append(_render_breakdown(self.param_name, self.breakdown))
        if self.provenance and not self.breakdown:
            lines = [f"this {self.param_name} -- 参照すべき情報 (source, from the UNTRUSTED "
                     "source; may be false, verify via an INDEPENDENT channel):"]
            for call, result in self.provenance:
                lines.append(f"  read from: {call}")
                lines.append(f"  that returned: {result}")
            parts.append("\n".join(lines))
        if self.source:
            src = ", ".join(self.source)
            parts.append(
                f"this {self.param_name} came from untrusted data ({src}) and may "
                "have been altered"
            )
        return ("WARNING: " + "; ".join(parts) + ". The gateway cannot verify the "
                "value is genuine -- check it against a trusted source before approving.")


def _breakdown_rows(breakdown) -> list[str]:
    """Just the table rows (mark, label, value, link, selected) -- no framing."""
    op, raw = breakdown
    rows = [r if isinstance(r, BreakdownRow) else BreakdownRow(_fmt_element(r), r) for r in raw]
    wlabel = max((len(str(r.label)) for r in rows), default=0)
    out = []
    for r in rows:
        mark = " > " if (r.selected and op in ("min", "max")) else "   "
        line = f"{mark}{str(r.label):<{wlabel}}  {_fmt_element(r.value):>10}"
        if r.link:
            line += f"  {r.link}"
        if r.selected and op in ("min", "max"):
            line += "  <- selected"
        out.append(line)
    return out


def _has_link(breakdown) -> bool:
    return breakdown is not None and any(
        isinstance(r, BreakdownRow) and r.link for r in breakdown[1]
    )


def _render_breakdown(param_name: str, breakdown) -> str:
    """Render a breakdown as the 参照すべき情報 (reference) block: the data table and
    links the value came from, for the human to VERIFY THEMSELVES.

    This material is from the UNTRUSTED source -- it may be correct or fabricated
    (a fake rating, a poisoned link). The gateway does not vouch for it; it only
    surfaces it so the human has a starting point to research (open a link, check a
    row against what they actually know) before approving. Correctness of the
    reference is the human's to establish, not the gateway's to assert.
    """
    op, raw = breakdown
    rows = [r if isinstance(r, BreakdownRow) else BreakdownRow(_fmt_element(r), r)
            for r in raw]  # tolerate bare values
    verb = {"sum": "is the sum of", "max": "was chosen as MAX among",
            "min": "was chosen as MIN among", "len": "counts"}.get(op, f"is {op} of")
    lines = [f"this {param_name} {verb} -- 参照すべき情報 (reference, from the UNTRUSTED "
             "source; may be false, verify it yourself):"]
    wlabel = max((len(str(r.label)) for r in rows), default=0)
    for r in rows:
        mark = " > " if (r.selected and op in ("min", "max")) else "   "
        row = f"{mark}{str(r.label):<{wlabel}}  {_fmt_element(r.value):>10}"
        if r.link:
            row += f"  {r.link}"
        if r.selected and op in ("min", "max"):
            row += "  <- selected"
        lines.append(row)
    tail = ("check the selected row -- an inflated candidate can win"
            if op in ("min", "max") else
            "check EACH row -- an injected/inflated line hides in the total")
    lines.append(f"  -- the gateway does NOT vouch for any of this; {tail}")
    if any(r.link for r in rows):
        lines.append("  -- the links are ALSO from the untrusted source and may be "
                     "spoofed; verify via an INDEPENDENT source, not by clicking these")
    return "\n".join(lines)


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
# rather than under-gates. Mirrors benchmarks.schema_scope's getter prefixes.
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
