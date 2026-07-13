"""A1 -- imperative code generation (paper sec. 4.1.1).

Given a user task and the schema of the available tools (extended with an
*output* schema, paper sec. 4.1.1), an LLM generates a ``run`` function in the
restricted grammar of Appendix A.  This is the only LLM-dependent step of
PAuth; everything downstream (A2, A3, B1-B4) is deterministic.

The OpenAI call is made lazily and only when ``OPENAI_API_KEY`` is available.
Generated code is cached on disk so a benchmark re-run costs nothing.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any

# System prompt -- a faithful rendering of paper Appendix A ("Prompt Used for
# Slice Generation"), including the production-rule BNF.  Kept close to the
# paper so reproductions are comparable, and complete enough that the LLM
# reliably stays inside the restricted grammar.
SYSTEM_PROMPT = """\
You translate a user's natural-language task into a single Python function
named `run` that calls the provided tools. The function is NOT executed for
its return value; it is a faithful imperative specification of the task.

The code MUST conform to this grammar (Appendix A production rules):

  <Slice>        ::= def run(<Parameters>): <Body>
  <Body>         ::= <StmtList>                          (4-space indented)
  <Stmt>         ::= <Assignment> | <ToolCall> | <Conditional> | <ForLoop> | pass
  <Assignment>   ::= <Identifier> = <Expr>
  <ToolCall>     ::= <Identifier> ( <ArgList> )
  <Conditional>  ::= if <Condition> : <Stmt>...           (body indented 8)
                   | if <Condition> : <Stmt>... else : <Stmt>...   (one flat else)
  <ForLoop>      ::= for <Identifier> in <Identifier> :   (iterate a bound
                       <ToolCall>...                         collection variable;
                                                             body = tool calls only)
  <Condition>    ::= <ConditionTerm>
                   | <Condition> and <ConditionTerm>
                   | <Condition> or <ConditionTerm>
  <ConditionTerm>::= <Expr> <RelOp> <Expr>
  <RelOp>        ::= <= | >= | < | > | == | !=
  <FieldAccess>  ::= <Identifier>.<Identifier>
                   | <FieldAccess>.<Identifier>
                   | <Identifier>[<number>].<Identifier>
  <HelperCall>   ::= len(<Identifier>)
                   | min(<Identifier>, key=<Lambda>)
                   | max(<Identifier>, key=<Lambda>)
                   | first(<Identifier>, predicate=<Lambda>)
                   | last(<Identifier>, predicate=<Lambda>)
  <Lambda>       ::= lambda <Identifier>: <Expr>
  <Expr>         ::= <Literal> | <Identifier> | <FieldAccess>
                   | <ToolCall> | <HelperCall> | <ArithExpr>
  <ArithExpr>    ::= <Expr> <ArithOp> <Expr>
  <ArithOp>      ::= + | - | * | / | // | %

There is NO production for: while-loops, comprehensions, generator/`any`/`all`,
ternary (`x if c else y`), `not`, method calls (e.g. `s.lower()`,
`s.startswith(...)`), type casts (`float(...)`, `int(...)`, `str(...)`),
dict/set literals, `elif`, if-inside-if (nested if), multiple functions. Do not
use them. A `for` loop and a single `else` ARE allowed, but only in the exact
shapes described in rules 2a and 10 below.
The first argument of every helper MUST be a bare variable name -- never a
tool call. A tool call may appear only as a statement or as the right-hand
side of an assignment, never nested inside another expression.

STRICT RULES for the python function named 'run':
1. Use only a subset of Python: no imports, no comments, no return statements,
   no print/logging, no f-strings, no exception handling, no type hints, no
   docstrings.
1a. ALWAYS use double quotes (") for all string literals.
2. Only call the provided tools - no other functions or libraries.
2a. 'while' loops are strictly forbidden. A 'for' loop is allowed ONLY in this
    exact shape -- iterate a variable that holds a prior tool result, with a body
    of tool-call statements only:
        items = list_items(None)
        for it in items:
            do_something(it.field, 1)
    The loop variable must be a fresh name; the iterable must be a bare variable
    naming an earlier tool result (NEVER range(), a literal, or an expression);
    the body may contain ONLY tool calls (no assignments, no if, no nested loop).
    When the task is find-the-best / aggregate rather than act-on-each, prefer the
    helpers len(), min(), max(), first(), last() over a loop.
2a1. FORBIDDEN: any(), all() and generator expressions. Use nested first()
     calls instead.
2b. ALLOWED HELPER FUNCTIONS - you may use these five helpers:
    - len(iterable): length of an iterable.
    - min(iterable, key=lambda item: item.field): minimum element by key.
    - max(iterable, key=lambda item: item.field): maximum element by key.
    - first(iterable, predicate=lambda item: condition): first matching
      element, or None. Always use the 'predicate=' keyword.
    - last(iterable, predicate=lambda item: condition): last matching element,
      or None. Always use the 'predicate=' keyword.
2b3. Helper functions MUST receive variables, not function calls: assign tool
     results to a variable first, then pass the variable to the helper.
2c. To 'find the item with the most/least X' you MUST use min()/max() with a
    key function. Never unroll comparisons with if-statements.
3. Only use basic arithmetic operations (+, -, *, /, //, %).
4. Call tools directly by their function names without any service prefixes.
5. Function signature must be 'def run(<params>):' followed by indented
   statements only. Use 4-space indentation; statements inside an if use 8.
6. Use positional arguments only when calling tools.
6a. Parameter order MUST match the order shown in the tool schema exactly.
6c. When using positional arguments, pass one value for EVERY parameter in
    schema order. For optional parameters you do not need, pass None (or []
    for array parameters). Never omit a parameter.
7. If there is nothing to be done, output 'def run():\\n    pass'.
8. Access object fields with dot notation (result.field_name).
8a. Never access the same field twice in one expression - store results in a
    variable first.
9. Call ALL relevant tools, including those with no parameters.
10. CONDITIONAL STATEMENTS: combine conditions with and/or. A single flat 'else'
    block IS allowed (`if C: ...` / `else: ...`). STILL FORBIDDEN: 'elif', and any
    'if' nested inside another if/else/for body -- keep conditionals one level deep.
10a. A variable may be assigned twice ONLY as (i) a constant default then one
     conditional override (`x = ""` then `if C: x = expr`), or (ii) both branches
     of the same if/else (`if C: x = a` / `else: x = b`). Otherwise assign each
     variable exactly once.
11. If the user provides specific values, use them as constants directly.
    Create function parameters only for values not specified in the request.
12. Always use the exact field names from the tool schemas.
13. Conditions read tool results: if result.field operator value: action.
14. TOOL CALLING: result = tool_name(parameters); use result.field in
    conditions and actions; assign tool results to variables before use.
15. Follow the user input EXACTLY: do not modify, interpret or add
    assumptions. Use the exact values described in the request.
16. NEVER write tool_name().field, tool_name().tool_name(), or repeat a tool
    call inside one expression - always assign to a variable first.
17. Always prefer the shortest, most concise solution.

Output ONLY the code of the `run` function, with no explanation and no
markdown fences.
"""


@dataclasses.dataclass
class ToolDoc:
    """A tool schema, extended with an output schema (paper sec. 4.1.1)."""

    name: str
    description: str
    parameters: list[dict[str, str]]   # ordered: {name, type, description?}
    returns: str

    def render(self) -> str:
        params = ", ".join(
            f"{p['name']}: {p.get('type', 'any')}" for p in self.parameters
        )
        lines = [f"- {self.name}({params})", f"    description: {self.description}"]
        if self.parameters:
            lines.append("    parameters (in order):")
            for i, p in enumerate(self.parameters):
                desc = f" -- {p['desc']}" if p.get("desc") else ""
                lines.append(f"      {i}. {p['name']}: {p.get('type', 'any')}{desc}")
        lines.append(f"    returns: {self.returns}")
        return "\n".join(lines)


@dataclasses.dataclass
class CodegenResult:
    code: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cached: bool
    model: str


# Approximate USD pricing per 1M tokens.  Override via PAUTH_PRICING if needed.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4o": (2.50, 10.00),
}


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = _PRICING.get(model, _PRICING["gpt-4.1"])
    return prompt_tokens / 1e6 * inp + completion_tokens / 1e6 * out


def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model added them despite instructions."""
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def build_user_prompt(task: str, tools: list[ToolDoc]) -> str:
    schema = "\n".join(t.render() for t in tools)
    return (
        "AVAILABLE TOOLS (name, parameters in order, return/output schema):\n"
        f"{schema}\n\n"
        "USER TASK:\n"
        f"{task}\n\n"
        "Generate the `run` function now."
    )


def has_api_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def generate_code(
    task: str,
    tools: list[ToolDoc],
    model: str = "gpt-4.1",
    cache_path: Path | None = None,
    client: Any | None = None,
) -> CodegenResult:
    """Generate the imperative ``run`` function for a task.

    If ``cache_path`` exists the cached code is returned at zero cost.
    Otherwise the OpenAI API is called; this requires ``OPENAI_API_KEY``.
    """
    if cache_path is not None and cache_path.exists():
        meta_path = cache_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return CodegenResult(
            code=cache_path.read_text(),
            prompt_tokens=meta.get("prompt_tokens", 0),
            completion_tokens=meta.get("completion_tokens", 0),
            cost_usd=0.0,  # already paid; cached re-use is free
            cached=True,
            model=meta.get("model", model),
        )

    if client is None:
        if not has_api_key():
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it (or put it in .env) to run "
                "the A1 code-generation step."
            )
        from openai import OpenAI  # imported lazily

        client = OpenAI()

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(task, tools)},
        ],
    )
    code = _strip_fences(response.choices[0].message.content or "")
    usage = response.usage
    prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
    cost = _cost(model, prompt_tokens, completion_tokens)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(code)
        cache_path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": cost,
                },
                indent=2,
            )
        )

    return CodegenResult(
        code=code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        cached=False,
        model=model,
    )
