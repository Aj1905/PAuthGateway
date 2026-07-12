"""Restricted-grammar parser and validator (paper Appendix A).

The A1 step asks an LLM to generate a ``run`` function in a restrictive subset
of Python.  Before any slice is derived we

1. parse the code and reject anything outside the grammar's *syntax*
   (:func:`parse_and_validate`);
2. remove dead / unreachable statements (:func:`strip_dead_code`);
3. reject anything that violates the grammar's *semantics* -- calls to
   non-tools, method calls, nested tool calls, ill-formed helper calls
   (:func:`validate_semantics`).

Paper sec. 4.1.1: "The code is parsed and checked for any syntax or semantic
errors and for any violation of our restrictive grammar."  Code that fails any
check is rejected at A1 and never reaches the enforcer -- so an LLM that emits,
e.g., ``item.subject.lower()`` (no method-call production exists in the BNF)
produces an *A1 failure*, never a false positive.
"""

from __future__ import annotations

import ast

from .symbolic import HELPERS, call_name


class RestrictedGrammarError(Exception):
    """Raised when generated code violates the PAuth restricted grammar."""


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
    ast.Lambda,
    ast.List,
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
    ast.For: "for-loops are forbidden (rule 2a)",
    ast.While: "while-loops are forbidden (rule 2a)",
    ast.Return: "return statements are forbidden (rule 1)",
    ast.Import: "imports are forbidden (rule 1)",
    ast.ImportFrom: "imports are forbidden (rule 1)",
    ast.ListComp: "comprehensions contain implicit loops (rule 2a1)",
    ast.SetComp: "comprehensions contain implicit loops (rule 2a1)",
    ast.DictComp: "comprehensions contain implicit loops (rule 2a1)",
    ast.GeneratorExp: "generator expressions contain implicit loops (rule 2a1)",
    ast.IfExp: "conditional (ternary) expressions are not in the grammar",
    ast.Try: "exception handling is forbidden (rule 1)",
    ast.With: "with-statements are forbidden",
    ast.ClassDef: "class definitions are forbidden",
    ast.JoinedStr: "f-strings are forbidden (rule 1)",
    ast.Dict: "dict literals are not in the grammar",
    ast.Set: "set literals are not in the grammar",
    ast.Global: "global statements are forbidden",
    ast.Nonlocal: "nonlocal statements are forbidden",
    ast.Yield: "yield is forbidden",
    ast.Not: "the 'not' operator is not in the <Condition> grammar",
}


def parse_and_validate(code: str) -> ast.FunctionDef:
    """Parse ``code`` and check its *syntax* against the restricted grammar.

    Returns the validated ``run`` function definition.  Semantic checks (which
    calls are allowed) happen later, in :func:`validate_semantics`, after dead
    code is stripped.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:  # noqa: BLE001
        raise RestrictedGrammarError(f"syntax error: {exc}") from exc

    body = [s for s in tree.body if not _is_docstring(s)]
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise RestrictedGrammarError(
            "module must contain exactly one function definition"
        )
    func = body[0]
    if func.name != "run":
        raise RestrictedGrammarError(f"function must be named 'run', got '{func.name}'")

    for node in ast.walk(func):
        for forbidden, reason in _FORBIDDEN.items():
            if isinstance(node, forbidden):
                raise RestrictedGrammarError(reason)
        if not isinstance(node, _ALLOWED):
            raise RestrictedGrammarError(
                f"disallowed construct: {type(node).__name__}"
            )
        if isinstance(node, ast.FunctionDef) and node is not func:
            raise RestrictedGrammarError("nested function definitions are forbidden")
        # Dunder / private attribute access is a sandbox-escape primitive: from
        # any wrapped value, ``x.__getattr__.__globals__['__builtins__']`` reaches
        # the real builtins even under exec with __builtins__={}. Business field
        # paths never start with an underscore, so ban it outright.
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise RestrictedGrammarError(
                f"attribute '{node.attr}' is not allowed (underscore/dunder access)"
            )

    _check_no_nested_if(func)
    _check_lambdas(func)
    return func


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _check_no_nested_if(func: ast.FunctionDef) -> None:
    """Rule 10: no nested if, no elif. A PLAIN else is allowed (its statements
    slice under ``not C``); elif (``orelse == [If]``) and nested ifs stay banned."""
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
                raise RestrictedGrammarError("elif blocks are forbidden (rule 10)")
            for inner in list(stmt.body) + list(stmt.orelse):
                if isinstance(inner, ast.If):
                    raise RestrictedGrammarError("nested if statements are forbidden (rule 10)")


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
        if isinstance(node, ast.Lambda) and id(node) not in lambda_nodes:
            raise RestrictedGrammarError(
                "lambdas are only allowed as key=/predicate= of helper calls"
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

    func.body = [s for s in func.body if not is_dead(s)] or [ast.Pass()]
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            stmt.body = [s for s in stmt.body if not is_dead(s)] or [ast.Pass()]
    return func


def validate_semantics(func: ast.FunctionDef, tool_names: set[str]) -> None:
    """Check the *semantic* rules of the grammar after dead-code removal.

    Enforces, faithfully to Appendix A's BNF and rules:

    * every call target is a bare identifier -- no method calls like
      ``s.lower()`` (there is no method-call production);
    * every called name is a provided tool or one of the five helpers
      (rule 2: "Only call the provided tools");
    * a helper's first argument is a bare identifier (``<HelperCall>`` takes
      ``<Identifier>``), and tool results are never passed to a helper
      (rule 2b3);
    * a tool call appears only as a statement or as the right-hand side of an
      assignment -- never nested inside another expression (rules 2b3, 16).
    """
    # Tool calls that the slicer can see: statement-level and let-RHS.
    sliceable: set[int] = set()
    bodies = [func.body] + [s.body for s in func.body if isinstance(s, ast.If)]
    for body in bodies:
        for stmt in body:
            call: ast.expr | None = None
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                call = stmt.value
            if isinstance(call, ast.Call) and call_name(call) in tool_names:
                sliceable.add(id(call))

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
    for stmt in func.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    kind = "top_const" if isinstance(stmt.value, ast.Constant) else "top_other"
                    sites.setdefault(t.id, []).append((kind, None))
        elif isinstance(stmt, ast.If):
            for branch, tag in ((stmt.body, "if_body"), (stmt.orelse, "else_body")):
                for inner in branch:
                    if isinstance(inner, ast.Assign):
                        for t in inner.targets:
                            if isinstance(t, ast.Name):
                                sites.setdefault(t.id, []).append((tag, id(stmt)))
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
        raise RestrictedGrammarError(
            f"variable(s) {sorted(reassigned)} assigned more than once; each variable "
            "must have a single definition (rules 14a/14f: no scoped assignments)"
        )

    # A tool/helper name must always resolve to the enforcer wrapper. Allowing an
    # assignment to shadow one (``send = something``) lets grammar-valid code call
    # an arbitrary callable through a name the call-target check accepts -- the
    # second half of the sandbox escape. Forbid shadowing outright.
    shadowed = sorted(names & (tool_names | HELPERS))
    if shadowed:
        raise RestrictedGrammarError(
            f"name(s) {shadowed} shadow a tool/helper; tool and helper names "
            "cannot be reassigned"
        )

    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            raise RestrictedGrammarError(
                "method calls are not in the grammar "
                f"(found '{ast.unparse(node.func)}(...)')"
            )
        name = node.func.id
        if name not in tool_names and name not in HELPERS:
            raise RestrictedGrammarError(
                f"call to '{name}' which is neither a provided tool nor a helper (rule 2)"
            )
        if name in HELPERS:
            if not node.args or not isinstance(node.args[0], ast.Name):
                raise RestrictedGrammarError(
                    f"helper '{name}' must take a bare variable as its first argument "
                    "(rule 2b3 / <HelperCall>)"
                )
        elif node.keywords:
            # Tool calls MUST be positional-only. The slicer and the taint gate
            # build operand rules from positional args only; a keyword-passed
            # control operand (recipient/amount) would otherwise be enforced by
            # neither and skip the confirmation gate. Rule 6 already mandates
            # positional args, so reject any keyword on a tool call.
            raise RestrictedGrammarError(
                f"tool call '{name}(...)' uses keyword arguments; tools must be "
                "called with positional arguments only (rule 6)"
            )
        elif id(node) not in sliceable:
            raise RestrictedGrammarError(
                f"tool call '{name}(...)' is nested inside an expression; tool "
                "results must be assigned to a variable first (rules 2b3, 16)"
            )
