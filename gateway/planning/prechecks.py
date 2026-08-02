"""Deterministic pre-checks: mechanical one-sided safety validation.

The semantic judge is probabilistic. Everything that can be checked
mechanically must be checked mechanically, BEFORE the judge, so the
over-authorization classes below never depend on an LLM verdict:

1. Forbidden tools -- an explicit denylist.
2. Recipient addition -- recipient-like string constants (and IBAN/email
   shaped constants anywhere in the code) must appear in the user prompt.
3. Amount fabrication -- amount-like numeric constants must appear in the
   user prompt; quantity-like constants must appear or be the implicit 1.
4. Read-to-write escalation -- optionally, side-effecting tools require
   declared evidence keywords in the prompt (policy data, not NL parsing).

One-sided by construction: every check only *rejects*; a false trigger is an
over-rejection (recoverable via the retry loop), never an over-authorization.
Constants are compared against the prompt; variables are left to the
deterministic dataflow layers (Slicer/Rule-compiler + enforcer), which already bind them to
signed observations.

"""

from __future__ import annotations

import ast
import dataclasses
import re

from pauth.codegen import ToolDoc

IBAN_RE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Parameter-name tokens (split on "_") that mark a destination-like operand.
RECIPIENT_TOKENS = frozenset(
    {"recipient", "recipients", "iban", "account", "email", "emails", "address",
     "destination", "target", "channel", "to", "payee", "sender", "receiver",
     "contact", "phone", "mailbox"}
)
AMOUNT_TOKENS = frozenset({"amount"})
QUANTITY_TOKENS = frozenset({"quantity", "qty", "units"})

# Description substrings that mark a destination-like operand even when the
# parameter *name* does not (e.g. slack's ``user: "The user to invite."``).
# High-precision phrases only, to keep over-rejection low; these are checked
# case-insensitively against the parameter description.
RECIPIENT_DESC_SUBSTRINGS = (
    "recipient",
    "iban",
    "email address",
    "phone number",
    "the user to ",          # "the user to invite/add/remove/send"
    "share the file with",
    "send the message to",
    "send money to",
    "send the email to",
)
# An operand is an amount-like sink when its description marks it as a
# transaction amount. "amount of the transaction" / "amount to transfer" match;
# read filters like "inclusive price ceiling" do not.
AMOUNT_DESC_SUBSTRING = "amount"

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "single": 1,
}

_NUMERIC_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclasses.dataclass(frozen=True)
class PrecheckPolicy:
    """Deployment-tunable inputs to the deterministic pre-checks.

    ``write_tool_evidence`` maps a side-effecting tool name to keywords, at
    least one of which must appear (case-insensitive) in the user prompt for
    the tool to be callable. Tools absent from the map are not gated.

    ``recipient_params`` / ``amount_params`` let a deployment declare, per
    tool, exactly which parameters are destination-like or amount-like. These
    override the name/description heuristics for that (tool, param) pair, so a
    suite with unusual naming can be made precise instead of relying on the
    best-effort classifier. Keyed by tool name; values are parameter names.
    """

    forbidden_tools: frozenset[str] = frozenset()
    write_tool_evidence: dict[str, tuple[str, ...]] = dataclasses.field(
        default_factory=dict
    )
    recipient_params: dict[str, frozenset[str]] = dataclasses.field(
        default_factory=dict
    )
    amount_params: dict[str, frozenset[str]] = dataclasses.field(
        default_factory=dict
    )


DEFAULT_POLICY = PrecheckPolicy()


def _param_tokens(name: str) -> set[str]:
    return {t for t in name.lower().split("_") if t}


def _classify_param(
    tool: str,
    param_name: str,
    param_desc: str,
    policy: PrecheckPolicy,
) -> set[str]:
    """Return the operand roles for a parameter: recipient / amount / quantity.

    Precedence: explicit policy declarations, then parameter-name tokens, then
    description keywords. Description matching closes the gap where a sink
    parameter is named generically (e.g. ``user``) but its description makes
    the destination role clear.
    """
    roles: set[str] = set()
    tokens = _param_tokens(param_name)
    desc = (param_desc or "").casefold()

    if param_name in policy.recipient_params.get(tool, frozenset()):
        roles.add("recipient")
    elif tokens & RECIPIENT_TOKENS:
        roles.add("recipient")
    elif any(sub in desc for sub in RECIPIENT_DESC_SUBSTRINGS):
        roles.add("recipient")

    if param_name in policy.amount_params.get(tool, frozenset()):
        roles.add("amount")
    elif tokens & AMOUNT_TOKENS:
        roles.add("amount")
    elif AMOUNT_DESC_SUBSTRING in desc:
        roles.add("amount")

    if tokens & QUANTITY_TOKENS:
        roles.add("quantity")
    return roles


def _prompt_numbers(prompt: str) -> set[float]:
    numbers: set[float] = set()
    for tok in _NUMERIC_TOKEN_RE.findall(prompt):
        try:
            numbers.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", prompt, re.IGNORECASE):
            numbers.add(float(value))
    return numbers


def _string_entailed(value: str, prompt: str) -> bool:
    if value.casefold() in prompt.casefold():
        return True
    # IBANs are often written with grouping spaces; compare space-insensitively.
    squeezed_value = re.sub(r"\s+", "", value).casefold()
    squeezed_prompt = re.sub(r"\s+", "", prompt).casefold()
    return bool(squeezed_value) and squeezed_value in squeezed_prompt


def _number_entailed(value: float, prompt_numbers: set[float]) -> bool:
    return float(value) in prompt_numbers


def _tool_calls(func: ast.FunctionDef, tool_names: set[str]):
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in tool_names
        ):
            yield node


def _arg_bindings(call: ast.Call, doc: ToolDoc) -> list[tuple[str, str, ast.expr]]:
    """Pair each argument expression with its schema parameter name and desc."""
    bindings: list[tuple[str, str, ast.expr]] = []
    params = [(p["name"], p.get("desc", "")) for p in doc.parameters]
    by_name = {name: desc for name, desc in params}
    for i, arg in enumerate(call.args):
        if i < len(params):
            name, desc = params[i]
            bindings.append((name, desc, arg))
    for kw in call.keywords:
        if kw.arg:
            bindings.append((kw.arg, by_name.get(kw.arg, ""), kw.value))
    return bindings


def precheck_code(
    prompt: str,
    code: str,
    tools: list[ToolDoc],
    policy: PrecheckPolicy | None = None,
) -> list[str]:
    """Return deterministic one-sided violations for ``code`` against ``prompt``.

    An empty list means "no mechanical objection" -- NOT "safe"; the semantic
    judge and the runtime enforcer still apply. Unparseable code yields a
    violation so callers stay fail-closed (the DSL layer normally rejects
    it first anyway).
    """
    policy = policy or DEFAULT_POLICY
    try:
        module = ast.parse(code)
    except SyntaxError as exc:
        return [f"code does not parse: {exc}"]
    funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    if not funcs:
        return ["no function definition found"]
    func = funcs[0]

    docs_by_name = {t.name: t for t in tools}
    tool_names = set(docs_by_name)
    prompt_numbers = _prompt_numbers(prompt)
    violations: list[str] = []

    called: set[str] = set()
    for call in _tool_calls(func, tool_names):
        name = call.func.id  # type: ignore[union-attr]
        called.add(name)
        if name in policy.forbidden_tools:
            violations.append(f"forbidden tool called: {name}")
            continue
        for param, desc, arg in _arg_bindings(call, docs_by_name[name]):
            if not isinstance(arg, ast.Constant):
                continue  # variables/field-accesses are dataflow-checked downstream
            roles = _classify_param(name, param, desc, policy)
            value = arg.value
            if isinstance(value, str) and "recipient" in roles:
                if not _string_entailed(value, prompt):
                    violations.append(
                        f"recipient-like constant {value!r} for parameter "
                        f"{param!r} of {name} does not appear in the user prompt"
                    )
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if "amount" in roles and not _number_entailed(value, prompt_numbers):
                    violations.append(
                        f"amount constant {value!r} for parameter {param!r} of "
                        f"{name} does not appear in the user prompt"
                    )
                if (
                    "quantity" in roles
                    and float(value) != 1.0
                    and not _number_entailed(value, prompt_numbers)
                ):
                    violations.append(
                        f"quantity constant {value!r} for parameter {param!r} of "
                        f"{name} does not appear in the user prompt"
                    )

    # Destination-shaped string constants anywhere in the function body must be
    # prompt-entailed, regardless of which parameter they flow into.
    for node in ast.walk(func):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if IBAN_RE.fullmatch(text) or EMAIL_RE.fullmatch(text):
                if not _string_entailed(text, prompt):
                    violations.append(
                        f"destination-shaped constant {text!r} does not appear "
                        "in the user prompt"
                    )

    prompt_lower = prompt.casefold()
    for name in sorted(called):
        keywords = policy.write_tool_evidence.get(name)
        if keywords and not any(kw.casefold() in prompt_lower for kw in keywords):
            violations.append(
                f"side-effecting tool {name} has no supporting evidence in the "
                f"user prompt (expected one of: {', '.join(keywords)})"
            )

    # Deduplicate while keeping order (the IBAN scan can duplicate a
    # recipient-parameter finding).
    seen: set[str] = set()
    unique: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique
