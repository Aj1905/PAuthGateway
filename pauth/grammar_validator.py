"""GrammarValidator -- parser for the Appendix A profile and extended G2.

The Planner step asks an LLM to generate a ``run`` function in a restrictive subset
of Python.  Before any slice is derived we

1. parse the code and reject anything outside the DSL's *syntax*
   (:func:`parse_and_validate`);
2. remove dead / unreachable statements (:func:`strip_dead_code`);
3. reject anything that violates the DSL's *semantics* -- calls to
   non-tools, method calls, nested tool calls, ill-formed helper calls
   (:func:`validate_semantics`).

Paper sec. 4.1.1: "The code is parsed and checked for any syntax or semantic
errors and for any violation of our DSL."  Code that fails any
check is rejected at the Planner and never reaches the enforcer -- so an LLM that emits,
e.g., ``item.subject.lower()`` (no method-call production exists in the BNF)
produces an *the Planner failure*, never a false positive.
"""

from __future__ import annotations

import ast

from .symbolic import HELPERS, call_name


class DSLRejectionError(Exception):
    """Raised when generated code violates the PAuth DSL."""


# AST node types permitted inside a slice (Appendix A, Production Rules).
_ALLOWED: tuple[type, ...] = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Load,
    ast.Store,
    # statements
    ast.Assign,
    ast.Expr,
    ast.If,
    ast.Pass,
    # expressions
    ast.Call,
    ast.keyword,
    ast.Name,
    ast.Constant,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Attribute,
    ast.Subscript,
    ast.Dict,
    ast.ListComp,      # pure map/filter over a signed collection (see _check_comprehensions)
    ast.comprehension,
    ast.Lambda,
    ast.List,
    ast.For,   # bounded for over a gateway-observed collection (see _check_bounded_for)
    # operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.And,
    ast.Or,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

# Explicitly forbidden constructs, mapped to a human-readable reason.
_FORBIDDEN = {
    ast.While: "while-loops are forbidden (rule 2a)",
    ast.Return: "return statements are forbidden (rule 1)",
    ast.Import: "imports are forbidden (rule 1)",
    ast.ImportFrom: "imports are forbidden (rule 1)",
    ast.SetComp: "comprehensions contain implicit loops (rule 2a1)",
    ast.DictComp: "comprehensions contain implicit loops (rule 2a1)",
    ast.GeneratorExp: "generator expressions contain implicit loops (rule 2a1)",
    ast.IfExp: "conditional (ternary) expressions are not in the DSL",
    ast.Try: "exception handling is forbidden (rule 1)",
    ast.With: "with-statements are forbidden",
    ast.ClassDef: "class definitions are forbidden",
    ast.JoinedStr: "f-strings are forbidden (rule 1)",
    ast.Set: "set literals are not in the DSL",
    ast.Global: "global statements are forbidden",
    ast.Nonlocal: "nonlocal statements are forbidden",
    ast.Yield: "yield is forbidden",
    ast.Not: "the 'not' operator is not in the <Condition> grammar",
}
# ast.Dict is ALLOWED: a dict literal of traced values is a deterministic
# construction the enforcer re-derives (like a list); taint propagates through it.


# DSL profiles (the experiment axis G in docs/SYSTEM_MODEL.md):
#   G1 = the Appendix A reconstruction defined in docs/SYSTEM_MODEL.md: flat
#        if only, the five paper helpers with tool-free lambdas, no for-loops,
#        comprehensions, dict literals, or sum, and strict single assignment
#        per name. Appendix A's contradictory helper-tool examples and
#        manual-unrolling appendix do not widen the executable profile. (The
#        underscore/dunder-attribute ban is kept in both profiles as a shared
#        sandbox-security fix.)
#   G2 = this repo's extended grammar (default): everything G1 accepts, plus
#        else/elif, nested if up to depth 3, bounded (nested) for, dict
#        literals, single-generator comprehensions, and the two blessed
#        assignment merges in validate_semantics.
DSL_PROFILE_PAPER = "g1"
DSL_PROFILE_EXTENDED = "g2"


def parse_and_validate(
    code: str, *, profile: str = DSL_PROFILE_EXTENDED
) -> ast.FunctionDef:
    """Parse ``code`` and check its *syntax* against the DSL.

    Returns the validated ``run`` function definition.  Semantic checks (which
    calls are allowed) happen later, in :func:`validate_semantics`, after dead
    code is stripped.

    ``profile`` selects the DSL version: :data:`DSL_PROFILE_EXTENDED`
    (default, this repo's grammar) or :data:`DSL_PROFILE_PAPER` (the
    Appendix A profile defined in ``docs/SYSTEM_MODEL.md``).
    """
    if profile not in (DSL_PROFILE_PAPER, DSL_PROFILE_EXTENDED):
        raise ValueError(f"unknown DSL profile {profile!r}")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:  # noqa: BLE001
        raise DSLRejectionError(f"syntax error: {exc}") from exc

    body = [s for s in tree.body if not _is_docstring(s)]
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise DSLRejectionError(
            "module must contain exactly one function definition"
        )
    func = body[0]
    if func.name != "run":
        raise DSLRejectionError(f"function must be named 'run', got '{func.name}'")

    for node in ast.walk(func):
        for forbidden, reason in _FORBIDDEN.items():
            if isinstance(node, forbidden):
                raise DSLRejectionError(reason)
        if not isinstance(node, _ALLOWED):
            raise DSLRejectionError(
                f"disallowed construct: {type(node).__name__}"
            )
        if isinstance(node, ast.FunctionDef) and node is not func:
            raise DSLRejectionError("nested function definitions are forbidden")
        # Dunder / private attribute access is a sandbox-escape primitive: from
        # any wrapped value, ``x.__getattr__.__globals__['__builtins__']`` reaches
        # the real builtins even under exec with __builtins__={}. Business field
        # paths never start with an underscore, so ban it outright.
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise DSLRejectionError(
                f"attribute '{node.attr}' is not allowed (underscore/dunder access)"
            )

    _check_no_nested_if(func)
    _check_bounded_for(func)
    _check_comprehensions(func)
    _check_lambdas(func)
    if profile == DSL_PROFILE_PAPER:
        _check_paper_profile(func)
    return func


def _check_paper_profile(func: ast.FunctionDef) -> None:
    """G1: enforce the Appendix A profile fixed in SYSTEM_MODEL.md."""
    _PAPER_FORBIDDEN: dict[type, str] = {
        ast.For: "for-loops are forbidden (rule 2a) [G1]",
        ast.ListComp: "comprehensions contain implicit loops (rule 2a1) [G1]",
        ast.Dict: "dict literals are not in the DSL [G1]",
    }
    for node in ast.walk(func):
        for forbidden, reason in _PAPER_FORBIDDEN.items():
            if isinstance(node, forbidden):
                raise DSLRejectionError(reason)
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            if stmt.orelse:
                raise DSLRejectionError(
                    "else / elif blocks are forbidden (rule 10) [G1]"
                )
            for inner in stmt.body:
                if isinstance(inner, ast.If):
                    raise DSLRejectionError(
                        "nested if statements are forbidden (rule 10) [G1]"
                    )
    assigned: dict[str, int] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = assigned.get(target.id, 0) + 1
    reassigned = sorted(name for name, count in assigned.items() if count > 1)
    if reassigned:
        raise DSLRejectionError(
            "variables are re-assigned (rules 14a/14f) [G1]: "
            + ", ".join(reassigned)
        )


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


_MAX_IF_DEPTH = 3


def _check_no_nested_if(func: ast.FunctionDef) -> None:
    """Rule 10 (Tier-3): nested if/else IS allowed -- each enclosing test adds a
    conjunct to the leaf's path condition, and the enforcer already requires ALL
    guards to hold, so ``if C1: if C2: act()`` compiles to a rule demanding
    ``C1 and C2`` (authorized only on that exact path; off-path values match no
    rule -> FN=0). ``elif`` is the same shape (``else: if``) and is likewise
    allowed. Depth is capped so slice forking stays bounded."""

    def walk(stmts: list[ast.stmt], depth: int) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                if depth + 1 > _MAX_IF_DEPTH:
                    raise DSLRejectionError(
                        f"if-nesting deeper than {_MAX_IF_DEPTH} is forbidden (rule 10)"
                    )
                walk(stmt.body, depth + 1)
                walk(stmt.orelse, depth + 1)
            elif isinstance(stmt, ast.For):
                walk(stmt.body, depth)  # for-body shape is checked in _check_bounded_for

    walk(func.body, 0)


def _check_bounded_for(func: ast.FunctionDef) -> None:
    """Bounded for: ``for <var> in <collection>: <tool calls / nested for>``.

    Sliceable soundly: each loop var is a single fresh identifier, each iterable is
    a bound collection variable (a tool result) or a field of an outer loop var
    (``order.items`` -- a sub-collection), and a for-body holds only tool calls or
    NESTED for-loops. The slicer records the loop stack and the enforcer enumerates
    the signed collections' nested product, so the authorized set is exactly the
    reachable tuples (FN=0). A for may NOT appear inside an ``if`` (mixing a
    quantifier with a path guard under one leaf is out of scope)."""
    assigned = {t.id for n in ast.walk(func) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}

    def _iter_root(it: ast.expr) -> str | None:
        while isinstance(it, (ast.Attribute, ast.Subscript)):
            it = it.value
        return it.id if isinstance(it, ast.Name) else None

    def _check_for(
        stmt: ast.For,
        prior_bindings: set[str],
        outer_loop_vars: set[str],
    ) -> None:
        if not isinstance(stmt.target, ast.Name):
            raise DSLRejectionError("for-loop target must be a single variable (rule 2a)")
        if stmt.target.id in assigned:
            raise DSLRejectionError(f"for-loop variable '{stmt.target.id}' shadows an assignment")
        root = _iter_root(stmt.iter)
        if root is None:
            raise DSLRejectionError(
                "for-loop must iterate a bound collection variable or a field of one, "
                "e.g. `for x in items:` or `for i in order.items:` (rule 2a)"
            )
        if root not in prior_bindings and root not in outer_loop_vars:
            raise DSLRejectionError(
                "for-loop collection must come from an earlier top-level assignment "
                "or a field of an outer loop variable (rule 2a)"
            )
        if stmt.orelse:
            raise DSLRejectionError("for-else is forbidden")
        nested_vars = outer_loop_vars | {stmt.target.id}
        for inner in stmt.body:
            if isinstance(inner, ast.Pass):
                continue
            if isinstance(inner, ast.For):
                _check_for(inner, prior_bindings, nested_vars)
            elif not (isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Call)):
                raise DSLRejectionError(
                    "a for-body may contain only tool-call statements or nested "
                    "for-loops (no assignments or ifs)"
                )

    def _no_for_in_if(stmts: list[ast.stmt]) -> None:
        for s in stmts:
            if isinstance(s, ast.If):
                if any(isinstance(n, ast.For) for n in ast.walk(s)):
                    raise DSLRejectionError("for-loops may not appear inside an if body")
            elif isinstance(s, ast.For):
                _no_for_in_if(s.body)

    _no_for_in_if(func.body)
    prior_bindings: set[str] = set()
    for stmt in func.body:
        if isinstance(stmt, ast.For):
            _check_for(stmt, prior_bindings, set())
        elif isinstance(stmt, ast.Assign):
            prior_bindings.update(
                target.id for target in stmt.targets if isinstance(target, ast.Name)
            )


def _check_comprehensions(func: ast.FunctionDef) -> None:
    """A list comprehension must be a PURE map/filter over a bound collection: a
    single generator, a fresh Name target, an iterable that is a bound variable or
    a field of one, and no nested comprehension. Then the enforcer re-derives it
    deterministically (a tool call inside is rejected by validate_semantics as a
    nested call), so an off-collection value is off-slice (FN=0)."""
    for comp in ast.walk(func):
        if not isinstance(comp, ast.ListComp):
            continue
        if len(comp.generators) != 1:
            raise DSLRejectionError("only single-generator comprehensions are allowed")
        gen = comp.generators[0]
        if getattr(gen, "is_async", 0):
            raise DSLRejectionError("async comprehensions are forbidden")
        if not isinstance(gen.target, ast.Name):
            raise DSLRejectionError("comprehension target must be a single variable")
        it = gen.iter
        while isinstance(it, (ast.Attribute, ast.Subscript)):
            it = it.value
        if not isinstance(it, ast.Name):
            raise DSLRejectionError(
                "comprehension must iterate a bound collection variable or a field of one"
            )
        for inner in ast.walk(comp.elt):
            if isinstance(inner, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                raise DSLRejectionError("nested comprehensions are forbidden")


def _check_lambdas(func: ast.FunctionDef) -> None:
    """Lambdas may only appear as the ``key=`` / ``predicate=`` argument of a
    helper call (Appendix A, ``<HelperCall>``)."""
    lambda_nodes: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) in HELPERS:
            for kw in node.keywords:
                if isinstance(kw.value, ast.Lambda):
                    lambda_nodes.add(id(kw.value))
    for node in ast.walk(func):
        if not isinstance(node, ast.Lambda):
            continue
        if id(node) not in lambda_nodes:
            raise DSLRejectionError(
                "lambdas are only allowed as key=/predicate= of helper calls"
            )
        args = node.args
        if (
            len(args.args) != 1
            or args.posonlyargs
            or args.vararg is not None
            or args.kwonlyargs
            or args.kwarg is not None
            or args.defaults
            or args.kw_defaults
        ):
            raise DSLRejectionError(
                "helper lambdas must take exactly one positional argument"
            )


def strip_dead_code(func: ast.FunctionDef, tool_names: set[str]) -> ast.FunctionDef:
    """Remove dead bare-expression statements.

    Paper sec. 4.1.1: "any call to functions other than the given tools (such
    as built-in Python functions like print or output) is marked as
    unreachable and removed."  Only *statement-level* bare calls can be
    dropped; an unknown call used as a value is a hard semantic error caught
    by :func:`validate_semantics`.

    Bodies emptied by stripping receive an explicit ``pass`` so the code
    remains well-formed.
    """

    def is_dead(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and call_name(stmt.value) not in tool_names
        )

    def clean(stmts: list[ast.stmt]) -> list[ast.stmt]:
        kept = [s for s in stmts if not is_dead(s)] or [ast.Pass()]
        for s in kept:
            if isinstance(s, ast.If):
                s.body = clean(s.body)
                s.orelse = clean(s.orelse) if s.orelse else s.orelse
            elif isinstance(s, ast.For):
                s.body = clean(s.body)
        return kept

    func.body = clean(func.body)
    return func


def validate_semantics(
    func: ast.FunctionDef,
    tool_names: set[str],
    *,
    profile: str = DSL_PROFILE_EXTENDED,
) -> None:
    """Check the *semantic* rules of the DSL after dead-code removal.

    Enforces the Appendix A reconstruction fixed in ``SYSTEM_MODEL.md``:

    * every call target is a bare identifier -- no method calls like
      ``s.lower()`` (there is no method-call production);
    * every called name is a provided tool or one of the five helpers
      (rule 2: "Only call the provided tools");
    * a helper's first argument is normally a bare identifier
      (``<HelperCall>`` takes ``<Identifier>``);
    * a tool call appears only as a statement or as the right-hand side of an
      assignment -- never nested inside another expression (rules 2b3, 16).
      Appendix A's contradictory helper-lambda examples are currently rejected
      because their ordered occurrence provenance is not yet executable.
    """
    # Tool calls that the slicer can see directly: statement-level (incl.
    # if / else / for bodies) and let-RHS.
    sliceable: set[int] = set()
    bodies: list[list[ast.stmt]] = []

    def _collect_bodies(stmts: list[ast.stmt]) -> None:
        bodies.append(stmts)
        for s in stmts:
            if isinstance(s, ast.If):
                _collect_bodies(s.body)
                _collect_bodies(s.orelse)
            elif isinstance(s, ast.For):
                _collect_bodies(s.body)

    _collect_bodies(func.body)
    for body in bodies:
        for stmt in body:
            call: ast.expr | None = None
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                call = stmt.value
            if isinstance(call, ast.Call) and call_name(call) in tool_names:
                sliceable.add(id(call))

    # Appendix A's examples contain tool calls inside helper lambdas, but the
    # current runtime cannot yet bind those repeated occurrences to an exact,
    # ordered execution history.  Keep the executable profile fail-closed:
    # every nested tool call is rejected until that provenance model is complete.
    helper_tool_ids: set[int] = set()

    # Each variable has a single definition, with TWO merge exceptions the slicer
    # models soundly by forking into mutually-exclusive branch-slices (the
    # enforcer's match-any turns those into a sound disjunction; an off-branch
    # value matches none -> still default-deny):
    #   (1) default-then-conditional: a top-level CONSTANT default + one if-body
    #       assign (``x = ""``; ``if C: x = e``)  -> const (no guard) / e (C).
    #   (2) if/else merge: one if-body + one else-body assign of the SAME if
    #       (``if C: x = a`` / ``else: x = b``)   -> a (C) / b (not C).
    # Anything else (non-constant default, >1 conditional set, cross-if pairs,
    # 3+ defs) needs path-merge logic we do not have, so it stays rejected.
    sites: dict[str, list[tuple[str, int | None]]] = {}

    def _add_site(assign: ast.Assign, tag: str, key: int | None) -> None:
        for t in assign.targets:
            if isinstance(t, ast.Name):
                sites.setdefault(t.id, []).append((tag, key))

    def _collect_sites(stmts: list[ast.stmt], context: object) -> None:
        # context: "top", ("if_body", ifid), ("else_body", ifid), or "nested".
        # Only a TOP-LEVEL if's DIRECT body/else earn the flat merge tags; anything
        # deeper is "nested" and matches no allowed merge pattern, so a variable
        # reassigned in a nested branch is rejected. This keeps the FN-safe
        # single-definition rule for everything the nesting introduces -- the two
        # blessed merges (const-default, if/else) stay flat-only.
        for stmt in stmts:
            if isinstance(stmt, ast.Assign):
                if context == "top":
                    tag = "top_const" if isinstance(stmt.value, ast.Constant) else "top_other"
                    _add_site(stmt, tag, None)
                elif isinstance(context, tuple):
                    _add_site(stmt, context[0], context[1])
                else:
                    _add_site(stmt, "nested", id(stmt))
            elif isinstance(stmt, ast.If):
                if context == "top":
                    _collect_sites(stmt.body, ("if_body", id(stmt)))
                    _collect_sites(stmt.orelse, ("else_body", id(stmt)))
                else:
                    _collect_sites(stmt.body, "nested")
                    _collect_sites(stmt.orelse, "nested")
            elif isinstance(stmt, ast.For):
                _collect_sites(stmt.body, "nested")

    _collect_sites(func.body, "top")
    names = set(sites)
    reassigned = []
    for name, locs in sites.items():
        if len(locs) <= 1:
            continue
        kinds = sorted(k for k, _ in locs)
        const_default = (
            len(locs) == 2 and kinds == ["if_body", "top_const"]
        )
        ifelse_merge = (
            len(locs) == 2 and kinds == ["else_body", "if_body"]
            and locs[0][1] == locs[1][1]  # both branches of the SAME if
        )
        if not (const_default or ifelse_merge):
            reassigned.append(name)
    if reassigned:
        raise DSLRejectionError(
            f"variable(s) {sorted(reassigned)} assigned more than once; each variable "
            "must have a single definition (rules 14a/14f: no scoped assignments)"
        )

    # A tool/helper name must always resolve to the enforcer wrapper. Allowing an
    # assignment to shadow one (``send = something``) lets DSL-valid code call
    # an arbitrary callable through a name the call-target check accepts -- the
    # second half of the sandbox escape. Forbid shadowing outright.
    shadowed = sorted(names & (tool_names | HELPERS))
    if shadowed:
        raise DSLRejectionError(
            f"name(s) {shadowed} shadow a tool/helper; tool and helper names "
            "cannot be reassigned"
        )

    if profile not in (DSL_PROFILE_PAPER, DSL_PROFILE_EXTENDED):
        raise ValueError(f"unknown DSL profile {profile!r}")

    def _check_helper_shape(node: ast.Call, name: str) -> None:
        paper_lambda_tool_input = (
            len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and id(node.args[0]) in helper_tool_ids
        )
        if (
            len(node.args) != 1
            or not (
                isinstance(node.args[0], ast.Name)
                or paper_lambda_tool_input
            )
        ):
            raise DSLRejectionError(
                f"helper '{name}' must take exactly one bare variable as its "
                "positional argument; nested tool results are not executable"
            )
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        if len(keywords) != len(node.keywords):
            raise DSLRejectionError(f"helper '{name}' does not allow **kwargs")
        if name == "len":
            expected: set[str] = set()
        elif name in {"min", "max"}:
            expected = {"key"}
        elif name in {"first", "last"}:
            expected = {"predicate"}
        else:  # G2-only sum(): projection is optional.
            expected = set() if not keywords else {"key"}
        if set(keywords) != expected or any(
            not isinstance(value, ast.Lambda) for value in keywords.values()
        ):
            rendered = {
                "len": "len(values)",
                "min": "min(values, key=lambda value: ...)",
                "max": "max(values, key=lambda value: ...)",
                "first": "first(values, predicate=lambda value: ...)",
                "last": "last(values, predicate=lambda value: ...)",
                "sum": "sum(values) or sum(values, key=lambda value: ...)",
            }[name]
            raise DSLRejectionError(
                f"helper '{name}' must use the form {rendered}"
            )

    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            raise DSLRejectionError(
                "method calls are not in the DSL "
                f"(found '{ast.unparse(node.func)}(...)')"
            )
        name = node.func.id
        if name not in tool_names and name not in HELPERS:
            raise DSLRejectionError(
                f"call to '{name}' which is neither a provided tool nor a helper (rule 2)"
            )
        if name in HELPERS:
            if profile == DSL_PROFILE_PAPER and name == "sum":
                raise DSLRejectionError(
                    "helper 'sum' is an extension and is forbidden in G1"
                )
            _check_helper_shape(node, name)
        elif node.keywords:
            # Tool calls MUST be positional-only. The slicer and the taint gate
            # build operand rules from positional args only; a keyword-passed
            # control operand (recipient/amount) would otherwise be enforced by
            # neither and skip the confirmation gate. Rule 6 already mandates
            # positional args, so reject any keyword on a tool call.
            raise DSLRejectionError(
                f"tool call '{name}(...)' uses keyword arguments; tools must be "
                "called with positional arguments only (rule 6)"
            )
        elif id(node) not in sliceable and id(node) not in helper_tool_ids:
            raise DSLRejectionError(
                f"tool call '{name}(...)' is nested inside an expression; tool "
                "results must be assigned first. Helper-lambda tool calls are "
                "rejected until their ordered occurrence provenance is implemented"
            )
