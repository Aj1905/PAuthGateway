"""Pivotal test: with structure_text in the toolset, does the PLANNER autonomously
route a prose-locked task through it (read -> structure_text -> field -> act) and
COMPLETE it (G5)? Regenerates the banking prose tasks fresh. Needs OPENAI_API_KEY.
"""

from __future__ import annotations

import copy

from agentdojo.task_suite.load_suites import get_suites

from benchmarks.agentdojo_adapter import load_suite
from benchmarks.structured_read import augment_with_structuring
from gateway.planning.agentic_a1 import generate_code_with_self_repair
from pauth import prepare
from pauth.enforcer import Enforcer, execute_generated_code
from pauth.envelope import EnvelopeStore, KeyRing

# banking tasks whose value is prose-locked (HOLLOW/WRONG-wall in ceiling_probe).
TARGETS = ["user_task_0", "user_task_2", "user_task_3", "user_task_6",
           "user_task_11", "user_task_12", "user_task_14"]


def _g5(suite, ut, code) -> tuple[bool, bool, str]:
    """Return (grammar_ok, g5, note)."""
    try:
        prepared = prepare(code, suite.tool_names(), suite.tool_signer())
    except Exception as e:  # noqa: BLE001
        return False, False, f"grammar-reject: {str(e)[:40]}"
    env = suite.make_env()
    pre = copy.deepcopy(env)
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    rep = execute_generated_code(prepared.source, enf, suite.tool_params(),
                                 suite.runner_factory(env))
    if rep.crashed or rep.denied:
        return True, False, f"crashed={bool(rep.crashed)} denied={bool(rep.denied)}"
    try:
        ok = bool(ut.utility("", pre, env))
    except Exception as e:  # noqa: BLE001
        return True, False, f"utility-error: {str(e)[:40]}"
    return True, ok, "utility=True" if ok else "utility=False"


def main() -> None:
    adj = get_suites("v1")["banking"]
    aug = augment_with_structuring(load_suite("banking"))
    docs = aug.tool_docs()
    used = g5 = 0
    for tid in TARGETS:
        ut = adj.user_tasks[tid]
        res = generate_code_with_self_repair(
            ut.PROMPT, docs, model="gpt-4.1", max_retries=3, enable_judge=False)
        code = res.code
        uses_st = "structure_text" in code
        gok, g5ok, note = _g5(aug, ut, code)
        used += uses_st
        g5 += g5ok
        print(f"\n=== banking/{tid} ===")
        print(f"  PROMPT: {ut.PROMPT[:90]}")
        print(f"  uses structure_text: {uses_st} | grammar_ok: {gok} | G5: {g5ok} ({note})")
        if uses_st:
            for ln in code.splitlines():
                print("    " + ln)
    print(f"\n### {used}/{len(TARGETS)} routed through structure_text ; "
          f"{g5}/{len(TARGETS)} completed (G5) ###")


if __name__ == "__main__":
    main()
