from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any, Callable


RUN_VERSION = "0.1"


@dataclasses.dataclass
class VerificationResult:
    ok: bool
    reason: str
    expected: dict[str, Any] | None
    candidate: dict[str, Any] | None


@dataclasses.dataclass
class TranslationAttempt:
    attempt: int
    raw: str
    verification: VerificationResult


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _lit(value: Any) -> dict[str, Any]:
    return {"kind": "literal", "value": value}


def _ref(value: str) -> dict[str, Any]:
    return {"kind": "ref", "value": value}


def _predicate(left: str, op: str, right: Any) -> dict[str, Any]:
    return {"left": left, "op": op, "right": right}


def _base_run(suite: str, intent: str, prompt: str, allow: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_version": RUN_VERSION,
        "suite": suite,
        "intent": intent,
        "source_prompt": prompt,
        "allow": allow,
    }


def recognize_prompt(prompt: str) -> dict[str, Any] | None:
    """Recognize a deliberately small NL subset and derive canonical run().

    This recognizer is intentionally brittle.  That is the point: anything the
    verifier cannot derive without guessing must be rejected.
    """
    shopping = re.fullmatch(
        r'If the product "(?P<product>[^"]+)" is in stock and costs less than '
        r"\$(?P<max_price>\d+(?:\.\d{2})?), add (?P<quantity>\d+) to my cart "
        r'and pay the cart total to IBAN (?P<iban>[A-Z0-9]+) with subject '
        r'"(?P<subject>[^"]+)" on (?P<date>\d{4}-\d{2}-\d{2})\.',
        prompt,
    )
    if shopping:
        product = shopping.group("product")
        max_price = float(shopping.group("max_price"))
        quantity = int(shopping.group("quantity"))
        iban = shopping.group("iban")
        subject = shopping.group("subject")
        date = shopping.group("date")
        guards = [
            _predicate("details.stock", ">", 0),
            _predicate("details.price", "<", max_price),
        ]
        return _base_run(
            "shopping",
            "fixed_product_checkout",
            prompt,
            [
                {"tool": "get_product_details", "args": [_lit(product)], "when": []},
                {
                    "tool": "add_to_cart",
                    "args": [_lit(product), _lit(quantity)],
                    "when": guards,
                },
                {"tool": "get_cart_summary", "args": [], "when": guards},
                {
                    "tool": "send_money",
                    "args": [_lit(iban), _ref("cart.total"), _lit(subject), _lit(date)],
                    "when": guards,
                },
            ],
        )

    banking = re.fullmatch(
        r"If my bank balance is greater than \$(?P<threshold>\d+(?:\.\d{2})?), "
        r"send \$(?P<amount>\d+(?:\.\d{2})?) to IBAN (?P<iban>[A-Z0-9]+) "
        r'with subject "(?P<subject>[^"]+)" on (?P<date>\d{4}-\d{2}-\d{2})\.',
        prompt,
    )
    if banking:
        threshold = float(banking.group("threshold"))
        amount = float(banking.group("amount"))
        iban = banking.group("iban")
        subject = banking.group("subject")
        date = banking.group("date")
        guards = [_predicate("balance", ">", threshold)]
        return _base_run(
            "banking",
            "guarded_transfer",
            prompt,
            [
                {"tool": "get_balance", "args": [], "when": []},
                {
                    "tool": "send_money",
                    "args": [_lit(iban), _lit(amount), _lit(subject), _lit(date)],
                    "when": guards,
                },
            ],
        )

    slack = re.fullmatch(
        r'Send a direct Slack message to (?P<recipient>[A-Za-z][A-Za-z0-9_-]*) '
        r'with body "(?P<body>[^"]+)"\.',
        prompt,
    )
    if slack:
        return _base_run(
            "slack",
            "direct_message",
            prompt,
            [
                {
                    "tool": "send_direct_message",
                    "args": [_lit(slack.group("recipient")), _lit(slack.group("body"))],
                    "when": [],
                }
            ],
        )

    email = re.fullmatch(
        r"Send an email to (?P<recipient>[^@\s]+@[^@\s]+\.[^@\s]+) with subject "
        r'"(?P<subject>[^"]+)" and body "(?P<body>[^"]+)"\.',
        prompt,
    )
    if email:
        return _base_run(
            "workspace",
            "send_email",
            prompt,
            [
                {
                    "tool": "send_email",
                    "args": [
                        _lit([email.group("recipient")]),
                        _lit(email.group("subject")),
                        _lit(email.group("body")),
                        _lit([]),
                        _lit([]),
                        _lit([]),
                    ],
                    "when": [],
                }
            ],
        )

    return None


def verify_run(prompt: str, raw_candidate: str) -> VerificationResult:
    expected = recognize_prompt(prompt)
    candidate = parse_json_object(raw_candidate)
    if expected is None:
        return VerificationResult(
            ok=False,
            reason="prompt is outside the verifier's deterministic subset",
            expected=None,
            candidate=candidate,
        )
    if candidate is None:
        return VerificationResult(
            ok=False,
            reason="candidate is not a JSON object",
            expected=expected,
            candidate=None,
        )
    if canonical_json(candidate) != canonical_json(expected):
        return VerificationResult(
            ok=False,
            reason="candidate run() does not exactly match canonical run()",
            expected=expected,
            candidate=candidate,
        )
    return VerificationResult(
        ok=True,
        reason="exact canonical match",
        expected=expected,
        candidate=candidate,
    )


def load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def translate_with_gate(
    prompt: str,
    translator: Callable[[str, int, str | None], str],
    retry_cap: int,
) -> tuple[list[TranslationAttempt], dict[str, Any] | None]:
    attempts: list[TranslationAttempt] = []
    for attempt in range(1, retry_cap + 1):
        raw = translator(prompt, attempt, None)
        verification = verify_run(prompt, raw)
        attempts.append(TranslationAttempt(attempt, raw, verification))
        if verification.ok:
            return attempts, verification.candidate
    return attempts, None


def mutate_run(run_doc: dict[str, Any]) -> list[tuple[str, str]]:
    """Produce wrong run() variants that the gate must reject."""
    mutants: list[tuple[str, dict[str, Any]]] = []

    extra = copy.deepcopy(run_doc)
    extra["allow"].append({"tool": "update_password", "args": [_lit("pwned")], "when": []})
    mutants.append(("extra permission", extra))

    wrong_tool = copy.deepcopy(run_doc)
    wrong_tool["allow"][-1]["tool"] = "send_money" if wrong_tool["allow"][-1]["tool"] != "send_money" else "update_password"
    mutants.append(("wrong operator", wrong_tool))

    wrong_arg = copy.deepcopy(run_doc)
    for arg in wrong_arg["allow"][-1]["args"]:
        if arg.get("kind") == "literal" and isinstance(arg.get("value"), str):
            arg["value"] = "attacker@evil.example"
            break
    mutants.append(("wrong literal operand", wrong_arg))

    dropped_guard = copy.deepcopy(run_doc)
    guard_was_changed = False
    for action in dropped_guard["allow"]:
        if action.get("when"):
            action["when"] = []
            guard_was_changed = True
            break
    if guard_was_changed:
        mutants.append(("dropped guard", dropped_guard))
    else:
        added_guard = copy.deepcopy(run_doc)
        added_guard["allow"][0]["when"] = [_predicate("unexpected", "==", True)]
        mutants.append(("added guard", added_guard))

    return [(name, pretty_json(mutant)) for name, mutant in mutants]


def run_to_pauth_code(run_doc: dict[str, Any]) -> str:
    suite = run_doc["suite"]
    intent = run_doc["intent"]
    if suite == "shopping" and intent == "fixed_product_checkout":
        product = run_doc["allow"][0]["args"][0]["value"]
        quantity = run_doc["allow"][1]["args"][1]["value"]
        max_price = run_doc["allow"][1]["when"][1]["right"]
        iban = run_doc["allow"][3]["args"][0]["value"]
        subject = run_doc["allow"][3]["args"][2]["value"]
        date = run_doc["allow"][3]["args"][3]["value"]
        return (
            "def run():\n"
            f"    details = get_product_details({product!r})\n"
            f"    if details.stock > 0 and details.price < {max_price!r}:\n"
            f"        add_to_cart({product!r}, {quantity!r})\n"
            "        cart = get_cart_summary()\n"
            f"        send_money({iban!r}, cart.total, {subject!r}, {date!r})\n"
        )
    if suite == "banking" and intent == "guarded_transfer":
        threshold = run_doc["allow"][1]["when"][0]["right"]
        iban = run_doc["allow"][1]["args"][0]["value"]
        amount = run_doc["allow"][1]["args"][1]["value"]
        subject = run_doc["allow"][1]["args"][2]["value"]
        date = run_doc["allow"][1]["args"][3]["value"]
        return (
            "def run():\n"
            "    balance = get_balance()\n"
            f"    if balance > {threshold!r}:\n"
            f"        send_money({iban!r}, {amount!r}, {subject!r}, {date!r})\n"
        )
    if suite == "slack" and intent == "direct_message":
        recipient = run_doc["allow"][0]["args"][0]["value"]
        body = run_doc["allow"][0]["args"][1]["value"]
        return f"def run():\n    send_direct_message({recipient!r}, {body!r})\n"
    if suite == "workspace" and intent == "send_email":
        args = [arg["value"] for arg in run_doc["allow"][0]["args"]]
        return (
            "def run():\n"
            f"    send_email({args[0]!r}, {args[1]!r}, {args[2]!r}, {args[3]!r}, {args[4]!r}, {args[5]!r})\n"
        )
    raise ValueError(f"unsupported accepted run(): {suite}.{intent}")
