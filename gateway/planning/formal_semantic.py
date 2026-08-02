"""P5 / formal-semantic: a formally parsed task language mapped to restricted run().

Design contract: docs/SYSTEM_MODEL.md 第 2 部ノード 1「P5 / formal-semantic」.

This planner accepts only a *defined* task language (FSL-1 below), parses it
with a formal grammar, semantically maps it onto the deployed suite's tool
schema, and emits restricted ``run()`` code. No LLM is involved. Anything the
grammar cannot parse -- or the semantic analysis cannot resolve against the
tool schema -- is rejected as unparseable; nothing is implicitly completed.

FSL-1 (formal task language, version 1)::

    task   := stmt (";" stmt)*
    stmt   := bind | call | cond
    bind   := NAME "=" call
    call   := "call" NAME ["with" value ("," value)*]
    cond   := "if" value CMP value "then" stmt
    value  := STRING | NUMBER | ref
    ref    := NAME ("." NAME)*
    CMP    := "==" | "!=" | "<=" | ">=" | "<" | ">"

Tokens: STRING is double-quoted (no escapes); NUMBER is a decimal int/float
with optional leading ``-``; NAME is ``[A-Za-z_][A-Za-z0-9_]*``. The words
``call``, ``with``, ``if``, ``then`` are reserved.

Semantic rules (violations reject the task):

* the tool of every ``call`` must exist in the deployed suite;
* the argument count must equal the tool's parameter count;
* every ``ref`` must resolve to a name bound by an earlier ``bind``.

Example::

    m = call read_message; if m.amount <= 100 then call send_money with
    m.iban, m.amount, "Order", "2024-01-01"
"""

from __future__ import annotations

import dataclasses
import re

from pauth import prepare
from pauth.suites.base import SuiteSpec

LANGUAGE_ID = "fsl-1"

_RESERVED = {"call", "with", "if", "then"}

_TOKEN_RE = re.compile(
    r"""
      (?P<STRING>"[^"]*")
    | (?P<NUMBER>-?\d+(?:\.\d+)?)
    | (?P<NAME>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<CMP>==|!=|<=|>=|<|>)
    | (?P<EQ>=)
    | (?P<DOT>\.)
    | (?P<COMMA>,)
    | (?P<SEMI>;)
    | (?P<WS>\s+)
    """,
    re.VERBOSE,
)


class FormalParseError(Exception):
    """The prompt is outside FSL-1 or fails semantic analysis."""


@dataclasses.dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise FormalParseError(
                f"unparseable input at position {pos}: {text[pos:pos + 20]!r}"
            )
        kind = m.lastgroup or ""
        if kind != "WS":
            tokens.append(_Token(kind, m.group(), pos))
        pos = m.end()
    return tokens


@dataclasses.dataclass(frozen=True)
class _Call:
    tool: str
    args: tuple[str, ...]      # rendered Python expressions
    bind: str | None


@dataclasses.dataclass(frozen=True)
class _Cond:
    left: str
    op: str
    right: str
    body: "_Call | _Cond"


class _Parser:
    def __init__(self, tokens: list[_Token], suite: SuiteSpec) -> None:
        self.tokens = tokens
        self.i = 0
        self.suite = suite
        self.bound: set[str] = set()

    def _peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _next(self, kind: str | None = None, text: str | None = None) -> _Token:
        tok = self._peek()
        if tok is None:
            raise FormalParseError("unexpected end of input")
        if kind is not None and tok.kind != kind:
            raise FormalParseError(
                f"expected {kind} at position {tok.pos}, found {tok.text!r}"
            )
        if text is not None and tok.text != text:
            raise FormalParseError(
                f"expected {text!r} at position {tok.pos}, found {tok.text!r}"
            )
        self.i += 1
        return tok

    def parse_task(self) -> list["_Call | _Cond"]:
        stmts = [self._stmt()]
        while self._peek() is not None:
            self._next("SEMI")
            if self._peek() is None:
                break
            stmts.append(self._stmt())
        return stmts

    def _stmt(self) -> "_Call | _Cond":
        tok = self._peek()
        if tok is None:
            raise FormalParseError("unexpected end of input")
        if tok.kind == "NAME" and tok.text == "if":
            return self._cond()
        if tok.kind == "NAME" and tok.text == "call":
            return self._call(bind=None)
        if tok.kind == "NAME":
            name = self._next("NAME").text
            if name in _RESERVED:
                raise FormalParseError(
                    f"reserved word {name!r} cannot start a statement here"
                )
            self._next("EQ")
            call = self._call(bind=name)
            self.bound.add(name)
            return call
        raise FormalParseError(
            f"expected a statement at position {tok.pos}, found {tok.text!r}"
        )

    def _cond(self) -> _Cond:
        self._next("NAME", "if")
        left = self._value()
        op = self._next("CMP").text
        right = self._value()
        self._next("NAME", "then")
        body = self._stmt()
        return _Cond(left=left, op=op, right=right, body=body)

    def _call(self, bind: str | None) -> _Call:
        self._next("NAME", "call")
        tool = self._next("NAME").text
        if tool not in self.suite.tools:
            raise FormalParseError(
                f"unknown tool {tool!r}; deployed tools: "
                f"{sorted(self.suite.tools)}"
            )
        args: list[str] = []
        tok = self._peek()
        if tok is not None and tok.kind == "NAME" and tok.text == "with":
            self._next("NAME", "with")
            args.append(self._value())
            while (t := self._peek()) is not None and t.kind == "COMMA":
                self._next("COMMA")
                args.append(self._value())
        expected = len(self.suite.tools[tool].params)
        if len(args) != expected:
            raise FormalParseError(
                f"tool {tool!r} takes {expected} argument(s), got {len(args)}"
            )
        return _Call(tool=tool, args=tuple(args), bind=bind)

    def _value(self) -> str:
        tok = self._peek()
        if tok is None:
            raise FormalParseError("unexpected end of input in value")
        if tok.kind == "STRING":
            self._next("STRING")
            return repr(tok.text[1:-1])
        if tok.kind == "NUMBER":
            self._next("NUMBER")
            return tok.text
        if tok.kind == "NAME":
            root = self._next("NAME").text
            if root in _RESERVED:
                raise FormalParseError(
                    f"reserved word {root!r} cannot be used as a value"
                )
            if root not in self.bound:
                raise FormalParseError(
                    f"reference {root!r} does not resolve to an earlier binding"
                )
            parts = [root]
            while (t := self._peek()) is not None and t.kind == "DOT":
                self._next("DOT")
                parts.append(self._next("NAME").text)
            return ".".join(parts)
        raise FormalParseError(
            f"expected a value at position {tok.pos}, found {tok.text!r}"
        )


def _render(stmts: list["_Call | _Cond"]) -> str:
    lines = ["def run():"]

    def emit(stmt: "_Call | _Cond", depth: int) -> None:
        pad = "    " * (depth + 1)
        if isinstance(stmt, _Call):
            call = f"{stmt.tool}({', '.join(stmt.args)})"
            lines.append(f"{pad}{stmt.bind} = {call}" if stmt.bind else f"{pad}{call}")
        else:
            lines.append(f"{pad}if {stmt.left} {stmt.op} {stmt.right}:")
            emit(stmt.body, depth + 1)

    for stmt in stmts:
        emit(stmt, 0)
    return "\n".join(lines) + "\n"


def translate_formal_task(text: str, suite: SuiteSpec) -> tuple[str, int]:
    """Parse an FSL-1 task and return (restricted run() code, statement count)."""
    parser = _Parser(_tokenize(text), suite)
    stmts = parser.parse_task()
    return _render(stmts), len(stmts)


@dataclasses.dataclass(frozen=True)
class FormalSemanticPlanner:
    """P5: formal grammar over a defined task language; no LLM, no completion."""

    suite_name: str

    def generate(self, prompt, suite_loader):
        from .planner import PlanDraft, PlanGenerationError

        try:
            suite: SuiteSpec = suite_loader(self.suite_name)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a clean rejection
            raise PlanGenerationError(
                f"unknown suite {self.suite_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            code, n_stmts = translate_formal_task(prompt, suite)
        except FormalParseError as exc:
            raise PlanGenerationError(f"prompt is outside {LANGUAGE_ID}: {exc}") from exc
        try:
            prepare(code, suite.tool_names(), suite.tool_signer())
        except Exception as exc:  # noqa: BLE001 -- mapping bug surfaced honestly
            raise PlanGenerationError(
                f"formal-semantic mapping produced invalid code: {exc}"
            ) from exc
        return PlanDraft(
            suite_name=self.suite_name,
            code=code,
            reason=f"plan accepted via formal-semantic parser ({self.suite_name})",
            planner_metadata={
                "strategy": "formal-semantic",
                "language": LANGUAGE_ID,
                "statements": n_stmts,
            },
        )
