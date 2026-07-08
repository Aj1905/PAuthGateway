# run() gate experiment

This directory is intentionally kept separate from the existing PAuth precision
experiment and the prompt-injection experiment.

This experiment validates a conservative front gate for the permission
description language run() that an LLM generates. This gate does not attempt to
solve arbitrary natural-language understanding. Instead, it accepts only prompts
that belong to a small, deterministically recognizable subset, derives a
canonical run() from that subset, and accepts only when the LLM's output exactly
matches the canonical run().

This is the only reliable way to aim for zero false accept. The cost is a high
false-reject rate. Ambiguous prompts are rejected even in cases where a human
could reasonably infer the intent.

## Running

Offline deterministic fixture translator:

```bash
.venv/bin/python -m tests.test_recognizer --backend fixture
```

Optional LLM translator:

```bash
.venv/bin/python -m tests.test_recognizer --backend llm --model gpt-4.1-mini --temperature 0.2
```

This runner does the following:

- prepares several natural-language task prompts;
- has the translator generate run() JSON;
- if the deterministic gate rejects a result, retries the same prompt until it
  becomes OK or a safety cap is reached;
- verifies that unsupported or injected prompts are rejected;
- mutates an accepted run() document and verifies that all mutations are
  rejected;
- converts an accepted run() document into PAuth-style `run()` code, runs
  `pauth.prepare()`, and confirms that the accepted case proceeds to slice/rule
  generation.

## Interpretation

This is not a proof that arbitrary NL-to-run() conversion can be done safely. It
demonstrates a narrower claim:

> If a user prompt belongs to a deterministic, auditable subset, and the run()
> output exactly matches the canonical run() derived by the verifier, then the
> verifier can avoid accepting a misconversion within that subset.

Everything outside the subset is rejected.
