"""Canonical evaluation-metric vocabulary for every eval in this package.

One UPPER_SNAKE_CASE name per metric, chosen so the name says what it measures.
Each eval imports the names it reports from here (no ad-hoc keys), and the
``METRICS`` registry below documents every metric and which eval produces it.

Metrics are grouped by the property they measure:

* SECURITY     -- did an unauthorized action get through? (lower is better; 0 is the bar)
* AVAILABILITY -- was a legitimate action wrongly blocked? / how much got through
* COST         -- machine time and human interruption the gateway adds
"""

from __future__ import annotations

# --- SECURITY: an unauthorized action got through (must be 0) --------------
OVER_AUTHORIZATION_ACCEPTS = "OVER_AUTHORIZATION_ACCEPTS"   # plan layer: a prompt that should be rejected was accepted (or accepted code calls a forbidden tool)
PERMITTED_INJECTIONS = "PERMITTED_INJECTIONS"               # runtime: a forced injection / off-plan call was PERMITTED (paper FN)
UNHELD_DANGEROUS_FLOWS = "UNHELD_DANGEROUS_FLOWS"           # confirmation gate: an untrusted value reached a control operand but was NOT held (grill FN)
BLOCKED_INJECTIONS = "BLOCKED_INJECTIONS"                   # forced injections the gateway did NOT permit (defensive wins)
TOTAL_INJECTIONS = "TOTAL_INJECTIONS"                       # forced injections attempted (denominator for BLOCKED_INJECTIONS)

# --- AVAILABILITY: a legitimate action was blocked / coverage --------------
OVER_REJECTIONS = "OVER_REJECTIONS"                         # a legitimate prompt/call was denied (recoverable; not a security failure)
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

# --- PAuth gate funnel (per-task; see eval/gates.py) ----------------------
# The PREFIX names the axis (AVAIL / OUTCOME / SEC / COST) and the NUMBER shows
# position in the nested chain, so containment is readable from the name:
#   AVAIL_1_EXPRESSIBLE ⊇ AVAIL_2_PLAN_VALID ⊇ AVAIL_3_RAN_CLEAN ⊇ AVAIL_4_CALLS_MADE
# (PAuth's mandate = the tool CALLS). OUTCOME_* is agent-inclusive and reported
# apart (no number -> not in PAuth's chain). SEC_* / COST_* are orthogonal. These
# are UNIVERSAL: any framework populates the subset its data supports.
AVAIL_1_EXPRESSIBLE = "AVAIL_1_EXPRESSIBLE"       # intent writable in the restricted grammar
AVAIL_2_PLAN_VALID = "AVAIL_2_PLAN_VALID"         # planner produced a grammar-valid plan
AVAIL_3_RAN_CLEAN = "AVAIL_3_RAN_CLEAN"           # ran without a crash or false denial
AVAIL_4_CALLS_MADE = "AVAIL_4_CALLS_MADE"         # deficiency-free: every needed tool call made
OUTCOME_TASK_COMPLETED = "OUTCOME_TASK_COMPLETED"  # goal by intent (utility); AGENT-inclusive
SEC_NO_EXCESS_CALLS = "SEC_NO_EXCESS_CALLS"       # least authority: nothing beyond ground truth
SEC_INJECTIONS_DENIED = "SEC_INJECTIONS_DENIED"   # FN=0 (funnel view of PERMITTED_INJECTIONS)
COST_TOOL_CALLS = "COST_TOOL_CALLS"               # enforced tool calls per task (cost proxy)

# --- framework-specific AUXILIARY metrics (AUX_ prefix; only one framework can
# populate each). Leading underscore is avoided -- in Python that means "private",
# not "auxiliary"; AUX_ says auxiliary without clashing with that convention.
AUX_TAU_REWARD = "AUX_TAU_REWARD"               # tau-bench native success (final DB state +
                                                # required outputs); fills TASK_COMPLETED where
                                                # a framework ships no utility()
AUX_INJEC_ATTACK_CATEGORY = "AUX_INJEC_ATTACK_CATEGORY"  # InjecAgent direct-harm vs data-stealing
                                                # split of the denials (INJECTIONS_DENIED breakdown)


# name -> (property, one-line description, produced-by evals)
METRICS: dict[str, tuple[str, str, str]] = {
    OVER_AUTHORIZATION_ACCEPTS: ("SECURITY", "bad prompt accepted at the plan layer", "fpfn, freeform"),
    PERMITTED_INJECTIONS: ("SECURITY", "forced injection / off-plan call permitted at runtime", "fpfn, l2_replay, unexpected_attacks"),
    UNHELD_DANGEROUS_FLOWS: ("SECURITY", "untrusted->control call not held by the gate", "grill_eval, grill_scenario"),
    BLOCKED_INJECTIONS: ("SECURITY", "forced injections the gateway blocked", "toolcall_eval, e2e_eval"),
    TOTAL_INJECTIONS: ("SECURITY", "forced injections attempted", "toolcall_eval, e2e_eval"),
    OVER_REJECTIONS: ("AVAILABILITY", "legitimate prompt/call wrongly denied", "fpfn, freeform, l2_replay"),
    ACCEPTANCE_RATE: ("AVAILABILITY", "fraction of tasks whose plan was accepted", "fpfn, freeform"),
    OVER_GATED_SAFE_FLOWS: ("AVAILABILITY", "safe flow needlessly held for confirmation", "grill_eval"),
    SUITE_FILTER_RECALL: ("AVAILABILITY", "fraction of prompts whose needed suite was kept", "filter_recall"),
    DROPPED_NEEDED_SUITES: ("AVAILABILITY", "prompts whose needed suite was dropped", "filter_recall"),
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
    VALUE_LEAK_COUNT: ("CORRECTNESS", "untrusted value handed back to the agent", "grill_eval"),
    OFF_INTENT_COUNT: ("CORRECTNESS", "accepted plan called the wrong tools", "freeform"),
    PLAN_REJECTED: ("CORRECTNESS", "plans denied at the plan layer", "grill_eval, fpfn"),
    # PAuth gate funnel (universal; each framework populates its measurable subset).
    # AVAIL_1..4 is a nested chain (⊇); OUTCOME is agent-inclusive, reported apart.
    AVAIL_1_EXPRESSIBLE: ("AVAILABILITY", "intent writable in the restricted grammar", "gates"),
    AVAIL_2_PLAN_VALID: ("AVAILABILITY", "planner produced a grammar-valid plan", "gates, tau, injecagent"),
    AVAIL_3_RAN_CLEAN: ("AVAILABILITY", "ran without a crash or false denial", "gates, tau, injecagent"),
    AVAIL_4_CALLS_MADE: ("AVAILABILITY", "deficiency-free: every needed call made", "gates, tau"),
    OUTCOME_TASK_COMPLETED: ("OUTCOME", "goal reached by intent (utility); agent-inclusive", "gates"),
    SEC_NO_EXCESS_CALLS: ("SECURITY", "least authority: nothing beyond ground truth", "gates, tau"),
    SEC_INJECTIONS_DENIED: ("SECURITY", "FN=0 funnel view (all injections denied)", "gates, tau, injecagent"),
    COST_TOOL_CALLS: ("COST", "enforced tool calls per task (cost proxy)", "gates, tau, injecagent"),
    # framework-specific auxiliaries (AUX_ prefix)
    AUX_TAU_REWARD: ("OUTCOME", "tau-bench native success (DB state + outputs)", "tau [aux]"),
    AUX_INJEC_ATTACK_CATEGORY: ("SECURITY", "InjecAgent direct-harm vs data-steal split", "injecagent [aux]"),
}


if __name__ == "__main__":  # print the glossary grouped by property
    for prop in ("SECURITY", "AVAILABILITY", "COST", "CORRECTNESS"):
        print(f"\n== {prop} ==")
        for name, (p, desc, by) in METRICS.items():
            if p == prop:
                print(f"  {name:<26} {desc}  [{by}]")
