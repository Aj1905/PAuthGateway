"""Canonical evaluation-metric vocabulary for every eval in this package.

One UPPER_SNAKE_CASE name per metric, chosen so the name says what it measures.
Each eval imports the names it reports from here (no ad-hoc keys), and the
``METRICS`` registry below documents every metric and which eval produces it.

The primary funnel does not put under-authorization under "availability" and
over-authorization under "security". They are the two error directions of one
REFERENCE_FIDELITY comparison. Feasibility, policy build, runtime reliability,
plan/policy conformance, end-state outcome, cost, and labelled attack probes are
reported separately.

Important scope boundary: the current benchmark compares a permitted concrete
trace with a finite reference trace. It does *not* enumerate the compiled
policy's history-dependent authorization relation, so it does not claim to
measure POLICY_OVER_GRANT / POLICY_UNDER_GRANT / POLICY_EXACT_GRANT.
"""

from __future__ import annotations

# --- legacy plan/enforcement diagnostics -----------------------------------
OVER_AUTHORIZATION_ACCEPTS = "OVER_AUTHORIZATION_ACCEPTS"   # plan layer: a prompt that should be rejected was accepted (or accepted code calls a forbidden tool)
PERMITTED_INJECTIONS = "PERMITTED_INJECTIONS"               # runtime: a forced injection / off-plan call was PERMITTED (paper FN)
UNHELD_DANGEROUS_FLOWS = "UNHELD_DANGEROUS_FLOWS"           # confirmation gate: an untrusted value reached a control operand but was NOT held (grill FN)
BLOCKED_INJECTIONS = "BLOCKED_INJECTIONS"                   # forced injections the gateway did NOT permit (defensive wins)
TOTAL_INJECTIONS = "TOTAL_INJECTIONS"                       # forced injections attempted (denominator for BLOCKED_INJECTIONS)

OVER_REJECTIONS = "OVER_REJECTIONS"                         # a legitimate prompt/call was rejected
ACCEPTANCE_RATE = "ACCEPTANCE_RATE"                         # fraction of tasks whose plan was accepted (the Planner -> rules)
OVER_GATED_SAFE_FLOWS = "OVER_GATED_SAFE_FLOWS"             # confirmation gate: a safe flow (trusted/content/constant) was needlessly held (grill FP)
SUITE_FILTER_RECALL = "SUITE_FILTER_RECALL"                 # fraction of prompts whose needed suite the filter retained
DROPPED_NEEDED_SUITES = "DROPPED_NEEDED_SUITES"             # count of prompts whose needed suite the filter dropped

# --- COST: machine time + human interruption ------------------------------
TOTAL_TOOL_CALLS = "TOTAL_TOOL_CALLS"                       # every tool call routed through the gateway
SIDE_EFFECTING_CALLS = "SIDE_EFFECTING_CALLS"              # of those, the writes/sinks (the only calls a confirmation can fire on)
UPFRONT_CONFIRMATIONS = "UPFRONT_CONFIRMATIONS"           # confirmations hoistable to the initial grill (fire before any write commits) -- batched into plan approval
MIDRUN_INTERRUPTIONS = "MIDRUN_INTERRUPTIONS"             # confirmations firing after a write already ran -- the flow-breaking, real autonomy friction
HUMAN_APPROVALS = "HUMAN_APPROVALS"                        # human approvals issued to resolve confirmations
ADDITIONAL_COST_ROUNDTRIPS = "ADDITIONAL_COST_ROUNDTRIPS"  # extra tool roundtrips the gating adds
ENFORCEMENT_US_PER_CALL = "ENFORCEMENT_US_PER_CALL"       # the gateway's per-call machinery overhead (gateway vs no-gateway)
LATENCY_MEAN_US = "LATENCY_MEAN_US"                        # gateway in->out per prompt (grill corpus)
PLAN_SETUP_MS = "PLAN_SETUP_MS"                            # one-time prompt -> rules cost per task

# --- CORRECTNESS / integrity diagnostics ----------------------------------
VALUE_LEAK_COUNT = "VALUE_LEAK_COUNT"                      # poisoned/untrusted value handed back to the agent (MUST be 0)
OFF_INTENT_COUNT = "OFF_INTENT_COUNT"                     # accepted plans whose code called the wrong tools (missing/spurious)
PLAN_REJECTED = "PLAN_REJECTED"                            # plans denied at the plan layer

# --- the Planner cost --------------------------------------------------------------
PROMPT_TOKENS = "PROMPT_TOKENS"
COMPLETION_TOKENS = "COMPLETION_TOKENS"

# --- PAuth evaluation funnel (per-task; see eval/gates.py) -----------------
# Preconditions / build.
FEASIBILITY_EXPRESSIBLE = "FEASIBILITY_EXPRESSIBLE"
SYNTHESIS_POLICY_COMPILED = "SYNTHESIS_POLICY_COMPILED"

# Diagnostics with deliberately narrow claims. Runtime crash probing is
# permissive (no Enforcer), while conformance is measured only on the one
# concrete generated-plan trace executed by the benchmark.
RELIABILITY_RUNTIME_CRASH_FREE = "RELIABILITY_RUNTIME_CRASH_FREE"
CONFORMANCE_PLAN_TRACE_PERMITTED = "CONFORMANCE_PLAN_TRACE_PERMITTED"

# One reference-fidelity plane. Both halves use the same tool+control-operand
# matcher; EXACT is their conjunction. These are about the permitted concrete
# trace, not the complete compiled-policy relation.
GT_NO_MISSING_CALLS = "GT_NO_MISSING_CALLS"
GT_NO_EXCESS_CALLS = "GT_NO_EXCESS_CALLS"
GT_EXACT_AUTHORIZATION = "GT_EXACT_AUTHORIZATION"

OUTCOME_TASK_COMPLETED = "OUTCOME_TASK_COMPLETED"  # post-state utility; reported apart
COST_TOOL_CALLS = "COST_TOOL_CALLS"               # permitted calls per compiled plan

# --- AUXILIARY diagnostics --------------------------------------------------
# Leading underscore is avoided -- in Python that means "private", not
# "auxiliary"; AUX_ says auxiliary without clashing with that convention.
AUX_INJECTIONS_DENIED = "AUX_INJECTIONS_DENIED"  # labelled forced-attack calls denied;
                                                 # stress-test evidence, not a universal guarantee
AUX_TAU_REWARD = "AUX_TAU_REWARD"               # tau-bench native success (final DB state +
                                                # required outputs); fills TASK_COMPLETED where
                                                # a framework ships no utility()
AUX_INJEC_ATTACK_CATEGORY = "AUX_INJEC_ATTACK_CATEGORY"  # InjecAgent direct-harm vs data-stealing
                                                # split of AUX_INJECTIONS_DENIED


# name -> (property, one-line description, produced-by evals)
METRICS: dict[str, tuple[str, str, str]] = {
    OVER_AUTHORIZATION_ACCEPTS: ("PLAN_FIDELITY", "bad prompt accepted at the plan layer", "fpfn"),
    PERMITTED_INJECTIONS: ("ADVERSARIAL_ROBUSTNESS", "forced injection / off-plan call permitted at runtime", "fpfn, unexpected_attacks"),
    UNHELD_DANGEROUS_FLOWS: ("ENFORCEMENT_FIDELITY", "untrusted->control call not held by the gate", "grill_eval, grill_scenario"),
    BLOCKED_INJECTIONS: ("ADVERSARIAL_ROBUSTNESS", "forced injections the gateway blocked", "toolcall_eval, e2e_eval"),
    TOTAL_INJECTIONS: ("ADVERSARIAL_ROBUSTNESS", "forced injections attempted", "toolcall_eval, e2e_eval"),
    OVER_REJECTIONS: ("AUTHORIZATION_FIDELITY", "legitimate prompt/call wrongly denied", "fpfn"),
    ACCEPTANCE_RATE: ("SYNTHESIS", "fraction of tasks whose plan was accepted", "fpfn"),
    OVER_GATED_SAFE_FLOWS: ("ENFORCEMENT_FIDELITY", "safe flow needlessly held for confirmation", "grill_eval"),
    SUITE_FILTER_RECALL: ("ROUTING", "fraction of prompts whose needed suite was kept", "filter_recall"),
    DROPPED_NEEDED_SUITES: ("ROUTING", "prompts whose needed suite was dropped", "filter_recall"),
    TOTAL_TOOL_CALLS: ("COST", "tool calls routed through the gateway", "toolcall_eval, e2e_eval"),
    SIDE_EFFECTING_CALLS: ("COST", "writes/sinks among the calls", "toolcall_eval, e2e_eval"),
    UPFRONT_CONFIRMATIONS: ("COST", "confirmations hoistable to the initial grill", "toolcall_eval, e2e_eval"),
    MIDRUN_INTERRUPTIONS: ("COST", "confirmations that interrupt mid-execution", "toolcall_eval, e2e_eval"),
    HUMAN_APPROVALS: ("COST", "human approvals issued", "grill_eval"),
    ADDITIONAL_COST_ROUNDTRIPS: ("COST", "extra tool roundtrips the gating adds", "grill_eval"),
    ENFORCEMENT_US_PER_CALL: ("COST", "gateway per-call machinery overhead", "toolcall_eval, e2e_eval"),
    LATENCY_MEAN_US: ("COST", "gateway in->out per prompt", "grill_eval"),
    PLAN_SETUP_MS: ("COST", "one-time prompt->rules cost per task", "e2e_eval"),
    PROMPT_TOKENS: ("COST", "the Planner prompt tokens", "fpfn"),
    COMPLETION_TOKENS: ("COST", "the Planner completion tokens", "fpfn"),
    VALUE_LEAK_COUNT: ("INTEGRITY", "untrusted value handed back to the agent", "grill_eval"),
    OFF_INTENT_COUNT: ("PLAN_FIDELITY", "accepted plan called the wrong tools", "(retired: freeform harness removed 2026-08-02)"),
    PLAN_REJECTED: ("SYNTHESIS", "plans denied at the plan layer", "grill_eval, fpfn"),
    # Primary PAuth funnel. Under/excess are one reference-fidelity comparison.
    FEASIBILITY_EXPRESSIBLE: (
        "FEASIBILITY", "control operands representable by the restricted mechanisms", "gates"
    ),
    SYNTHESIS_POLICY_COMPILED: (
        "SYNTHESIS", "generated code validated and compiled into policy rules", "gates, tau, injecagent"
    ),
    RELIABILITY_RUNTIME_CRASH_FREE: (
        "RELIABILITY", "generated plan completed a permissive mock run without a code crash", "gates, tau, injecagent"
    ),
    CONFORMANCE_PLAN_TRACE_PERMITTED: (
        "CONFORMANCE", "the compiled policy denied no call on the observed generated-plan trace", "gates, tau, injecagent"
    ),
    GT_NO_MISSING_CALLS: (
        "REFERENCE_FIDELITY", "no required reference call missing from the permitted trace", "gates, tau"
    ),
    GT_NO_EXCESS_CALLS: (
        "REFERENCE_FIDELITY", "no non-reference call present in the permitted trace", "gates, tau"
    ),
    GT_EXACT_AUTHORIZATION: (
        "REFERENCE_FIDELITY", "required-call coverage and no-excess both pass", "gates, tau"
    ),
    OUTCOME_TASK_COMPLETED: ("OUTCOME", "post-state utility reached in the plan simulation", "gates"),
    AUX_INJECTIONS_DENIED: ("ADVERSARIAL_ROBUSTNESS", "labelled forced-attack calls denied; tested-set stress result", "gates, tau, injecagent"),
    COST_TOOL_CALLS: ("COST", "permitted tool calls per compiled plan", "gates, tau, injecagent"),
    # framework-specific auxiliaries (AUX_ prefix)
    AUX_TAU_REWARD: ("OUTCOME", "tau-bench native success (DB state + outputs)", "tau [aux]"),
    AUX_INJEC_ATTACK_CATEGORY: ("ADVERSARIAL_ROBUSTNESS", "InjecAgent direct-harm vs data-steal split", "injecagent [aux]"),
}


if __name__ == "__main__":  # print the glossary grouped by property
    properties = (
        "FEASIBILITY", "SYNTHESIS", "PLAN_FIDELITY", "RELIABILITY",
        "CONFORMANCE", "AUTHORIZATION_FIDELITY", "ENFORCEMENT_FIDELITY", "REFERENCE_FIDELITY",
        "OUTCOME", "ADVERSARIAL_ROBUSTNESS", "ROUTING", "INTEGRITY", "COST",
    )
    for prop in properties:
        print(f"\n== {prop} ==")
        for name, (p, desc, by) in METRICS.items():
            if p == prop:
                print(f"  {name:<26} {desc}  [{by}]")
