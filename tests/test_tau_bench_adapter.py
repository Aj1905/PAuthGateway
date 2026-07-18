"""The tau-bench retail adapter: structured returns, ground-truth plans are
grammar-expressible, and off-plan injections are denied. Needs the tau_bench
package (a benchmark dependency); skipped cleanly if it is absent."""

from __future__ import annotations

import pytest

pytest.importorskip("tau_bench")

from pauth import prepare
from pauth.enforcer import Enforcer, check_injection, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing

from benchmarks.tau_bench_adapter import build_suite
from benchmarks.schema_scope import TEXT_BLOB, classify_return


def test_suite_loads_structured_tools_and_tasks():
    suite = build_suite()
    assert len(suite.tools) == 16
    assert len(suite.tasks) > 50
    # tau-bench tools return JSON strings; the adapter must surface STRUCTURE,
    # so no data getter is a text blob.
    for name, spec in suite.tools.items():
        assert classify_return(name, spec.doc.returns) != TEXT_BLOB, name


def test_ground_truth_plans_are_expressible_and_injections_denied():
    suite = build_suite()
    tools, signer, params = suite.tool_names(), suite.tool_signer(), suite.tool_params()
    checked = 0
    for task in suite.tasks[:40]:
        prepared = prepare(task.reference_code, tools, signer)  # must not raise
        assert prepared.rules, task.id
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signer)
        execute_generated_code(
            prepared.source, enf, params, suite.runner_factory(suite.make_env())
        )
        for inj in task.forced_injections:
            assert not check_injection(enf, inj.tool, inj.args).permit, f"{task.id}:{inj.tool}"
            checked += 1
    assert checked > 0
