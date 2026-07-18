# Experiment log — searching for a sound, generalizing performance lift

**Invariant:** FN=0 (GS) must hold on every run — an intervention that lifts G2/G5
but breaks GS is rejected outright.
**Anti-overfit discipline:** measure on AgentDojo (banking dev) AND a non-AgentDojo
framework (tau_retail). Trust a lift only if it shows on BOTH, or at least does not
regress the held-out one. A gain on banking alone is treated as suspected overfit.
**Noise:** gpt-4.1 is non-deterministic (~±1-2 tasks on a 16-task suite). Single-run
deltas of 1-2 are within noise.

Baselines (cached one-shot, old prompt):
- banking: G2 14/16, G5 4/16, GS 16/16
- tau_retail (30-sample): G2 7/30, FID 0/30, GS 30/30
- tau_retail (all 113): G2 22/113, FID 0/113, GS 113/113

Harness: `g5_experiment.py` (banking, G2/G5/GS), `tau_experiment.py` (tau,
G2/FID/GS). Regenerated plans cached to scratch dirs so re-measuring is free.

---

## E0 — prompt/grammar sync + self-repair regeneration
- **Hypothesis:** the cached one-shot plans used a prompt that forbade constructs
  the grammar now accepts; regenerating with the synced prompt + repair lifts G2/G5.
- **Method:** sync SYSTEM_PROMPT to the grammar (committed 6d23fc2); regenerate.
- **Result:** banking G2 14→16, G5 4→6-7 (+2-3, 1 regression); **tau G2 7→6 (flat/−1)**,
  FID 0→0. GS held everywhere.
- **Verdict:** PARTIAL / SUSPECTED OVERFIT. Helped banking modestly, did NOT
  generalize to tau. Kept the prompt sync (it is a real desync bug fix and is
  grammar-generic, not task-tuned) but do NOT claim a general G5 lift.
- **Rollback:** none (sync is a correctness fix, committed).

## E1 — structure_text exposure (extraction route)
- **Hypothesis:** exposing structure_text lets the Planner extract prose-locked
  control values, lifting G5 on prose-heavy banking.
- **Method:** regenerate banking with augment_with_structuring.
- **Result:** banking G5 6→7 (+1 over plain, within noise); the structure_text-using
  tasks stayed G5=0 (content/date grading wall). GS held.
- **Verdict:** FAIL for G5 (marginal, within noise; capped by AgentDojo's content
  grading). The mechanism is sound (FN=0, control values correct) but does not lift
  the G5 metric on this data.
- **Rollback:** none (structure_text is pre-existing, not wired into default path).

## E2 — few-shot exemplars in SYSTEM_PROMPT
- **Hypothesis:** generic worked examples of the hard grammar shapes (loop,
  nested-if, structure_text, sum, default-override; toy tools, no task content)
  raise G2 across frameworks.
- **Method:** add a WORKED EXAMPLES block to SYSTEM_PROMPT; regenerate banking + tau.
- **Result:** banking G2 14→15, G5 4→7 (within noise of E0's 6-7); **tau G2 7→5
  (DOWN)**, FID 0. GS held on both.
- **Verdict:** FAIL / OVERFIT. banking within noise, tau regressed -- the example
  shapes bias the Planner away from tau's long procedural chains.
- **Rollback:** YES -- reverted the SYSTEM_PROMPT edit in pauth/codegen.py.

## E3 — best-of-N sampling (search, not tuning)
- **Hypothesis:** generating N candidates and selecting the first grammar-valid
  that runs clean (deployment-available, no GT) lifts G2 mechanically and should
  generalize (not task-tuned).
- **Method:** N=3 candidates/task; select first grammar-valid + runs-clean.
- **Result:** banking G2 16/16, G5 7/16 (== single regeneration, no gain from N);
  tau G2 3/15 (== baseline ~20%). GS held on both.
- **Verdict:** FAIL. No lift over single regeneration -- valid candidates all run
  clean, so best-of-N picks the first, same as N=1.
- **Rollback:** none (harness-only probe, no source change).

---

## Meta-conclusion (E0-E3)
- **No intervention produced a robust, GENERALIZING performance lift.** Every
  banking gain is within the noise band; tau is flat-to-negative on all of them.
- **The one replicated effect:** synced-prompt + repair regeneration reliably beats
  the OLD one-shot cache on banking (G5 4 -> 6-7 across all 4 runs). But it is
  framework-specific -- tau does not move (tau's bottleneck is deeper grammar
  expressibility for long procedural tasks, G2 ~20%, not the prompt).
- **structure_text / few-shot / best-of-N add nothing on top of the regeneration**,
  and few-shot slightly hurts the held-out framework.
- **FN=0 (GS) held on EVERY intervention on BOTH frameworks.** Soundness generalizes;
  performance does not. This is the honest headline: the architecture's security
  invariant is robust, but grammar/Planner performance gains do not transfer across
  frameworks -- so any single-framework "win" must be treated as suspected overfit
  until it replicates on a held-out framework.
