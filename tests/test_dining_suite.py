"""The dining suite (structured-native, Option B) is solvable in-grammar and
its forced injections are all denied (FN=0) -- offline, no API key."""

from __future__ import annotations

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.dining import build_suite
from benchmarks.schema_scope import STRUCTURED, TEXT_BLOB, classify_return


def _prepared(task, suite):
    return prepare(task.reference_code, suite.tool_names(), suite.tool_signer())


def test_every_task_ships_reference_code():
    suite = build_suite()
    assert suite.tasks
    assert all(t.reference_code for t in suite.tasks)


def test_reference_code_is_valid_and_executes_cleanly():
    suite = build_suite()
    for task in suite.tasks:
        prepared = _prepared(task, suite)
        assert prepared.rules, task.id
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
        report = execute_generated_code(
            prepared.source, enf, suite.tool_params(),
            suite.tool_executor_factory(suite.make_env()),
        )
        # The structured max()/min()-over-a-collection plan runs without a crash
        # (the shape AgentDojo travel could not express with its string returns).
        assert not report.crashed, task.id
        assert report.events, task.id


def test_all_forced_injections_are_denied():
    suite = build_suite()
    for task in suite.tasks:
        prepared = _prepared(task, suite)
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
        execute_generated_code(
            prepared.source, enf, suite.tool_params(),
            suite.tool_executor_factory(suite.make_env()),
        )
        for inj in task.forced_injections:
            assert not check_injection(enf, inj.tool, inj.args).permit, f"{task.id}:{inj.tool}"


def test_all_tool_returns_are_structured():
    # The product-surface premise: every tool returns a typed object / list of
    # objects, never an unstructured text blob (unlike AgentDojo travel).
    suite = build_suite()
    for name, spec in suite.tools.items():
        cat = classify_return(name, spec.doc.returns)
        assert cat == STRUCTURED, f"{name} -> {cat} ({spec.doc.returns})"
        assert cat != TEXT_BLOB
