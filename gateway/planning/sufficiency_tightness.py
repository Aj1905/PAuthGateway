"""Coverage-first, delete-only authorization planning.

The first phase asks the model for a *sufficient* restricted ``run`` program:
all reads, dependencies, guards and requested effects needed by one plausible
execution path should be explicit.  The second phase does not get to rewrite
that program.  It may only select existing tool-call occurrences to retain.

That asymmetry is deliberate.  A free-form "tightness rewrite" could introduce
new tools, weaken a guard or broaden an operand while claiming to reduce
authority.  Here the gateway performs the deletion itself, so the final
program is structurally a subset of the coverage program.
"""

from __future__ import annotations

import ast
import collections
import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pauth.codegen import SYSTEM_PROMPT, ToolDoc, _cost, _strip_fences, build_user_prompt
from pauth.pipeline import PreparedTask, prepare
from pauth.symbolic import call_name

from .agentic_planner import (
    DEFAULT_JUDGE_MODEL,
    AgenticCodegenResult,
    JudgeVerdict,
    _call_generator,
    _get_generation_client,
    _get_judge_client,
    _judge_intent,
    generate_code_with_self_repair,
)
from .prechecks import PrecheckPolicy, precheck_code


class SufficiencyTightnessError(Exception):
    """The two-phase planner could not produce an audited subset plan."""


_PHASE_VERSION = "st-delete-only-v1"


_COVERAGE_INSTRUCTION = """\
This is the SUFFICIENCY phase of authorization planning.

Produce one restricted `run` function that makes the full execution closure
explicit:
- include every tool read and dependency needed to derive requested operands;
- preserve every condition, selection rule and requested side effect;
- include alternative branches only when the user's task genuinely requires
  them;
- do not optimize for fewer calls in this phase.

Coverage is not permission to invent behavior. Never add an unrequested side
effect, recipient, amount, destination or tool capability. A later,
delete-only audit will remove calls that are not justified.
"""


_TIGHTNESS_SYSTEM = """\
You audit an authorization plan for LEAST PRIVILEGE.

The coverage phase already produced a restricted `run` function. Every tool
call occurrence has an immutable action ID. Decide which existing IDs must be
kept to complete the user's task and which are unjustified excess.

Rules:
- You may KEEP or DROP existing action IDs only.
- You cannot add a tool, rewrite an operand, weaken a condition, broaden a
  loop, or invent a new action.
- Keep every data/control dependency of an action you keep.
- Keep an action when dropping it would make a requested effect, condition,
  selection, or runtime-derived operand impossible.
- Drop an action only when the user's task does not justify it.
- When evidence is genuinely ambiguous, keep the action and say why.

Return exactly one JSON object:
{"keep_action_ids": ["tool#0"],
 "drop_reasons": {"tool#1": "short concrete reason"}}

No markdown and no prose outside the JSON object.
"""


_AUDIT_REPAIR = """\
Your audit was rejected.

PROBLEM:
{problem}

Review the same immutable action catalog. You may restore or remove existing
IDs, but you still cannot add or rewrite an action. Return the JSON object only.
"""


@dataclasses.dataclass(frozen=True)
class ActionCandidate:
    """One immutable tool-call occurrence offered to the tightness auditor."""

    action_id: str
    tool: str
    source_line: int
    statement: str
    depends_on: tuple[str, ...] = ()
    authority_signature: str = dataclasses.field(repr=False, default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "source_line": self.source_line,
            "statement": self.statement,
            "depends_on": list(self.depends_on),
        }


@dataclasses.dataclass(frozen=True)
class TightnessAudit:
    """Parsed output of the delete-only audit."""

    keep_action_ids: tuple[str, ...]
    drop_reasons: dict[str, str]
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    cached: bool = False


@dataclasses.dataclass
class SufficiencyTightnessResult:
    """Both phase artifacts and the mechanically reduced final program."""

    coverage_code: str
    code: str
    actions: tuple[ActionCandidate, ...]
    audit: TightnessAudit
    coverage_result: AgenticCodegenResult
    final_judge_verdicts: list[JudgeVerdict] = dataclasses.field(default_factory=list)

    @property
    def dropped_action_ids(self) -> tuple[str, ...]:
        kept = set(self.audit.keep_action_ids)
        return tuple(a.action_id for a in self.actions if a.action_id not in kept)

    @property
    def prompt_tokens(self) -> int:
        return self.coverage_result.prompt_tokens + self.audit.prompt_tokens

    @property
    def completion_tokens(self) -> int:
        return self.coverage_result.completion_tokens + self.audit.completion_tokens

    @property
    def cost_usd(self) -> float | None:
        audit_cost: float | None = 0.0
        if not self.audit.cached:
            audit_cost = _cost(
                self.coverage_result.model,
                self.audit.prompt_tokens,
                self.audit.completion_tokens,
            )
        if self.coverage_result.cost_usd is None or audit_cost is None:
            return None
        return self.coverage_result.cost_usd + audit_cost

    def metadata(self) -> dict[str, Any]:
        return {
            "strategy": "sufficiency-tightness",
            "coverage_source_sha256": _sha256(self.coverage_code),
            "final_source_sha256": _sha256(self.code),
            "coverage_action_ids": [a.action_id for a in self.actions],
            "kept_action_ids": list(self.audit.keep_action_ids),
            "dropped_action_ids": list(self.dropped_action_ids),
            "drop_reasons": dict(self.audit.drop_reasons),
            "coverage_attempts": self.coverage_result.attempts,
            "audit_attempts": self.audit.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "cached": self.coverage_result.cached and self.audit.cached,
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_docs_sha256(tools: list[ToolDoc]) -> str:
    payload = [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "returns": tool.returns,
        }
        for tool in tools
    ]
    return _sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _tool_call_of(stmt: ast.stmt, tool_names: set[str]) -> ast.Call | None:
    node: ast.expr | None = None
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        node = stmt.value
    elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
        node = stmt.value
    if isinstance(node, ast.Call) and call_name(node) in tool_names:
        return node
    return None


def _action_statements(
    func: ast.FunctionDef,
    tool_names: set[str],
) -> list[tuple[str, str, ast.stmt, str]]:
    """Return action IDs/statements/signatures in compiler source order."""
    counts: dict[str, int] = {}
    out: list[tuple[str, str, ast.stmt, str]] = []

    def walk(stmts: list[ast.stmt], context: tuple[str, ...]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                test = ast.dump(stmt.test, include_attributes=False)
                walk(stmt.body, context + (f"if:{test}:body",))
                walk(stmt.orelse, context + (f"if:{test}:else",))
                continue
            if isinstance(stmt, ast.For):
                loop_header = (
                    ast.dump(stmt.target, include_attributes=False)
                    + ":"
                    + ast.dump(stmt.iter, include_attributes=False)
                )
                walk(stmt.body, context + (f"for:{loop_header}",))
                continue
            call = _tool_call_of(stmt, tool_names)
            if call is None:
                continue
            tool = call_name(call)
            assert tool is not None
            index = counts.get(tool, 0)
            counts[tool] = index + 1
            out.append(
                (
                    f"{tool}#{index}",
                    tool,
                    stmt,
                    _authority_signature(stmt, context),
                )
            )

    walk(func.body, ())
    return out


def _build_actions(prepared: PreparedTask, tool_names: set[str]) -> tuple[ActionCandidate, ...]:
    statements = {
        action_id: (tool, stmt, signature)
        for action_id, tool, stmt, signature in _action_statements(
            prepared.func, tool_names
        )
    }
    actions: list[ActionCandidate] = []
    for step in prepared.execution_plan.steps:
        tool, stmt, signature = statements[step.key]
        actions.append(
            ActionCandidate(
                action_id=step.key,
                tool=tool,
                source_line=step.source_line,
                statement=ast.unparse(stmt),
                depends_on=step.depends_on_steps,
                authority_signature=signature,
            )
        )
    return tuple(actions)


def _audit_user_prompt(
    task: str,
    tools: list[ToolDoc],
    coverage_code: str,
    actions: tuple[ActionCandidate, ...],
) -> str:
    catalog = json.dumps(
        [action.to_dict() for action in actions],
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        build_user_prompt(task, tools)
        + "\n\nCOVERAGE PROGRAM:\n"
        + coverage_code
        + "\n\nIMMUTABLE ACTION CATALOG:\n"
        + catalog
        + "\n\nReturn the delete-only audit JSON."
    )


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _strip_fences(text.strip())
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SufficiencyTightnessError(f"audit response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SufficiencyTightnessError("audit response must be a JSON object")
    return parsed


def _parse_audit(
    raw: str,
    actions: tuple[ActionCandidate, ...],
) -> tuple[tuple[str, ...], dict[str, str]]:
    parsed = _extract_json(raw)
    # ``keep``/``drop`` are accepted as a compact compatibility form for
    # offline experiments. The stored/canonical form always uses the explicit
    # field names.
    keep_raw = parsed.get("keep_action_ids", parsed.get("keep"))
    drop_raw = parsed.get("drop_reasons", parsed.get("drop", {}))
    if not isinstance(keep_raw, list) or not all(isinstance(v, str) for v in keep_raw):
        raise SufficiencyTightnessError("audit keep_action_ids must be a list of strings")
    known = {action.action_id for action in actions}
    keep_set = set(keep_raw)
    unknown = sorted(keep_set - known)
    if unknown:
        raise SufficiencyTightnessError(f"audit referenced unknown action IDs: {unknown}")
    if not keep_set:
        raise SufficiencyTightnessError("audit removed every tool action")

    if isinstance(drop_raw, list):
        drop_reasons = {str(action_id): "auditor marked as excess" for action_id in drop_raw}
    elif isinstance(drop_raw, dict):
        drop_reasons = {
            str(action_id): str(reason)
            for action_id, reason in drop_raw.items()
            if str(action_id) in known and str(action_id) not in keep_set
        }
    else:
        raise SufficiencyTightnessError("audit drop_reasons must be an object or list")

    ordered_keep = tuple(
        action.action_id for action in actions if action.action_id in keep_set
    )
    return ordered_keep, drop_reasons


def _validate_dependency_closure(
    keep_action_ids: tuple[str, ...],
    actions: tuple[ActionCandidate, ...],
) -> None:
    keep = set(keep_action_ids)
    missing: dict[str, list[str]] = {}
    for action in actions:
        if action.action_id not in keep:
            continue
        absent = sorted(set(action.depends_on) - keep)
        if absent:
            missing[action.action_id] = absent
    if missing:
        raise SufficiencyTightnessError(
            "audit dropped required data/control dependencies: "
            + json.dumps(missing, sort_keys=True)
        )


def _reduce_to_actions(
    coverage_code: str,
    tool_names: set[str],
    keep_action_ids: tuple[str, ...],
) -> str:
    """Delete unkept tool-call statements without rewriting any kept node."""
    func = ast.parse(coverage_code).body[0]
    assert isinstance(func, ast.FunctionDef)
    keep = set(keep_action_ids)
    counts: dict[str, int] = {}

    def clean(stmts: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                stmt.body = clean(stmt.body)
                stmt.orelse = clean(stmt.orelse) if stmt.orelse else []
                if not stmt.body:
                    stmt.body = [ast.copy_location(ast.Pass(), stmt)]
                out.append(stmt)
                continue
            if isinstance(stmt, ast.For):
                stmt.body = clean(stmt.body)
                if not stmt.body:
                    stmt.body = [ast.copy_location(ast.Pass(), stmt)]
                out.append(stmt)
                continue
            call = _tool_call_of(stmt, tool_names)
            if call is None:
                out.append(stmt)
                continue
            tool = call_name(call)
            assert tool is not None
            index = counts.get(tool, 0)
            counts[tool] = index + 1
            if f"{tool}#{index}" in keep:
                out.append(stmt)
        return out

    func.body = clean(func.body) or [ast.Pass()]
    ast.fix_missing_locations(func)
    return ast.unparse(ast.Module(body=[func], type_ignores=[]))


def _authority_signatures(
    code: str,
    tool_names: set[str],
) -> collections.Counter[str]:
    """Canonical calls plus their unchanged guard/loop context."""
    func = ast.parse(code).body[0]
    assert isinstance(func, ast.FunctionDef)
    signatures: collections.Counter[str] = collections.Counter()

    def walk(stmts: list[ast.stmt], context: tuple[str, ...]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                test = ast.dump(stmt.test, include_attributes=False)
                walk(stmt.body, context + (f"if:{test}:body",))
                walk(stmt.orelse, context + (f"if:{test}:else",))
                continue
            if isinstance(stmt, ast.For):
                # The body itself is excluded from the context dump; otherwise
                # deleting a sibling action would change every retained
                # signature in the same loop.
                loop_header = (
                    ast.dump(stmt.target, include_attributes=False)
                    + ":"
                    + ast.dump(stmt.iter, include_attributes=False)
                )
                walk(stmt.body, context + (f"for:{loop_header}",))
                continue
            if _tool_call_of(stmt, tool_names) is None:
                continue
            signature = _authority_signature(stmt, context)
            signatures[signature] += 1

    walk(func.body, ())
    return signatures


def _authority_signature(stmt: ast.stmt, context: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "context": context,
            "statement": ast.dump(stmt, include_attributes=False),
        },
        sort_keys=True,
    )


def _assert_delete_only(
    coverage_code: str,
    final_code: str,
    tool_names: set[str],
) -> None:
    coverage = _authority_signatures(coverage_code, tool_names)
    final = _authority_signatures(final_code, tool_names)
    introduced = final - coverage
    if introduced:
        raise SufficiencyTightnessError(
            "tightness phase introduced or broadened authority instead of "
            "deleting actions"
        )


def _assert_exact_retained_actions(
    final_code: str,
    tool_names: set[str],
    keep_action_ids: tuple[str, ...],
    actions: tuple[ActionCandidate, ...],
) -> None:
    keep = set(keep_action_ids)
    expected = collections.Counter(
        action.authority_signature
        for action in actions
        if action.action_id in keep
    )
    actual = _authority_signatures(final_code, tool_names)
    if actual != expected:
        raise SufficiencyTightnessError(
            "reduced program does not exactly match the auditor's retained "
            "action IDs"
        )


def _coverage_cache_path(cache_path: Path | None) -> Path | None:
    return cache_path.with_suffix(".coverage.py") if cache_path is not None else None


def _audit_cache_path(cache_path: Path | None) -> Path | None:
    return cache_path.with_suffix(".tightness.json") if cache_path is not None else None


def _read_audit_cache(
    path: Path | None,
    *,
    task: str,
    tools: list[ToolDoc],
    coverage_code: str,
    model: str,
    actions: tuple[ActionCandidate, ...],
    enable_judge: bool,
    judge_model: str,
    max_retries: int,
) -> TightnessAudit | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    expected = {
        "task_sha256": _sha256(task),
        "tools_sha256": _tool_docs_sha256(tools),
        "coverage_sha256": _sha256(coverage_code),
        "model": model,
        "phase_version": _PHASE_VERSION,
        "enable_judge": enable_judge,
        "judge_model": judge_model,
        "max_retries": max_retries,
        "audit_system_sha256": _sha256(_TIGHTNESS_SYSTEM),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return None
    try:
        keep, reasons = _parse_audit(json.dumps(payload["audit"]), actions)
        _validate_dependency_closure(keep, actions)
    except (KeyError, SufficiencyTightnessError):
        return None
    cached_final = _reduce_to_actions(
        coverage_code, {tool.name for tool in tools}, keep
    )
    if payload.get("final_sha256") != _sha256(cached_final):
        return None
    return TightnessAudit(
        keep_action_ids=keep,
        drop_reasons=reasons,
        attempts=int(payload.get("attempts", 1)),
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
        cached=True,
    )


def _write_audit_cache(
    path: Path,
    *,
    task: str,
    tools: list[ToolDoc],
    coverage_code: str,
    model: str,
    audit: TightnessAudit,
    enable_judge: bool,
    judge_model: str,
    max_retries: int,
    final_code: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_sha256": _sha256(task),
                "tools_sha256": _tool_docs_sha256(tools),
                "coverage_sha256": _sha256(coverage_code),
                "final_sha256": _sha256(final_code),
                "model": model,
                "phase_version": _PHASE_VERSION,
                "enable_judge": enable_judge,
                "judge_model": judge_model,
                "max_retries": max_retries,
                "audit_system_sha256": _sha256(_TIGHTNESS_SYSTEM),
                "audit": {
                    "keep_action_ids": list(audit.keep_action_ids),
                    "drop_reasons": audit.drop_reasons,
                },
                "attempts": audit.attempts,
                "prompt_tokens": audit.prompt_tokens,
                "completion_tokens": audit.completion_tokens,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def generate_sufficiency_tightness(
    task: str,
    tools: list[ToolDoc],
    *,
    tool_signer: dict[str, str] | None = None,
    model: str = "gpt-4.1",
    max_retries: int = 3,
    cache_path: Path | None = None,
    client: Any | None = None,
    enable_judge: bool = True,
    judge_model: str | None = None,
    judge_client: Any | None = None,
    precheck_policy: PrecheckPolicy | None = None,
    executor: Any | None = None,
) -> SufficiencyTightnessResult:
    """Generate a coverage plan, then reduce it through an immutable ID audit."""
    generation_client = _get_generation_client(model, client)
    coverage_result = generate_code_with_self_repair(
        task,
        tools,
        model=model,
        max_retries=max_retries,
        cache_path=_coverage_cache_path(cache_path),
        client=generation_client,
        enable_judge=False,
        precheck_policy=precheck_policy,
        executor=executor,
        initial_system_prompt=SYSTEM_PROMPT + "\n\n" + _COVERAGE_INSTRUCTION,
    )
    tool_names = {tool.name for tool in tools}
    try:
        coverage_prepared = prepare(
            coverage_result.code,
            tool_names,
            tool_signer,
        )
    except Exception as exc:  # noqa: BLE001
        raise SufficiencyTightnessError(
            f"coverage phase did not compile: {type(exc).__name__}: {exc}"
        ) from exc
    if not coverage_prepared.rules:
        raise SufficiencyTightnessError("coverage phase produced no tool actions")

    # From this boundary onward use only the compiler's canonical source.
    # ``prepare`` may normalize call-as-argument and other safe shorthand into
    # additional statement-level actions. Auditing/reducing the raw model text
    # while cataloguing the normalized AST would create an ID mismatch.
    coverage_code = coverage_prepared.source
    actions = _build_actions(coverage_prepared, tool_names)
    if not actions:
        raise SufficiencyTightnessError("coverage phase produced no auditable actions")

    resolved_judge_model = judge_model or model or DEFAULT_JUDGE_MODEL
    cached_audit = _read_audit_cache(
        _audit_cache_path(cache_path),
        task=task,
        tools=tools,
        coverage_code=coverage_code,
        model=model,
        actions=actions,
        enable_judge=enable_judge,
        judge_model=resolved_judge_model,
        max_retries=max_retries,
    )
    if cached_audit is not None:
        final_code = _reduce_to_actions(
            coverage_code, tool_names, cached_audit.keep_action_ids
        )
        _assert_delete_only(coverage_code, final_code, tool_names)
        _assert_exact_retained_actions(
            final_code,
            tool_names,
            cached_audit.keep_action_ids,
            actions,
        )
        violations = precheck_code(task, final_code, tools, policy=precheck_policy)
        if violations:
            raise SufficiencyTightnessError(
                "cached reduced plan failed deterministic prechecks: "
                + "; ".join(violations)
            )
        try:
            final_prepared = prepare(final_code, tool_names, tool_signer)
        except Exception as exc:  # noqa: BLE001
            raise SufficiencyTightnessError(
                f"cached reduced plan did not compile: {type(exc).__name__}: {exc}"
            ) from exc
        if not final_prepared.rules:
            raise SufficiencyTightnessError(
                "cached reduced plan authorizes no tool calls"
            )
        if executor is not None:
            runtime_error = executor(final_code)
            if runtime_error:
                raise SufficiencyTightnessError(
                    "cached reduced plan crashed in the runtime probe: "
                    f"{runtime_error}"
                )
        return SufficiencyTightnessResult(
            coverage_code=coverage_code,
            code=final_code,
            actions=actions,
            audit=cached_audit,
            coverage_result=coverage_result,
        )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _TIGHTNESS_SYSTEM},
        {
            "role": "user",
            "content": _audit_user_prompt(task, tools, coverage_code, actions),
        },
    ]
    total_prompt_tokens = 0
    total_completion_tokens = 0
    final_judge_verdicts: list[JudgeVerdict] = []
    if enable_judge and judge_client is None:
        judge_client = (
            generation_client
            if resolved_judge_model == model
            else _get_judge_client(resolved_judge_model, generation_client)
        )
    last_problem = "tightness audit did not run"

    for attempt in range(1, max_retries + 2):
        raw, prompt_tokens, completion_tokens = _call_generator(
            generation_client, model, messages
        )
        total_prompt_tokens += prompt_tokens
        total_completion_tokens += completion_tokens
        try:
            keep, drop_reasons = _parse_audit(raw, actions)
            _validate_dependency_closure(keep, actions)
            final_code = _reduce_to_actions(coverage_code, tool_names, keep)
            _assert_delete_only(coverage_code, final_code, tool_names)
            _assert_exact_retained_actions(final_code, tool_names, keep, actions)
            violations = precheck_code(
                task, final_code, tools, policy=precheck_policy
            )
            if violations:
                raise SufficiencyTightnessError(
                    "reduced plan failed deterministic prechecks: "
                    + "; ".join(violations)
                )
            final_prepared = prepare(final_code, tool_names, tool_signer)
            if not final_prepared.rules:
                raise SufficiencyTightnessError("reduced plan authorizes no tool calls")

            if executor is not None:
                runtime_error = executor(final_code)
                if runtime_error:
                    raise SufficiencyTightnessError(
                        f"reduced plan crashed in the runtime probe: {runtime_error}"
                    )

            if enable_judge:
                intent_ok, issues = _judge_intent(
                    task,
                    final_code,
                    resolved_judge_model,
                    judge_client,
                )
                final_judge_verdicts.append(
                    JudgeVerdict(
                        attempt=attempt,
                        judge_model=resolved_judge_model,
                        intent_captured=intent_ok,
                        issues=list(issues),
                    )
                )
                if not intent_ok:
                    raise SufficiencyTightnessError(
                        "final intent review failed: "
                        + ("; ".join(issues) if issues else "no issue supplied")
                    )
        except Exception as exc:  # noqa: BLE001 -- audit failures are retryable
            last_problem = f"{type(exc).__name__}: {exc}"
            if attempt > max_retries:
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": _AUDIT_REPAIR.format(problem=last_problem),
                }
            )
            continue

        audit = TightnessAudit(
            keep_action_ids=keep,
            drop_reasons=drop_reasons,
            attempts=attempt,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cached=False,
        )
        audit_path = _audit_cache_path(cache_path)
        if audit_path is not None:
            _write_audit_cache(
                audit_path,
                task=task,
                tools=tools,
                coverage_code=coverage_code,
                model=model,
                audit=audit,
                enable_judge=enable_judge,
                judge_model=resolved_judge_model,
                max_retries=max_retries,
                final_code=final_code,
            )
        return SufficiencyTightnessResult(
            coverage_code=coverage_code,
            code=final_code,
            actions=actions,
            audit=audit,
            coverage_result=coverage_result,
            final_judge_verdicts=final_judge_verdicts,
        )

    raise SufficiencyTightnessError(
        f"tightness phase exhausted its retry budget: {last_problem}"
    )
