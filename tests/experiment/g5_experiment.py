"""Search for a G5-lifting intervention that keeps FN=0.

Per task, compare the cached baseline against a regeneration under an intervention
(synced prompt + optional structure_text exposure). Reports G2 (DSL-valid),
G5 (utility), and GS (forced injections all denied = FN=0) for each, so a lift is
only credited if soundness holds. Regenerated plans are cached to scratch so
re-measuring costs no API. Needs OPENAI_API_KEY.

Usage: python -m tests.experiment.g5_experiment <suite> [--structuring]
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.forced_injection import generate_for_task
from benchmarks.structured_read import augment_with_structuring
from gateway.planning.agentic_planner import generate_code_with_self_repair
from pauth import prepare
from pauth.enforcer import Enforcer, check_injection
from pauth.tool_executor import execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.grammar_validator import DSLRejectionError

CACHE = Path("tests/experiment/cache")
SCRATCH = Path("tests/experiment/g5_scratch")


def _measure(suite, adj, ut, code) -> dict:
    """G2/G4/G5/GS for one plan on one suite."""
    out = {"g2": False, "g5": False, "gs": True}
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except DSLRejectionError:
        return out
    out["g2"] = True
    env = suite.make_env()
    pre = copy.deepcopy(env)
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(env))
    if rep.crashed is None and not rep.denied:
        try:
            out["g5"] = bool(ut.utility("", pre, env))
        except Exception:  # noqa: BLE001
            out["g5"] = False
    # GS: every forced injection must be denied (FN=0)
    for c in generate_for_task(adj, ut, suite.tool_params(), suite.make_env):
        if check_injection(enf, c.tool, list(c.args)).permit:
            out["gs"] = False
            break
    return out


def main() -> None:
    suite_name = sys.argv[1] if len(sys.argv) > 1 else "banking"
    use_struct = "--structuring" in sys.argv
    adj = get_suites("v1")[suite_name]
    base_suite = load_suite(suite_name)
    gen_suite = augment_with_structuring(base_suite) if use_struct else base_suite
    docs = gen_suite.tool_docs()
    tag = "struct" if use_struct else "plain"
    scratch = SCRATCH / f"{suite_name}_{tag}"
    scratch.mkdir(parents=True, exist_ok=True)

    agg = {k: [0, 0, 0] for k in ("base", "new")}  # [g2, g5, gs] counts
    n = 0
    for tid in sorted(adj.user_tasks):
        ut = adj.user_tasks[tid]
        n += 1
        cached = (CACHE / suite_name / f"{tid}.py")
        base_code = cached.read_text() if cached.exists() else "def run():\n    pass\n"
        b = _measure(base_suite, adj, ut, base_code)

        pf = scratch / f"{tid}.py"
        if pf.exists():
            new_code = pf.read_text()
        else:
            res = generate_code_with_self_repair(
                ut.PROMPT, docs, model="gpt-4.1", max_retries=3, enable_judge=False)
            new_code = res.code
            pf.write_text(new_code)
        nw = _measure(gen_suite, adj, ut, new_code)

        for key, m in (("base", b), ("new", nw)):
            agg[key][0] += m["g2"]; agg[key][1] += m["g5"]; agg[key][2] += m["gs"]
        flags = []
        if nw["g5"] and not b["g5"]:
            flags.append("G5+")
        if not nw["gs"]:
            flags.append("FN!")
        st = "st" if "structure_text" in new_code else "  "
        print(f"{tid:16} base g2/g5/gs={int(b['g2'])}{int(b['g5'])}{int(b['gs'])}  "
              f"new={int(nw['g2'])}{int(nw['g5'])}{int(nw['gs'])} {st} {' '.join(flags)}")

    print(f"\n=== {suite_name} ({tag}), {n} tasks ===")
    for key in ("base", "new"):
        g2, g5, gs = agg[key]
        print(f"  {key:5} G2={g2}/{n}  G5={g5}/{n}  GS(FN=0)={gs}/{n}")


if __name__ == "__main__":
    main()
