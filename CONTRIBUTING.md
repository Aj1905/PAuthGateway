# Contributing to PAuth Gateway

Thanks for your interest. PAuth Gateway is a task-scoped authorization gateway
for AI agents — a security tool, and a reproduction/extension of the PAuth paper
(see `NOTICE`). Contributions are welcome under the Apache-2.0 license.

## Ground rules for a security tool

The point of this project is one guarantee: **a compromised agent cannot act
beyond the approved plan** (no over-authorization). Two invariants protect it —
do not weaken them without a clear rationale in the pull request:

1. **The deterministic core stays deterministic.** Every planner strategy emits
   restricted `run()` code that `pauth.prepare()` parses, slices, and compiles
   into rules. No planner may bypass that validation or emit rules directly
   (see `docs/planning-strategies.md`).
2. **Agent-facing feedback stays value-free.** Denial reasons that re-enter the
   agent's model context must carry no operand values — they could be a
   prompt-injection payload. See `gateway/runtime/feedback.py`.

## Repository layout

- `pauth/` — the framework-neutral algorithm core: restricted grammar, slicing,
  rule compilation, the enforcer, signed envelopes, and A1 code generation.
- `gateway/` — the runtime: planner strategies, per-call enforcement, tool
  providers (MCP / OpenAPI / suites), HTTP serving, ingress, deploy scripts, and
  Claude Code hooks.
- `eval/` — measurement runners (FP/FN, freeform A1).
- `tests/` — unit tests plus experiment adapters and fixtures.
- `docs/` — design docs: architecture, threat model, self-hosting, ingress
  design, planning strategies, and design status.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

`OPENAI_API_KEY` (and optionally `ANTHROPIC_API_KEY`) are only needed for the
LLM planner / judge paths; the deterministic and offline tests run without any
key. Copy `.env.example` to `.env` if you use one.

## Running the tests

```bash
.venv/bin/python -m pytest tests/ -q                          # full suite, offline
.venv/bin/python -m tests.test_recognizer --backend fixture   # A1 recognizer, no key
.venv/bin/python -m eval.fpfn --suites shopping               # FP/FN measurement, no key
```

All tests must pass before a PR. New behavior needs a test; a security-relevant
change (enforcement, feedback, taint, side-channel, egress) needs a test that
would fail without the fix.

## Pull requests

- Branch from `main`; keep each PR focused.
- Match the surrounding code style. Comments should state constraints, not
  narrate the change.
- When your change touches enforcement, feedback, taint, or side-channel policy,
  explain the security rationale in the PR description.
- By submitting a pull request you agree to license your contribution under the
  Apache-2.0 license.

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Use GitHub's private
vulnerability reporting — see `SECURITY.md`.
