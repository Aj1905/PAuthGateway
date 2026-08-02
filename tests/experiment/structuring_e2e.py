"""End-to-end: does the structure_text extraction route actually COMPLETE a
prose-locked task (G5), not just reach the gate? Runs the extraction plan on the
real banking UserTask0 and checks ut.utility(). Deterministic, no API key.
"""

from __future__ import annotations

import copy

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing

# The extraction plan we WISH the Planner produced (hand-written for now).
PLAN = '''\
def run():
    bill = read_file("bill-december-2023.txt")
    view = structure_text(bill)
    ibans = view.ibans
    amounts = view.amounts
    send_money(ibans[0], amounts[0], "Car Rental", "2022-01-01")
'''


def _g5(suite, ut, code) -> tuple[bool, str]:
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except Exception as e:  # noqa: BLE001
        return False, f"grammar-reject: {e}"
    env = suite.make_env()
    pre = copy.deepcopy(env)
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.tool_executor_factory(env))
    if rep.crashed or rep.denied:
        return False, f"crashed={rep.crashed} denied={bool(rep.denied)}"
    try:
        ok = bool(ut.utility("", pre, env))
    except Exception as e:  # noqa: BLE001
        return False, f"utility-error: {e}"
    return ok, "utility=True" if ok else "utility=False"


def main() -> None:
    adj = get_suites("v1")["banking"]
    ut = adj.user_tasks["user_task_0"]
    print("PROMPT:", ut.PROMPT)

    base = load_suite("banking")
    aug = augment_with_structuring(base)

    # baseline: the cached (blob) plan vs the extraction plan
    cached = open("tests/experiment/cache/banking/user_task_0.py").read()
    print("\n-- cached plan (no structuring) --")
    print(cached)
    ok_c, why_c = _g5(base, ut, cached)
    print(f"  G5: {ok_c}  ({why_c})")

    print("\n-- extraction plan (structure_text route) --")
    ok_e, why_e = _g5(aug, ut, PLAN)
    print(f"  G5: {ok_e}  ({why_e})")

    print(f"\n=> structuring flips banking/user_task_0 G5: {ok_c} -> {ok_e}")


if __name__ == "__main__":
    main()
