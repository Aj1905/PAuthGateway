# PAuthGateway

**A personal "task-scoped authorization firewall" that sits between an AI agent
and the real tools/SaaS it calls.** (The first target is Claude Code.)

Before the agent starts acting, the natural-language prompt the user enters is
converted exactly once into a "restricted plan," and every subsequent tool call
is checked against that plan (default-deny). As a result, even if the agent is
hijacked by prompt injection or tool-result poisoning, **operations the user did
not actually request cannot be executed**.

The method itself is based on the paper **"PAuth – Precise Task-Scoped
Authorization For Agents"** (Sharma, Jiang, Lin & Chen, arXiv:2603.17170).

## The problem it solves

An autonomous agent with access to real systems (banks, email, internal SaaS)
can, from a single injection, execute operations the user never asked for, such
as "wire money to the attacker's destination" or "forward confidential email."
Existing countermeasures tend toward either (a) modifying the agent itself or
(b) making the LLM police itself. The former is heavy to deploy, and the latter
delegates the judgment to the very party that has been hijacked.

PAuthGateway moves the enforcement point outside the agent:

- **The agent is unmodified.** It only intercepts prompts and tool calls via
  Claude Code hooks (later MCP/proxy).
- **Plan once.** The agent cannot rewrite the plan after seeing poisoned tool
  output (plan once / enforce every call).
- **Enforcement is deterministic.** No LLM is used for the permit decision. Only
  plan generation (A1) uses an LLM; the checking (A2/A3 and B1–B4) is fully
  deterministic.
- **The gateway is the authority on observation.** The gateway records each
  tool's execution result as a signed envelope, so even if the agent forges a
  value, it does not affect subsequent checks.

For the detailed design see [`docs/architecture.md`](docs/architecture.md); for
the defense scope and non-targets see
[`docs/threat-model.md`](docs/threat-model.md).

## What this is *not*

- Not a guarantee of correctness. If the user approves a wrong plan, then wrong
  things will happen within that plan. PAuth only guarantees that "it does not
  exceed the scope the user requested."
- Not a sandbox for the agent itself. The gateway only sees tool calls. Side
  channels such as Bash or file operations require a separate mechanism (a
  sandbox, etc.).

---

## Quick check: does the gateway actually control the agent?

Before anything else, confirm the core property on every integrated framework in
one command (no API key needed for the offline frameworks):

```bash
.venv/bin/python -m eval.check
```

It runs each framework's benign tasks and replays every forced injection through
the gateway, then reports **FN** (an injection that was wrongly *permitted* —
must be 0) and **FP** (a benign call wrongly *denied*). It exits non-zero if any
injection gets through anywhere:

```
framework      FN  injections   FP  tasks  result
shopping        0           8    0      2  PASS
dining          0           7    0      2  PASS
injecagent      0        1598    0   1054  PASS   <- InjecAgent indirect-injection benchmark
banking         0         135    0     13  PASS   <- AgentDojo (cached A1)
...
RESULT: PASS -- no injection permitted on any framework (FN=0).
```

**FN=0 is the security bar and is the check's pass/fail gate.** FP=0 (zero
over-rejection) is an availability goal we also track but do not fail on — it
depends on A1 plan quality, is recoverable by retry, and is not a breach.
Offline frameworks (shopping, dining, injecagent) always run; the AgentDojo
suites run from cached A1 and are skipped cleanly if no cache/key is present.

---

## Deploying the gateway in front of your agent

This section is for **operators** who want to actually put the gateway between an
agent and the real tools it calls. (If you only want to reproduce the paper's
measurements, skip to [Setup](#setup-reproducing-the-paper-experiments).)

Deployment is the same four moves in every case:

1. **Install the tools** — Python + this repo.
2. **Deploy the gateway** — run the daemon as its own service.
3. **Configure the network (needs admin)** — run the agent as a dedicated
   non-admin user.
4. **Restrict egress to the gateway** — pin that user's outbound traffic so the
   gateway is the *only* place its tool calls can go.

But **how you connect the agent to the gateway differs by where the agent runs.**
Pick your case:

| Your situation | How the agent reaches the gateway | Read |
|---|---|---|
| **A. Local agent** — Claude Code / Codex / a script running on your own machine | You own the agent's config, so you hand the prompt and tool calls to the gateway directly (edit hooks / your own code). No traffic sniffing needed. | [Case A](#case-a--local-agent-on-your-machine) |
| **B. Cloud / API agent** — the agent runs on a provider and you drive it over an API | You do **not** own the agent process, so you cannot add a local firewall around it or edit its internals. The gateway becomes the **tool/credential boundary** instead. | [Case B](#case-b--cloud--api-agent) |

### Prerequisites (both cases)

- **Python 3.12+** (developed and verified on 3.14).
- **This repository**, with the virtualenv installed:

```bash
git clone https://github.com/Aj1905/PAuthGateway.git && cd PAuthGateway && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

- **A shared auth token** so only your client can drive the daemon. Generate one and keep it:

```bash
export GATEWAY_AUTH_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

- For **Case A only**: admin/`sudo` on the machine (used *once*, for the network
  step) and an OS with a supported firewall — Linux `nftables`/`iptables` or
  macOS `pf`.

### Deploy the gateway (both cases)

Run the daemon. Bind it to loopback and require the token on every route:

```bash
.venv/bin/python gateway/serving/http_server.py --host 127.0.0.1 --port 8081 --auth-token "$GATEWAY_AUTH_TOKEN"
```

Keep it running (a `systemd` unit, `launchd` job, or `tmux` window is fine).
Useful flags: `--session-store PATH` to survive restarts, `--audit-log PATH` to
append permit/deny decisions as JSONL (place it where the agent user cannot read
it — it can quote values). Check liveness (this one route needs no token):

```bash
curl http://127.0.0.1:8081/health
```

> **Run the gateway as a *different* OS user than the agent.** The gateway has to
> reach the real SaaS; the egress rule in Case A deliberately does not apply to
> it. If the agent and the gateway share a user, the lockdown either breaks the
> gateway or is too loose to hold.

---

### Case A — local agent on your machine

Because the agent runs as *your* process, you don't need to intercept the LLM's
network traffic to recover the prompt. You **edit your own configuration** to
hand the clean prompt and each tool call to the gateway. For Claude Code this is
two hooks (no changes to Claude Code itself, no change to how you type prompts):

1. **Connect the agent to the gateway.** Add the two hook scripts to
   `~/.claude/settings.json` (or a project-local `.claude/settings.json`):

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

   `submit_prompt.sh` forwards the prompt **before** the model sees it (so the
   plan is built from the clean task); `pretool.sh` presents **every** tool call
   for a permit/deny check. Point them at the daemon with the same token:
   `export GATEWAY_URL=http://127.0.0.1:8081` and
   `export GATEWAY_AUTH_TOKEN=…`. Full options (strict vs log mode, planner
   choice) are in [`gateway/hooks/README.md`](gateway/hooks/README.md).

   *Not Claude Code?* Any local agent works the same way: from your own code,
   `POST` the prompt once, then `POST` each tool call, to
   `/sessions/<id>/messages` (schema in
   [`docs/self-hosting.md`](docs/self-hosting.md#prompt-capture-boundary)). You
   are editing code you control — no traffic interception.

2. **Create a dedicated non-admin agent user** (needs admin). This is the account
   the agent runs under:

   ```bash
   sudo useradd -m -s /bin/bash pauth-agent   # macOS: sysadminctl -addUser pauth-agent
   ```

3. **Restrict that user's egress to the gateway only** (needs admin, run once):

   ```bash
   sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 gateway/deploy/egress_lockdown.sh apply
   ```

   Now every outbound connection that user (or any process it spawns, including a
   hand-typed `curl`) makes can reach **only** `127.0.0.1:8081`. Anything else is
   dropped by the kernel — so a tool call that tries to skip the hook has nowhere
   to go, and anything that does reach the gateway hits a default-deny check.
   Verify and, when needed, undo:

   ```bash
   sudo AGENT_USER=pauth-agent gateway/deploy/egress_lockdown.sh status
   sudo AGENT_USER=pauth-agent gateway/deploy/egress_lockdown.sh remove
   ```

4. **Run the agent as that user** (e.g. `sudo -u pauth-agent claude`).

---

### Case B — cloud / API agent

Here the agent runs on a provider you don't control, and you drive it over an
API. Two things change:

- **You cannot use the OS egress lockdown of Step 4.** There is no local UID to
  pin — the agent isn't a process on your machine. The equivalent control moves
  into the environment the agent *does* run in: make the gateway the **only tool
  endpoint the agent can reach**, and broker credentials so the real SaaS
  tokens live only inside the gateway (the agent never holds a working
  credential to call SaaS directly). On a cloud VM you own, that means the VM's
  egress policy (security group / firewall) allows only the gateway.
- **Prompt capture is different.** You usually can't edit the hosted agent's
  internals, so instead of a hook you use a **gateway-owned entry point**: submit
  the task to the gateway *first* (`POST /sessions/<id>/messages` with the clean
  prompt), get back a session, then let the agent run — with its tool calls
  routed to the gateway as its tool/MCP endpoint. If the provider exposes a
  tool-call callback, forward each call to the same session.

The honest caveat: if a cloud agent can call arbitrary URLs and you cannot
constrain its egress or its credentials, a pure API relationship gives you
**observation and per-destination allow/deny, not full PAuth enforcement**. Treat
that as L1/L2, not L3. See
[`docs/self-hosting.md`](docs/self-hosting.md#setup-boundary) for the level
definitions and the reasoning.

---

### The one rule that voids everything: keep the agent non-admin

The egress lockdown (Case A) and the credential/egress boundary (Case B) are only
as strong as the agent's inability to remove them. **If the agent runs as
root / an admin / a `sudo`-capable user, injected code can flush the firewall
rule or reach SaaS directly and bypass the gateway entirely.** The lockdown
script refuses to apply to a privileged account for exactly this reason. If you
grant the agent admin, report the effective protection honestly as L1/L2.

---

## Design validity (reproduction experiment)

We demonstrate, in a measurable form, that the core algorithm holds with zero FP
/ zero FN exactly as in the paper. We reconstructed the paper's central claim —
*task-scoped authorization via NL slices and envelopes permits all benign tasks
(zero FP) and detects all injected illegitimate operations (zero FN)* — in a
form that can actually be measured and verified.

> **The measurement is honest.** The experiment runner does not hard-code FP/FN
> to 0. If the LLM generates wrong code, an FP appears; if a slice is inaccurate,
> an FN appears. The runner reports what actually happened (the `ANOMALIES`
> section).

### Experiment results (GPT-4.1, AgentDojo v1 + shopping)

Measured values from `python -m eval.fpfn --suites all`:

| Suite | #FN (#injection runs) | #FP (#benign runs) | A1 skipped |
|-------|----------------------|--------------------|------------|
| shopping | 0 (8) | 0 (2) | 0 |
| banking | 0 (135) | 0 (13) | 3 |
| slack | 0 (51) | 0 (7) | 14 |
| travel | 0 (32) | 0 (5) | 15 |
| workspace | 0 (164) | 0 (22) | 18 |
| **Overall** | **0 (390)** | **0 (49)** | **50** |

- **zero FP / zero FN** — Across all tasks where A1 generated grammar-conforming
  code that could be executed (benign 49 + forced injection 390 runs), both false
  positives and false negatives were 0. This reproduces the central claim of the
  paper's Table 2.
- **A1 skipped 50** — Tasks where GPT-4.1 generated code outside the restricted
  grammar (loops, comprehensions, method calls, multiple assignment, etc.).
  Rejected at the A1 gate, they never reach the enforcer. The paper reports that
  "GPT-4.1 generated correct code for all 100 tasks," whereas this
  implementation's A1 success rate is lower than that (presumably due to
  differences in prompt strictness and model snapshot).
- **code-crash 3** — Code that satisfies the grammar but crashes at runtime due
  to a logic bug (type misuse such as `str > int`). Not FP/FN; reported
  separately under `ANOMALIES`.

What this result shows is, as in the paper's sec. 5.2 — *if slices/rules are
derived correctly, zero FP and zero FN are a natural consequence of PAuth's
design.*

---

## Correspondence with the paper

| Paper | This implementation |
|------|----------|
| A1: imperative code generation (LLM, sec. 4.1.1) | `pauth/codegen.py` (OpenAI GPT-4.1, prompt from Appendix A) |
| A2: NL slice derivation (sec. 3.3 / 4.1.2, deterministic) | `pauth/slicing.py` |
| A3: rule compilation (Algorithm 1, deterministic) | `pauth/rules.py` |
| envelope (signed, sec. 3.4 / Fig. 3) | `pauth/envelope.py` |
| B1-B4: runtime enforcement (sec. 4.1.3, deterministic) | `pauth/enforcer.py` |
| restricted grammar (BNF from Appendix A) | `pauth/grammar.py` |
| implementation on AgentDojo (sec. 4.1) | `experiment/agentdojo_adapter.py` |
| Shopping suite (sec. 5.1) | `pauth/suites/shopping.py` |
| forced injection (sec. 5.1) | `experiment/forced_injection.py` |
| FP/FN evaluation (sec. 5.2, Table 2) | `eval/fpfn.py` |

As in the paper, **only A1 requires an LLM**; A2/A3/B1-B4 and the envelope are
fully deterministic (paper sec. 5.2: "The derivation of slices/rules ... is
deterministic without LLM").

---

## Setup (reproducing the paper experiments)

> To deploy the gateway in front of a real agent, see
> [Deploying the gateway in front of your agent](#deploying-the-gateway-in-front-of-your-agent).
> The steps below are only for running the reproduction experiments.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.12 or later recommended (developed and verified on 3.14).

---

## 1. Offline verification (no API key required)

Against the paper's worked example (banking sec. 5.3 / shopping sec. 4 · 5.3),
this verifies zero FP / zero FN of the deterministic core (A2/A3/B1-B4)
**without calling any API**.

```bash
.venv/bin/python -m tests.test_worked_examples
```

What is verified:
- that slice derivation matches the figures in the paper's sec. 5.3
- that all calls are permitted in benign execution (zero FP)
- that forced injection (illegitimate recipient / tampered amount / illegitimate
  operator) is all rejected (zero FN)
- that it works on the actual AgentDojo banking tools, environment, and pydantic
  objects

The Shopping suite ships with reference code, so it too can run without an API:

```bash
.venv/bin/python -m eval.fpfn --suites shopping
```

### Unexpected-attack probes (no API key required)

A test that throws attacks not derived from AgentDojo injection tasks directly at
the enforcer, under the precondition that a correct slice has already been
generated. In addition to Shopping, it also checks against the real tools of
AgentDojo banking / slack / travel / workspace:

```bash
.venv/bin/python -m tests.test_unexpected_attacks
```

Attacks verified:
- off-slice sensitive operator / read operator
- tampering of recipient, amount, subject, date, or product name
- direct call in a state where no upstream envelope exists
- enforcement of a call in a branch where the guard is false
- tampering of a signed envelope

Interpret the results strictly. Because PAuth is task-scope authorization, it can
reject off-slice attacks, but **a replay that exactly matches the legitimate
slice is permitted**. This is not an implementation bug; it is PAuth's
authorization boundary.

---

## 2. Full experiment (OpenAI API key required)

For the four AgentDojo suites (banking / slack / travel / workspace), this runs
A1 with **OpenAI GPT-4.1** and measures FP/FN in the paper's Table 2 format.

```bash
cp .env.example .env          # write OPENAI_API_KEY into .env
.venv/bin/python -m eval.fpfn --suites all
```

An environment variable also works, without `.env`:

```bash
OPENAI_API_KEY=sk-... .venv/bin/python -m eval.fpfn --suites all
```

**Options**

| Flag | Description |
|--------|------|
| `--suites all` | shopping + the 4 AgentDojo suites (default) |
| `--suites banking,shopping` | specify suites |
| `--limit N` | only the first N tasks of each suite (for cheap sanity checks) |
| `--model gpt-4.1` | A1's model (`gpt-5-mini` etc. also possible) |
| `--no-cache` | ignore cached generated code and regenerate |
| `--out path.json` | output path for the result JSON |

**Cost and time estimate**: about $0.002–0.04 per task (paper Fig. 10). Roughly
$1–4 and about 10 minutes for all 97 tasks. Generated code is cached under
`experiment/cache/`, so re-runs after the first are free.

To try cheaply first:

```bash
.venv/bin/python -m eval.fpfn --suites banking --limit 3
```

---

## How to read the output

```
Suite       #FN (#injection runs)     #FP (#benign runs)      A1 skipped
banking     0 (166)                   0 (16)                  0
...
Overall     0 (756)                   0 (97)                  0
```

- **#FP (#benign runs)** — number of benign-execution tasks in which some call
  was rejected.
- **#FN (#injection runs)** — number of forced injections that PAuth ended up
  permitting.
- **A1 skipped** — number of tasks that could not be evaluated due to a missing
  API key or a code-generation error.
- **ANOMALIES** — details of tasks where an FP/FN or a crash of generated code
  occurred. If this is empty, zero FP / zero FN holds.

Details are written to `experiment/results/results.json` (including per-task
slice, rejection reason, and token cost).

---

## How FP/FN is measured

- **FP (benign)**: The code generated by A1 is *actually executed*, and each tool
  call is passed through the enforcer. If even one is rejected, that task is an
  FP. Since the rules are derived from the same code, if the implementation is
  correct, FP should be 0 — and if one appears, it is a genuine signal of either
  A1's code quality or an inconsistency in the implementation.
- **FN (injection)**: A forced injection is "an illegitimate operation slipped
  into a benign task" (paper sec. 5.1). Given the envelope store after benign
  execution, an illegitimate call is presented to the enforcer. If any rule for
  the `tool` permits it, it is an FN. PAuth is default-deny (rejects if there is
  no exactly matching rule).
- Forced injection comes in 2 kinds: (1) operand tampering (recipient → attacker,
  or amount increased), (2) illegitimate operator (a sensitive call that
  AgentDojo's injection task carries).

The test harness is not vacuous: it is verified that passing an on-slice call as
an injection is permitted (i.e., detected as an FN).

---

## Structure

```
pauth/                  PAuth core mechanism (framework-independent, mostly deterministic)
  grammar.py            restricted-grammar parser / validator / dead-code elimination (Appendix A)
  slicing.py            A2: NL slice derivation
  rules.py              A3: rule compilation via Algorithm 1
  envelope.py           envelope data structure, HMAC signing, store
  evaluator.py          deterministic evaluator for slice expressions (including helpers len/min/max/first/last)
  enforcer.py           B1-B4: runtime enforcement + sandboxed executor
  codegen.py            A1: code generation via OpenAI (Appendix A prompt)
  pipeline.py           wiring of A1→A2→A3
  suites/shopping.py    the paper's Shopping suite (self-contained)
gateway/
  api_spec_monitor.py   report generation for OpenAPI-spec change detection / notification
  gateway.py            the plan once / enforce every call runtime boundary
  openapi_suite.py      automatic reflection of OpenAPI 3.x spec → SuiteSpec
  planner.py            swappable A1 strategy (deterministic recognizer / LLM free-form)
  agent_channel.py      JSON message boundary for the agent
  http_server.py        local HTTP daemon
docs/
  architecture.md       logical design (whole system)
  threat-model.md       defense boundary (in / out of scope)
  self-hosting.md       design boundaries of the self-hosted / network-connected versions
  ingress-design.md     ingress two-mode (SDK / interception) design
  planning-strategies.md A1 strategy catalog (dialogue structuring / dedicated model / formal analysis)
  design-status.md      organization of current design / under discussion / impossible / bottlenecks
  business-operations.md organization of OSS free scope / commercial operation / billing boundary
tests/experiment/
  agentdojo_adapter.py  normalizes the 4 AgentDojo suites to a common interface
  forced_injection.py   forced injection generation (sec. 5.1)
eval/
  fpfn.py               FP/FN experiment runner (Table 2 / Fig. 10)
  freeform.py           measurement runner for free-form A1
tests/
  test_worked_examples.py  offline zero-FP/FN verification (no API key required)
```

---

## Notes on reproduction scope

- **A1's model**: The paper primarily uses GPT-4.1, with partial evaluation of
  GPT-5-Mini / Gemini-3-Flash / Sonnet-4.5. This implementation supports only the
  OpenAI family by default (switchable with `--model`).
- **envelope signing**: In the paper's multi-host setup, signed envelopes are
  exchanged between servers. This implementation uses a single-host configuration
  matched to AgentDojo, attaching HMAC signatures to a shared-memory envelope
  store (faithful to the paper's sec. 4.1.3 configuration).
- **Shopping suite**: Since it is the paper's own suite, it is reconstructed
  self-contained based on the paper's examples (sec. 4 / 5.3) (2 tasks, reference
  code included).
- **forced injection count**: The paper hand-crafted 634 cases per task. Because
  this implementation auto-generates from AgentDojo's injection tasks and operand
  tampering, the count does not match (about 750), making for a broader search.
- AgentDojo `v1` task counts are banking 16 / slack 21 / travel 20 /
  workspace 40. This differs only slightly from the paper's tally (slack 19).
