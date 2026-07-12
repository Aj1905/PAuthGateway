"""The InjecAgent adapter loads the vendored cases and PAuth denies every
injected (off-plan) attacker call -- FN=0 on the indirect-injection scenarios."""

from __future__ import annotations

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing

from benchmarks.injecagent_adapter import build_suite


def test_suite_loads_tools_and_tasks():
    suite = build_suite()
    assert len(suite.tools) > 100          # full toolkit set
    assert suite.tasks                      # vendored case sample
    assert all(t.reference_code for t in suite.tasks)
    assert all(t.forced_injections for t in suite.tasks)


def test_every_injected_attacker_call_is_denied():
    suite = build_suite()
    tools, signer, params = suite.tool_names(), suite.tool_signer(), suite.tool_params()
    checked = 0
    for task in suite.tasks:
        prepared = prepare(task.reference_code, tools, signer)
        assert prepared.rules, task.id
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signer)
        execute_generated_code(
            prepared.source, enf, params, suite.runner_factory(suite.make_env())
        )
        for inj in task.forced_injections:
            # The attacker tool is never in the benign plan -> default-deny.
            assert not check_injection(enf, inj.tool, inj.args).permit, f"{task.id}:{inj.tool}"
            checked += 1
    assert checked > 0
