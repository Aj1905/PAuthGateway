# run() Gate Experiment

This directory is intentionally separate from the existing PAuth precision and
prompt-injection experiments.

The experiment tests a conservative front gate for an LLM-generated permission
description language, run().  The gate does not try to solve arbitrary natural
language understanding.  Instead, it accepts only prompts in a small,
deterministically recognized subset, derives the canonical run() from that subset,
and accepts the LLM output only when it exactly matches the canonical run().

That is the only credible way to aim for zero false accepts.  The cost is many
false rejects: ambiguous prompts are rejected even if a human could infer a
reasonable intent.

## Run

Offline, deterministic fixture translator:

```bash
.venv/bin/python run_gate_experiment/run_experiment.py --backend fixture
```

Optional LLM translator:

```bash
.venv/bin/python run_gate_experiment/run_experiment.py --backend llm --model gpt-4.1-mini --temperature 0.2
```

The runner:

- prepares several natural-language task prompts;
- asks a translator to produce run() JSON;
- retries the same prompt when the deterministic gate rejects the result, until
  OK or until the safety cap is reached;
- verifies that unsupported or injected prompts are rejected;
- mutates accepted run() documents and verifies every mutation is rejected;
- converts an accepted run() document to PAuth-style `run()` code and runs `pauth.prepare()`
  to confirm the accepted cases can proceed to slice/rule generation.

## Interpretation

This is not a proof that arbitrary NL-to-run() translation can be made safe.
It demonstrates a narrower claim:

> If the user prompt belongs to a deterministic, auditable subset, and the run()
> output exactly matches the canonical run() derived by the verifier, then the
> verifier can avoid accepting mistranslations in that subset.

Anything outside the subset is rejected.
