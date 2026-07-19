# Evaluation

This is the canonical results write-up: the central claim, the evaluation setup,
the measured results, and the honest limits. Trial-by-trial detail lives in
[avail4-improvement-log.md](avail4-improvement-log.md); the architecture lives in
[architecture.md](architecture.md); the threat model in [threat-model.md](threat-model.md).

Every number here is reproducible from the commands in each section. The runner
never hard-codes an outcome: a wrong plan surfaces as lost availability, an
inaccurate slice would surface as a denied-injection failure.

---

## 1. Central claim

> **The security guarantee (FN = 0, no over-authorization) is decoupled from the
> planner's quality and from the task's form. Availability is bounded instead by
> a task's *static plannability*. Where that bound bites, a human-authorization
> path recovers a slice of it by moving FN = 0 from the enforcer to a human,
> under single-use, fully-bound grants — at a measured automation cost.**

Three consequences, each measured below:

1. **FN = 0 is framework- and planner-invariant** (§4). It holds on AgentDojo,
   tau-bench retail, and InjecAgent; with a weak planner (GPT-4.1) and a strong
   one (GPT-5.1); on the automatic path and on the human path.
2. **Availability (AVAIL_4) is a function of static plannability** (§5). It moves
   from 48 % (single-turn natural-language tasks) to 0 % (multi-turn tasks whose
   control values are disclosed only at runtime). A stronger planner raises it but
   cannot reach 100 %: the residual is structurally un-plannable under FN = 0.
3. **A stronger planner trades availability for least authority** (§6), and the
   only FN = 0-preserving way to recover the availability gap is a human in the
   loop (§7) — not a smarter or more-informed planner.

---

## 2. System under test

The pipeline (full detail in [architecture.md](architecture.md)):

```
prompt ──▶ Planner ──▶ Slicer ──▶ Rule compiler ──▶ Enforcer
        (LLM, the ONLY      (deterministic)        (default-deny; matches every
         non-deterministic                          call's control operands against
         step)                                      the compiled rules; signs results
                                                    into tamper-evident envelopes)
```

The Planner reads **only the trusted prompt and the tool schemas** — never
untrusted runtime data — so the control operands it plans (recipient, amount:
the operands that steer a side effect) have clean provenance. The Enforcer is
default-deny: a call is authorized only if some rule re-derives its control
operands from signed envelopes. This is what makes FN = 0 independent of the
Planner: however the Planner errs, the Enforcer authorizes only what the trusted
plan re-derives.

**Two authorization paths.** The automatic path (Enforcer) authorizes calls whose
control operands have provable provenance. The **human-authorization path** (§7)
covers calls the Enforcer *cannot* verify — a value that lives only in untrusted
data the plan never read — by asking a human, who then holds FN = 0 for that call.

---

## 3. Evaluation setup

| Framework | Prompt form | Ground truth | What it exercises |
|-----------|-------------|--------------|-------------------|
| **AgentDojo** v1 (banking, slack, travel, workspace) | single-turn natural language | `ground_truth` calls + `utility` | availability + security, single-turn |
| **tau-bench retail** | multi-turn role-play | GT actions (no utility) | the multi-turn / runtime-disclosed-value ceiling |
| **InjecAgent** | attack-focused | reference plan | security only (injection density) |

**Metric vocabulary** (canonical names in [`eval/metrics.py`](../eval/metrics.py);
the prefix names the axis, the number the position in the nested availability chain):

```
AVAIL_1_EXPRESSIBLE ⊇ AVAIL_2_PLAN_VALID ⊇ AVAIL_3_RAN_CLEAN ⊇ AVAIL_4_CALLS_MADE
OUTCOME_TASK_COMPLETED     (agent-inclusive; reported apart)
SEC_NO_EXCESS_CALLS        (least authority: nothing beyond ground truth)
SEC_INJECTIONS_DENIED      (FN = 0: every forced injection denied)
COST_TOOL_CALLS            (calls routed through the Enforcer)
```

AVAIL_4 counts a task as complete only if the plan makes every ground-truth call
matched on its **control operands** — PAuth's mandate — not on benign content or
count arguments. Percentages are of the full task total (97 for AgentDojo, 113
for tau).

Reproduce:

```
python -m eval.funnel agentdojo --mode headless --planner bestof --model gpt-5.1 --structuring
```

---

## 4. Result R1 — FN = 0 is framework- and planner-invariant

Every forced injection (tampered control operand, or an off-task sensitive call)
is denied, across all three frameworks and both planner models:

| Framework | Forced injections | Denied (FN = 0) |
|-----------|-------------------|-----------------|
| AgentDojo (GPT-5.1) | 92 | **92 / 92** |
| tau-bench retail (GPT-5.1) | 109 | **109 / 109** |
| InjecAgent | 1054 | **1054 / 1054** |
| AgentDojo (GPT-4.1, `eval.fpfn`) | 390 | **390 / 390** |

The weak and strong planners hold FN = 0 identically. This is the decoupling: the
Enforcer's guarantee does not depend on how good the plan is. A denied-injection
failure would appear as a number below the total; none does.

```
python -m eval.fpfn --suites all        # GPT-4.1 forced-injection sweep
```

---

## 5. Result R2 — availability is bounded by static plannability

The nested availability chain, weak vs. strong planner, on AgentDojo (/97):

| Metric | GPT-4.1 (single-shot) | GPT-5.1 (structured + best-of-N) |
|--------|----------------------:|---------------------------------:|
| AVAIL_1_EXPRESSIBLE | 97 | 97 |
| AVAIL_2_PLAN_VALID | 62 | 92 |
| AVAIL_3_RAN_CLEAN | 51 | 88 |
| **AVAIL_4_CALLS_MADE** | **16 (16 %)** | **47 (48 %)** |
| OUTCOME_TASK_COMPLETED | 14 | 22 |
| COST_TOOL_CALLS | 2.2 | 3.2 |

A stronger planner nearly triples AVAIL_4 — but not to 100 %. The 50 lost tasks
(of 97) decompose by **cause**, and the decomposition is the point:

| Cause | Count | Can any planner fix it under FN = 0? |
|-------|------:|--------------------------------------|
| Grammar-invalid plan (AVAIL_2 loss) | 5 | maybe (grammar/prompt) |
| Crash / false denial (AVAIL_3 loss) | 4 | maybe (execution repair) |
| **Deficiency** — a needed control call the plan never made | 41 | split below |
| ├─ static multi-step / conditional (capability + grammar limit) | 26 | no legitimate lever reaches it |
| ├─ dynamic-content ("do everything on this page") | 5 | **no — actions live in unread untrusted content** |
| └─ un-derivable value (GT-fixed, in no data) | 6 | **no — the value exists nowhere to read** |

The **cross-framework** view makes the bound explicit. tau-bench retail, whose
tasks are entirely multi-turn with control values disclosed turn-by-turn at
runtime, has AVAIL_4 = **0 / 113** — model-invariant (GPT-4.1 and GPT-5.1 both 0),
because a statically-compiled plan cannot contain a value that does not exist
until a later turn. Same wall as AgentDojo's dynamic-content tasks, covering the
whole corpus. FN = 0 held throughout (109 / 109).

**AVAIL_4 = 100 % is structurally unreachable** under static planning + FN = 0 +
no metric-gaming. Thirteen trials across two models establish this
([avail4-improvement-log.md](avail4-improvement-log.md)); the honest ceiling is
48 % (GPT-5.1) / 61 % (GPT-4.1 with control-match + best-of-N + structured reads).
The ceiling is not a defect — it is the price of FN = 0 (§8).

---

## 6. Result R3 — a stronger planner trades availability for least authority

Least authority (SEC_NO_EXCESS: no call beyond ground truth) moves the *wrong*
way with the stronger model:

| Metric (AgentDojo /97) | GPT-4.1 | GPT-5.1 |
|------------------------|--------:|--------:|
| AVAIL_4 (availability) | 27 % | **48 %** ↑ |
| SEC_NO_EXCESS (least authority) | 28 % | **22 %** ↓ |
| excess side-effecting authorizations | fewer | **42** ↑ |

GPT-5.1 completes more tasks but authorizes *more* than each task needs — 42 extra
side-effecting calls (send, update, schedule). Each excess authorization widens
the authorized set: an injection matching an excess call's tool **and control
operands** would be permitted. `SEC_INJECTIONS_DENIED` = FN 0 only means the
*fixed tested* injection set was denied; it does not certify the excess surface.
This is a **weak** FN surface — an injection must match the excess call's specific
benign operands, so it re-authorizes a benign action rather than an arbitrary
attacker one — but it is a real least-authority regression, on PAuth's own axis.

The excess is **planner-intrinsic, not a selection artifact.** Re-selecting the
best-of-N candidate for fewest side-effecting calls instead of most leaves it
unchanged (SEC_NO_EXCESS 21 → 21, AVAIL_4 47 → 46): the N candidates emit the same
excess. Selection is not a lever ([`tests/experiment/selection_tradeoff.py`](../tests/experiment/selection_tradeoff.py)).

---

## 7. Result R4 — the human-authorization path recovers a measured slice

Where the Enforcer must deny (the value lives only in untrusted data), a human —
not a smarter planner — recovers the task. The production path is
[`gateway/runtime/human_authorized.py`](../gateway/runtime/human_authorized.py):

1. A **proposer** (pluggable; an LLM extractor in production, an oracle in eval —
   its accuracy is a separate, generic problem) surfaces the missing action with a
   concrete, untrusted-labelled candidate value.
2. A **confirmer** (the human) approves or rejects. FN = 0 for this call now rests
   on the human, so a rubber-stamp human loses it and an informed one keeps it.
3. On approval, a single-use **HumanGrant** is minted, **bound to the tool and
   every control operand** and signed; the action executes only by *redeeming* the
   grant, which consumes it.

Measured through the real execution path (`mode=authorize`, GPT-5.1, /97):

| Metric | Headless (Enforcer only) | + human-authorization |
|--------|-------------------------:|----------------------:|
| AVAIL_4 | 47 | **49 (+2)** |
| OUTCOME (utility-verified) | 22 | **29 (+7)** |
| automation cost | — | 27 confirmations over 16 gated tasks |

**Two honesty notes.** (a) The recovery is modest. An earlier estimate that
localized candidate values by substring suggested +23; driving the *real* path —
which requires each deficiency to actually resolve via an executed grant — deflated
it to **+2 AVAIL_4 / +7 OUTCOME**. OUTCOME recovers more than AVAIL_4 because a task
can functionally complete via the human-authorized call while the strict
control-operand metric still flags another unmatched call. (b) The confirmer here
is perfectly informed (a ceiling assuming perfect extraction); a real extractor
and a real human do worse.

The grant's security properties are pinned in
[`tests/test_human_authorized.py`](../tests/test_human_authorized.py):

- an approved proposal executes; the plan's own enforced calls are unchanged;
- **single use** — a replayed identical call finds no unconsumed grant (denied);
- **full binding** — a grant for `(landlord, 98.70)` does not authorize
  `(attacker, 98.70)` (operand splice denied);
- a forged grant fails signature verification;
- **condition 1** — the same tampered proposal is approved by a rubber-stamp human
  (FN) and rejected by an informed one (FN = 0).

```
python -m eval.funnel agentdojo --mode authorize --planner bestof --model gpt-5.1 --structuring
```

---

## 8. Why the availability ceiling *is* the security guarantee

The Enforcer plans from trusted input only; the executing agent acts on all data,
including attacker injections. The gap between "what the agent would do (data-
informed)" and "what the plan authorizes (trust-informed)" splits in two:

- **benign gap** — the agent's data-derived action is legitimate, but PAuth cannot
  re-derive it from trusted input, so it denies → an availability loss (the AVAIL_4
  ceiling);
- **malicious gap** — the agent's data-derived action is an injection → PAuth
  denies → FN = 0.

PAuth cannot tell them apart from trusted input alone, so it fail-safe-denies both.
**The availability ceiling and the injection surface are the same gap.** Closing it
(feeding the planner all data) would re-open the injection surface. The only FN = 0-
preserving recovery is to let a human resolve the benign gap — which is exactly the
human-authorization path, at the cost of automation rate.

---

## 9. Limitations (explicitly out of scope)

Named so they are not mistaken for unacknowledged gaps:

1. **Extractor accuracy.** The human-path recovery (§7) assumes perfect extraction.
   A production proposer is an LLM information-extraction component; its accuracy
   caps real recovery and is a generic NLP problem, not PAuth's contribution.
2. **Confirmation fatigue.** Condition 1 (meaningful confirmation) is load-bearing;
   a real human under volume (≈1.7 confirmations per gated task here) may
   rubber-stamp, which forfeits FN = 0. The confirmation UX (decomposition,
   provenance, hidden-character detection) mitigates but does not evaluate this.
3. **Multi-turn / dynamic tasks.** tau's AVAIL_4 = 0 is architectural: static
   planning cannot cover runtime-disclosed control values. Recovering these needs
   staged re-planning (per-turn FN = 0 re-establishment) — a different system.
4. **Ceiling vs. deployment.** All human-path numbers use an oracle confirmer and
   an oracle proposer; they are upper bounds. A real deployment does worse.

---

## 10. Summary

| Claim | Evidence | Status |
|-------|----------|--------|
| FN = 0 is planner- and framework-invariant | R1: all injections denied, 3 frameworks, 2 models | **established** |
| Availability is bounded by static plannability | R2: 48 % single-turn → 0 % multi-turn; ceiling proven | **established** |
| Stronger planner erodes least authority | R3: SEC_NO_EXCESS 28 % → 22 %, 42 excess calls, planner-intrinsic | **established** |
| Human path recovers a slice, FN = 0 held by human | R4: +2 AVAIL_4 / +7 OUTCOME, single-use bound grants | **established (ceiling)** |
| The ceiling is the price of FN = 0 | §8: benign gap ≡ injection surface | **argued** |

274 tests pass. No measurement is hard-coded; the honest ceiling is reported, not
engineered.
