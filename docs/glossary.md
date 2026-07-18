# glossary

Precise definitions for the vocabulary used across `pauth/`, `gateway/`,
`eval/`, and the design docs. Terms are grounded in the code, not intuition:
where a distinction is subtle (crash vs denial, clean vs success, expressibility
vs fidelity) the exact boundary is stated.

---

## The gate chain (prompt → correct execution)

The gateway's mandate is to execute a natural-language prompt's intent with **no
excess and no deficiency** (過不足なく). Between the prompt and a correct
execution are gates; a failure at any one means the prompt was not executed
correctly. Measured per task by `eval/gates.py`.

Metric names carry their axis as a PREFIX (`AVAIL_` / `OUTCOME_` / `SEC_` /
`COST_`) and a NUMBER for position in the nested availability chain, so containment
and axis are readable from the name. PAuth's mandate is the tool CALLS, so the
chain ends at "the required calls were made", NOT at the end-to-end outcome.

**PAuth availability chain (nested `AVAIL_1 ⊇ AVAIL_2 ⊇ AVAIL_3 ⊇ AVAIL_4`):**

- **AVAIL_1_EXPRESSIBLE / 表現可能性.** Can the intent be written in the restricted
  grammar *at all*? The ceiling, Planner-independent. A value buried in prose (a
  bill's "98.70") needs extraction the grammar lacks → inexpressible.
- **AVAIL_2_PLAN_VALID / 文法適合.** Did the Planner produce grammar-valid code?
- **AVAIL_3_RAN_CLEAN / 実行健全.** Did it run without a crash or a false denial?
- **AVAIL_4_CALLS_MADE / 呼び出し達成.** Deficiency-free: was **every required tool
  call made** (correct args)? The **end of PAuth's chain** — its responsibility is
  the calls, not the outcome. A missing call (deficiency) fails here.

**OUTCOME (agent-inclusive, reported APART — not PAuth's chain):**

- **OUTCOME_TASK_COMPLETED / 意図通りの完了.** Did execution reach the goal by the
  prompt's intent (`utility()`)? This is agent-inclusive (answer/content
  generation), so a miss is **not charged to PAuth**. Kept off the chain because it
  *diverges* from the calls (right calls yet no goal — e.g. an answer-query task; or
  goal reached via a non-canonical trace).

Fidelity (過不足) is therefore split THREE ways: **deficiency → AVAIL_4**
(availability), **excess → SEC_NO_EXCESS_CALLS** (security), **end-to-end result →
OUTCOME** (agent). No single fidelity gate.

**Orthogonal SECURITY axis** (never gates the availability chain):

- **SEC_NO_EXCESS_CALLS / 過剰認可なし (least authority).** Does the plan authorize
  **nothing beyond** the ground truth? Excess widens the enforcer's authorized set —
  an injection matching an excess call would be permitted (an **FN enabler**).
  Surfaces this even where the fixed injection set does not. Auxiliary.
- **SEC_INJECTIONS_DENIED / セキュリティ.** Are the task's forced injections denied?
  The FN=0 axis.

**COST:**

- **COST_TOOL_CALLS.** Enforced tool calls per task — a deterministic cost proxy.

**Coverage.** These metrics are UNIVERSAL; each framework populates the subset its
data supports (AgentDojo: all; tau: no OUTCOME; InjecAgent: only SEC). One
parameterized `eval/funnel.py` runs the funnel over any corpus/mode; per-framework
extras are AUX_ metrics (e.g. `AUX_TAU_REWARD`).

---

## Levels & metrics

Three increasingly strict levels a task passes through, and the two error rates.

- **Grammar-accepted / 文法受理 (usable, ACCEPTANCE_RATE).** The Planner produced a plan
  that passed grammar + slice + rule (+ plan-layer). A plan *exists*. Says
  nothing about whether it runs or is correct.
- **Clean / 実行完遂.** A grammar-accepted plan that ran the benign trace
  **without a crash and without a false denial**. It *ran*, not that it *worked*.
- **Task success / タスク成功.** The executed plan reached the goal state
  (ground-truth `utility()` is True). The only level that means the task was
  actually done. `accepted ⊋ clean ⊋ success` — e.g. 64% → 53% → 14% one-shot.
- **FN — over-authorization / 過剰認可 (false negative).** A spurious/injected
  call the enforcer *permitted*. The core invariant is **FN = 0**. This is the
  security failure; everything else is availability.
- **FP — over-rejection / 過剰拒否 (false positive).** A legitimate, benign call
  the enforcer *wrongly denied*. An availability loss, recoverable by retry.
- **ACCEPTANCE_RATE.** usable / total. The availability counterweight to FN=0
  (rejecting everything makes FN=0 trivially).

---

## Failure modes (distinct, do not conflate)

- **Crash / クラッシュ.** The generated `run()` raised **any Python exception
  other than `_Denied`** during execution (KeyError from subscripting prose,
  `datetime <= str` TypeError, `None.field`), so it stopped early. A bug in the
  *generated code's* view of the data. NOT a security event.
- **Denial / 拒否 (`_Denied`).** The **enforcer blocked a call** (no matching
  rule). Raised as `_Denied`, caught separately — *not* a crash. A denial of an
  injection is correct (GS); a denial of a benign call is an FP.
- **Tool error.** The mock tool itself raised; the wrapper swallows it and
  returns `None` (recorded in `tool_errors`) — *not* a crash. (But code that then
  does `None.field` crashes there.)
- **Excess / 過剰.** A call in the plan's trace that is not in the ground truth.
- **Deficiency / 欠落.** A ground-truth call missing from the plan's trace. A
  crash or false denial manifests here (truncated trace).

---

## PAuth mechanism (paper sec. 3–4)

- **Restricted grammar / 制限文法.** The narrow Python subset the **Planner** must
  emit (no method calls, no string ops, no `while`, bounded `for`, flat/nested-if
  ≤3). Narrow *so that* slicing is exact and FN=0 is guaranteed — the same
  restriction that caps expressibility (G1).
- **Planner.** The one LLM step: prompt + tool schemas → a `run()` function in
  the restricted grammar. The only non-deterministic stage, and the quality
  bottleneck for task success once G1 is high. The `--planner oneshot|agentic`
  flag selects it.
- **Slicer.** Deterministic: derives **slices** from the Planner's `run()`.
- **Rule compiler.** Deterministic: compiles slices into **rules**.
- **Enforcer.** Runtime enforcement: intercept each tool call, check it against
  the rules, execute if permitted, wrap the result in a signed
  envelope.
- **Slice / スライス.** Per tool call, a symbolic spec: an expression for each
  operand + the path conditions (guards) to reach the call.
- **Rule / ルール.** A compiled slice the enforcer matches concrete calls
  against. Match-any-rule semantics; default-deny.
- **Enforcer / 執行器.** Checks each concrete call against the rules
  (guards hold AND every operand equals its sliced value) and signs results.
- **Envelope / 封筒.** A concrete value bound to its symbolic provenance, HMAC
  signed by the producing tool. Binds an **immutable snapshot** so later mutation
  of shared env state can't invalidate an earlier signature.
- **Guard.** A path condition on a call (`if C:` → guard `C`; nested → `C1 and
  C2`; else → `not C`). The enforcer requires all guards to hold.
- **Confirmation gate / 確認ゲート.** Holds a call for human confirmation when an
  untrusted-derived value reaches a control operand of a side-effecting call.
  Code: `_confirmation_gate` → `PendingConfirmation` → a `Confirmer`.
- **Two authorization paths / 二つの認可経路.** Every intercepted tool call takes
  exactly one of two paths, forked on whether its control operand's provenance is
  mechanically *provable*:
  - **自動認可経路 (auto-authorization path).** The operand has verifiable
    provenance (prompt-literal, tool-field, computed, structured), so the enforcer
    re-derives and decides allow/deny with no human. FN=0 is held by the enforcer.
    This is the only path headless eval can complete.
  - **人間確認経路 (human-confirmation path).** The operand is untrusted-derived
    (e.g. a value LLM-extracted from prose, `verifiable=False`), so it *cannot* be
    proven; the call is held as a `PendingConfirmation` and routed to a human.
    The human's approve/reject IS the authorization, so **FN=0 is held by the
    human**, and its safety depends on how verifiably the gate presents the
    evidence. A headless run (no `Confirmer`) leaves these calls pending → not
    completed; a human deployment (`InteractiveConfirmer`) can complete them.
- **ground_truth().** AgentDojo's canonical correct tool-call sequence for a
  task. Enables deterministic G1 (expressibility) and the excess/deficiency
  split (XS / G4) — no LLM.
- **utility().** AgentDojo's deterministic check on the post-execution
  environment: did the goal state get reached? The G4 (goal) success criterion.

---

## Key concepts (session findings)

- **Expressibility ceiling / 表現可能性の天井.** The fraction of tasks the
  restricted grammar can represent at all (G1 ≈ 40% on AgentDojo). The binding
  constraint on availability — not Planner quality.
- **Prose-locked value / 散文に埋もれた値.** A value present only inside an
  unstructured text return (a bill amount in a `.txt`), unextractable by the
  string-op-free grammar. The dominant expressibility failure.
- **exec-repair / 実行時修復.** Agentic-Planner stage: dry-run the grammar-valid
  candidate against a mock env; feed a crash back for repair; if still crashing
  after retries, replace with the reject sentinel `def run(): pass`. Eliminates
  crashes but does not raise task success (a `pass` plan does nothing).
- **Intent judge / 意図判定器.** A separate LLM that checks whether the code
  captures the prompt's intent (excess/deficiency) during agentic repair. Noisy
  (opinion-based); the ground-truth `utility()`/`ground_truth()` are preferred.
- **Reject sentinel.** `def run(): pass` — the honest "do nothing" plan the
  agentic pipeline falls back to. Safer than a silently-wrong plan (refuses
  instead of, e.g., sending money with `amount=None`).
- **Framework coverage.** Each benchmark exercises only *part* of the gate chain.
  Only AgentDojo ships `ground_truth`+`utility`, so only it tests G4 (goal) + XS;
  InjecAgent / adversarial probes / tau_retail cover mainly GS (security). "FN=0
  on nine frameworks" was six ways of testing one gate.
- **Measurement noise.** Fresh `--no-cache` agentic acceptance carries ±5–7 pt
  run-to-run variance (gpt-4.1 non-determinism); small effects need multi-run
  averaging or a deterministic (cached / ground-truth) metric.
