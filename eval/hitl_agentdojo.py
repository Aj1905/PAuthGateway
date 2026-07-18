"""AgentDojo WITH the confirmation gate in the loop (HITL).

The headless fpfn measures the enforcer only (off-plan injections denied by
construction). This adds the GATE layer on the real AgentDojo suites: for each
cached plan, which side-effecting calls carry an untrusted-derived control
operand (narrow, S15-correct: recipient/amount only -- content operands exempt),
so a human must confirm them? That footprint bounds the gate's availability cost,
and -- run interactively -- lets a human actually answer.

Trust policy is fail-closed (every read untrusted, no own-data exceptions): the
UPPER bound on how much the gate fires. A real deployment trusting own-account
reads gates strictly less. We also report the BROAD footprint (decision operands
included) for contrast.

Run:  .venv/bin/python -m eval.hitl_agentdojo   [--interactive]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar import RestrictedGrammarError
from gateway.runtime.confirmation import (
    PendingConfirmation,
    SourceTrust,
    broad_taint_map,
    is_side_effecting,
    reduction_breakdown,
    static_taint_map,
)
from gateway.runtime.confirmer import CautiousConfirmer, InteractiveConfirmer

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tests" / "experiment" / "cache"
SUITES = ("banking", "slack", "travel", "workspace")


def _plan(suite: str, task_id: str) -> str | None:
    p = CACHE / suite / f"{task_id}.py"
    return p.read_text() if p.exists() else None


def _analyse(suite_name: str, confirmer=None):
    adj = get_suites("v1")[suite_name]
    spec = load_suite(suite_name)
    docs = {n: s.doc for n, s in spec.tools.items()}
    tools, signer, params = spec.tool_names(), spec.tool_signer(), spec.tool_params()
    # fail-closed: everything untrusted -> upper bound on the gate footprint.
    st_narrow = SourceTrust.fail_closed()
    st_broad = SourceTrust(default_untrusted=True, confirm_untrusted_decisions=True)

    n = n_sideeffect = n_gate_narrow = n_gate_broad = n_calls_gated = n_judgeable = 0
    for task_id in sorted(adj.user_tasks):
        code = _plan(suite_name, task_id)
        if code is None:
            continue
        try:
            prepared = prepare(code, tools, signer)
        except RestrictedGrammarError:
            continue
        n += 1
        narrow = static_taint_map(code, docs, st_narrow)
        broad = broad_taint_map(code, docs, st_broad)

        env = spec.make_env()
        enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), signer)
        rep = execute_generated_code(prepared.source, enf, params, spec.runner_factory(env))

        se = [e for e in rep.events if e.decision.permit and is_side_effecting(e.tool)]
        if se:
            n_sideeffect += 1
        gated_narrow = [(e, i) for e in se for (t, i) in narrow if t == e.tool and i < len(e.args)]
        gated_broad = [(e, i) for e in se for (t, i) in broad if t == e.tool and i < len(e.args)]
        if gated_narrow:
            n_gate_narrow += 1
        if gated_broad:
            n_gate_broad += 1
        n_calls_gated += len(gated_narrow)

        for e, i in gated_narrow:
            bd = None
            for rule in enf.rules_by_tool.get(e.tool, []):
                bd = reduction_breakdown(rule, i, enf.store)
                if bd:
                    break
            pname = docs[e.tool].parameters[i]["name"] if i < len(docs[e.tool].parameters) else str(i)
            pc = PendingConfirmation(
                f"{task_id}", e.tool, i, pname, e.args[i],
                source=narrow[(e.tool, i)], breakdown=bd,
            )
            if CautiousConfirmer.judgeable(pc):
                n_judgeable += 1
            if confirmer is not None:  # interactive: actually ask
                approved = confirmer.confirm(pc)
                print(f"    {suite_name}/{task_id}: {e.tool}.{pname} "
                      f"{'APPROVED' if approved else 'REJECTED'}")
    return n, n_sideeffect, n_gate_narrow, n_gate_broad, n_calls_gated, n_judgeable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interactive", action="store_true",
                    help="answer each gated control operand yourself on stdin")
    args = ap.parse_args()
    conf = InteractiveConfirmer() if args.interactive else None

    print("AgentDojo WITH the confirmation gate (fail-closed = upper bound)\n")
    hdr = (f"{'suite':<10}{'plans':>7}{'side-eff':>10}{'gated(ctrl)':>13}"
           f"{'#calls':>8}{'judgeable':>11}")
    print(hdr); print("-" * len(hdr))
    tot = [0, 0, 0, 0, 0, 0]
    for name in SUITES:
        r = _analyse(name, conf)
        tot = [a + b for a, b in zip(tot, r)]
        print(f"{name:<10}{r[0]:>7}{r[1]:>10}{r[2]:>13}{r[4]:>8}{r[5]:>11}")
    print("-" * len(hdr))
    print(f"{'ALL':<10}{tot[0]:>7}{tot[1]:>10}{tot[2]:>13}{tot[4]:>8}{tot[5]:>11}")
    n, se, gn, gb, calls, judge = tot
    print(f"\n  gated (control operands, S15-correct): {gn}/{n} plans "
          f"({100*gn/n:.0f}%), {calls} calls need a human")
    print(f"  gated (broad, decisions included) ....: {gb}/{n} plans ({100*gb/n:.0f}%)")
    print(f"  JUDGEABLE (a cautious human can approve): {judge}/{calls} gated calls")
    print(f"    -> a cautious human (rejects what it cannot judge) would BLOCK "
          f"{calls - judge}/{calls} benign calls purely for lack of a breakdown.")
    print("\n  fail-closed = UPPER bound (every read untrusted). The judgeable gap is")
    print("  the availability the gate LOSES to poor UX: bare recipient/amount values")
    print("  with no decomposition. Surfacing provenance/decomposition for them is the")
    print("  concrete UX fix. Run --interactive to answer the gates yourself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
