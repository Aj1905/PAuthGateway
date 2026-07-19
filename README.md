# PAuthGateway

**A task-scoped authorization firewall that sits between an AI agent and the real
tools and SaaS it calls.** It derives a plan from the user's clean prompt exactly
once, then checks every tool call against it — default-deny. Even if the agent is
hijacked by prompt injection or poisoned tool output, **operations the user did
not request cannot execute.**

Based on *"PAuth — Precise Task-Scoped Authorization For Agents"* (Sharma, Jiang,
Lin & Chen, arXiv:2603.17170). The first integration target is Claude Code.

- **Want the security argument and the numbers?** → [`docs/evaluation.md`](docs/evaluation.md)
- **Want to run the gateway in front of an agent?** → [Deploy](#deploy)
- **Want to reproduce the experiments?** → [Reproduce](#reproduce)

---

## Why

An OAuth token grants *standing* access to a whole scope ("can send email", "can
transfer money"). Once the agent is hijacked, everything the token allows, the
attacker does too. A token answers "*who* may use this API" — never "*is this
specific call part of what the user asked for?*"

PAuthGateway moves the enforcement point **outside** the agent:

- **The agent is unmodified** — it is intercepted via hooks (later MCP / proxy).
- **Plan once, enforce every call** — the agent cannot rewrite the plan after
  seeing poisoned output.
- **The decision is deterministic** — no LLM makes the permit call. Only plan
  generation uses an LLM; slicing, rule compilation, and enforcement are
  deterministic.
- **The gateway owns observation** — each tool result is recorded as a signed
  envelope, so a forged value cannot steer later checks.

Design: [`docs/architecture.md`](docs/architecture.md) ·
Threat model: [`docs/threat-model.md`](docs/threat-model.md) ·
Terms: [`docs/glossary.md`](docs/glossary.md)

## What it is *not*

- **Not a correctness guarantee.** If the user approves a wrong plan, wrong things
  happen *within* that plan. PAuth guarantees only "does not exceed the requested
  scope."
- **Not an agent sandbox.** The gateway sees tool calls. Side channels (Bash, file
  ops) need a separate mechanism.

---

## The pipeline

```
prompt ─▶ Planner ─▶ Slicer ─▶ Rule compiler ─▶ Enforcer
        (LLM: the      (deterministic)          (default-deny; matches each call's
         only non-                               control operands against the rules;
         deterministic                          signs results into tamper-evident
         step)                                   envelopes)
```

The **Planner** reads only the trusted prompt and tool schemas — never untrusted
runtime data — so control operands (recipient, amount) have clean provenance. The
**Enforcer** authorizes a call only if a rule re-derives its control operands from
signed envelopes. This is why the security guarantee does not depend on plan
quality: however the Planner errs, the Enforcer authorizes only what the trusted
plan re-derives.

---

## Quick check — does the gateway actually control the agent?

Confirm the core property (**FN = 0**: no injection is ever permitted) on every
framework in one command. No API key needed for the offline frameworks:

```bash
.venv/bin/python -m eval.check
```

```
framework      FN  injections   FP  tasks  result
shopping        0           8    0      2  PASS
dining          0           7    0      2  PASS
injecagent      0        1598    0   1054  PASS
banking         0         135    0     13  PASS
...
RESULT: PASS -- no injection permitted on any framework (FN=0).
```

**FN = 0 is the security bar and the pass/fail gate.** FP = 0 (no over-rejection)
is an availability goal we track but do not fail on: it depends on plan quality,
is recoverable by retry, and is not a breach.

**Full results, cross-framework and cross-model, are in
[`docs/evaluation.md`](docs/evaluation.md)** — including the availability ceiling,
the least-authority trade-off, and the human-authorization path.

---

## Deploy

For **operators** putting the gateway between an agent and real tools. (To only
reproduce the experiments, skip to [Reproduce](#reproduce).)

Four moves in every case: **install → run the daemon → run the agent as a
dedicated non-admin user → restrict that user's egress to the gateway.** How the
agent *reaches* the gateway differs by where it runs:

| Situation | How the agent reaches the gateway |
|-----------|-----------------------------------|
| **A. Local agent** (Claude Code / a script on your machine) | You own the config, so you hand the prompt and tool calls to the gateway directly via hooks. |
| **B. Cloud / API agent** (runs on a provider, driven over an API) | You don't own the process, so the gateway becomes the tool/credential boundary. |

### Prerequisites

Install the repo and virtualenv:

```bash
git clone https://github.com/Aj1905/PAuthGateway.git && cd PAuthGateway && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Generate a shared auth token so only your client can drive the daemon:

```bash
export GATEWAY_AUTH_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Case A additionally needs admin/`sudo` (used once, for the network step) and a
supported firewall — Linux `nftables`/`iptables` or macOS `pf`.

### Run the daemon (both cases)

Bind to loopback and require the token on every route:

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081 --auth-token "$GATEWAY_AUTH_TOKEN"
```

Keep it running (a `systemd` unit, `launchd` job, or `tmux` window). Useful flags:
`--session-store PATH` to survive restarts, `--audit-log PATH` to append
permit/deny decisions as JSONL. Check liveness (this route needs no token):

```bash
curl http://127.0.0.1:8081/health
```

> **Run the gateway as a *different* OS user than the agent.** The gateway must
> reach the real SaaS; the egress rule in Case A deliberately does not apply to it.

### Case A — local agent

Add two hooks so Claude Code hands the clean prompt and each tool call to the
gateway (no change to Claude Code, no change to how you type prompts). In
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "type": "command", "command": "/ABSOLUTE/PATH/PAuthGateway/gateway/hooks/submit_prompt.sh" }
    ],
    "PreToolUse": [
      { "type": "command", "command": "/ABSOLUTE/PATH/PAuthGateway/gateway/hooks/pretool.sh" }
    ]
  }
}
```

`submit_prompt.sh` forwards the prompt **before** the model sees it (the plan is
built from the clean task); `pretool.sh` presents **every** tool call for a
permit/deny check. Point them at the daemon with `export
GATEWAY_URL=http://127.0.0.1:8081` and the same token. Options:
[`gateway/hooks/README.md`](gateway/hooks/README.md). Not Claude Code? Any local
agent can `POST` the prompt once, then each tool call, to `/sessions/<id>/messages`
([`docs/self-hosting.md`](docs/self-hosting.md#prompt-capture-boundary)).

Create the dedicated non-admin agent user (needs admin):

```bash
sudo useradd -m -s /bin/bash pauth-agent   # macOS: sysadminctl -addUser pauth-agent
```

Restrict that user's egress to the gateway only (needs admin, run once):

```bash
sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
```

Now every outbound connection that user makes can reach only `127.0.0.1:8081`;
anything else is dropped by the kernel. Status / undo:

```bash
sudo AGENT_USER=pauth-agent gateway/deploy/egress_lockdown.sh status
```

Then run the agent as that user (e.g. `sudo -u pauth-agent claude`).

### Case B — cloud / API agent

The agent runs on a provider you don't control. Two things change: (1) there is no
local UID to pin, so the equivalent control is to make the gateway the **only tool
endpoint the agent can reach** and broker credentials so real SaaS tokens live
only inside the gateway; (2) prompt capture uses a **gateway-owned entry point** —
submit the task to the gateway *first* (`POST /sessions/<id>/messages`), then let
the agent run with its tool calls routed to the gateway.

Honest caveat: if a cloud agent can call arbitrary URLs and you cannot constrain
its egress or credentials, a pure API relationship gives you **observation and
per-destination allow/deny, not full enforcement**. See
[`docs/self-hosting.md`](docs/self-hosting.md#setup-boundary).

### The one rule that voids everything

**Keep the agent non-admin.** If it runs as root / admin / a `sudo`-capable user,
injected code can flush the firewall rule or reach SaaS directly and bypass the
gateway. The lockdown script refuses to apply to a privileged account for exactly
this reason.

---

## Reproduce

Install:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Python 3.12+ (verified on 3.14).

### Offline — no API key

Verify zero FP / zero FN of the deterministic core against the paper's worked
examples (banking sec. 5.3, shopping sec. 4):

```bash
.venv/bin/python -m tests.test_worked_examples
```

Throw attacks (off-slice operators, tampered recipient/amount/date, tampered
envelope) directly at the Enforcer on the real AgentDojo tools:

```bash
.venv/bin/python -m tests.test_unexpected_attacks
```

Interpret strictly: PAuth rejects off-slice attacks, but **a replay that exactly
matches the legitimate slice is permitted** — that is PAuth's authorization
boundary, not a bug.

### Full experiment — OpenAI API key

Run the Planner (GPT-4.1) over the four AgentDojo suites and measure FP/FN:

```bash
cp .env.example .env   # write OPENAI_API_KEY, then:
.venv/bin/python -m eval.fpfn --suites all
```

Try cheaply first (first 3 tasks of one suite):

```bash
.venv/bin/python -m eval.fpfn --suites banking --limit 3
```

The parameterized funnel measures the full availability + security chain across
models and modes (see [`docs/evaluation.md`](docs/evaluation.md)):

```bash
.venv/bin/python -m eval.funnel agentdojo --mode headless --planner bestof --model gpt-5.1 --structuring
```

`eval.fpfn` options: `--suites banking,shopping` (pick suites), `--limit N`,
`--model gpt-4.1`, `--no-cache`, `--out path.json`. Cost ≈ $0.002–0.04/task
(~$1–4 for all 97); generated plans cache under `tests/experiment/cache/`, so
re-runs are free.

### How FP / FN is measured

- **FP (benign)** — the generated plan is *actually executed* and every call
  passes through the Enforcer; one rejection makes the task an FP. Rules are
  derived from the same plan, so a correct implementation yields FP = 0.
- **FN (injection)** — a forced injection (a tampered operand, or an off-task
  sensitive call) is presented to the Enforcer against the post-run envelope
  store; if any rule permits it, that is an FN. PAuth is default-deny.

The harness is not vacuous: passing an on-slice call as an injection *is*
permitted (correctly detected as an FN).

---

## Repository structure

```
pauth/              PAuth core (framework-independent, mostly deterministic)
  grammar.py          restricted-grammar parser / validator (paper Appendix A)
  codegen.py          Planner: restricted-grammar code generation (OpenAI)
  slicing.py          Slicer: natural-language slice derivation
  rules.py            Rule compiler: Algorithm 1
  evaluator.py        deterministic evaluator for slice expressions
  enforcer.py         Enforcer: runtime authorization + sandboxed executor
  envelope.py         signed-envelope structure, HMAC signing, store
  pipeline.py         Planner → Slicer → Rule compiler wiring
  suites/shopping.py  the paper's self-contained Shopping suite
gateway/
  serving/http_server.py       local HTTP daemon
  hooks/                        Claude Code prompt + tool-call hooks
  deploy/egress_lockdown.sh     per-user egress restriction
  planning/agentic_planner.py   Planner with grammar + semantic self-repair
  runtime/confirmation.py       confirmation-gate machinery (untrusted-derived operands)
  runtime/confirmer.py          confirmer strategies (informed / cautious / rubber-stamp)
  runtime/human_authorized.py   human-authorization path: single-use, bound, signed grants
benchmarks/
  agentdojo_adapter.py          normalizes the 4 AgentDojo suites to one interface
  forced_injection.py           forced-injection generation (sec. 5.1)
  injecagent_adapter.py, tau_bench_adapter.py   additional framework adapters
eval/
  check.py            one-command FN=0 control check across every framework
  fpfn.py             FP/FN + acceptance runner (paper Table 2 / Fig. 10)
  funnel.py           parameterized funnel: availability + security across corpus/mode/planner
  metrics.py          canonical metric vocabulary (AVAIL / OUTCOME / SEC / COST)
  gates.py            per-metric attribution (see docs/glossary.md)
docs/
  evaluation.md       central claim, results, limitations  ← start here for the science
  architecture.md     whole-system logical design
  threat-model.md     defense boundary (in / out of scope)
  glossary.md         precise definitions
```

---

## Correspondence with the paper

| Paper | This implementation |
|-------|---------------------|
| Imperative code generation (LLM, sec. 4.1.1) | Planner — `pauth/codegen.py` (OpenAI, Appendix A prompt) |
| NL slice derivation (sec. 3.3 / 4.1.2, deterministic) | Slicer — `pauth/slicing.py` |
| Rule compilation (Algorithm 1, deterministic) | Rule compiler — `pauth/rules.py` |
| Signed envelope (sec. 3.4 / Fig. 3) | `pauth/envelope.py` |
| Runtime enforcement (sec. 4.1.3, deterministic) | Enforcer — `pauth/enforcer.py` |
| Restricted grammar (BNF, Appendix A) | `pauth/grammar.py` |
| AgentDojo implementation (sec. 4.1) | `benchmarks/agentdojo_adapter.py` |
| Forced injection (sec. 5.1) | `benchmarks/forced_injection.py` |
| FP/FN evaluation (sec. 5.2, Table 2) | `eval/fpfn.py` |

As in the paper, **only the Planner needs an LLM**; slicing, rule compilation,
enforcement, and the envelope are fully deterministic (paper sec. 5.2).

## Reproduction scope

- **Planner model** — the paper primarily uses GPT-4.1 (partial GPT-5-Mini /
  Gemini-3-Flash / Sonnet-4.5). This implementation defaults to the OpenAI family
  (`--model` to switch); results here also cover GPT-5.1.
- **Envelope signing** — the paper exchanges signed envelopes between hosts; this
  is a single-host configuration matched to AgentDojo, using HMAC over a
  shared-memory envelope store.
