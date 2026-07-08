# Planning strategies

The gateway must not bet the product on a single prompt-to-code method. PAuth's
stable boundary is as follows:

```text
user intent -> restricted imperative run() code -> pauth.prepare() -> rules
```

Everything before `pauth.prepare()` is a swappable A1 strategy.

## Strategy Names

In config, JSON messages, and environment variables, use these canonical names:

| Name | Status | Meaning |
|---|---|---|
| `deterministic` | implemented | Regex recognizer for known prompt patterns. |
| `llm-freeform` | implemented | A general-purpose LLM A1 with grammar repair and an optional judge. |
| `auto` | implemented | Recognizer fast-path, falling back to `llm-freeform` on a miss (the default main ingress strategy, S2; also known as `hybrid`). |
| `interactive-structuring` | registered | A clarification loop before code generation. |
| `specialized-codegen` | registered | A dedicated imperative-code model + validator retries. |
| `formal-semantic` | registered | A formal NL parser / semantic analysis pass. |

The default for the main ingress (`AgentChannel`) is `auto` (when
`PAUTH_PLANNER_STRATEGY` is unset). Without `PAUTH_PLANNER_SUITE`, `auto` has no
fallback target and has the same acceptance set as `deterministic`.
`Gateway.submit_user_prompt` uses `deterministic` directly.

Runtime selection:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform
PAUTH_PLANNER_SUITE=shopping
PAUTH_PLANNER_MODEL=gpt-4.1
PAUTH_PLANNER_MAX_RETRIES=3
```

A registered but unimplemented strategy is deliberately rejected with a clear
message. They are strategy slots, not silent fallbacks.

## Strategy 1: Interactive Structuring

Before A1 generation, use a guided, "Grill me"-style dialogue.

Flow:

1. Start from the user's raw natural-language task.
2. Ask targeted questions about missing operands, conditions, tools, dates,
   quantities, recipients, and disambiguation.
3. Produce a structured task prompt or a structured intent object.
4. Pass that structured representation to the code generator.
5. Validate the generated imperative code with `pauth.prepare()`.

This is useful when the input task is underspecified or ambiguous. It also suits
early product work, because it surfaces the missing intent rather than letting
the model silently fabricate values.

Hard constraint: this strategy requires a user-interaction surface before
planning. Unless the gateway owns the user-facing prompt step, it is not the same
as "network config only" for an agent that is already running.

Failure mode:

- It can become a form-filling product masquerading as an agent firewall.
- If the questions are too broad, the user gives vague answers and the planner
  still fabricates the details.
- If the resulting structured prompt is not auditable, this strategy merely moves
  hallucination from code generation to prompt rewriting.

Design implication:

- Keep the interactive collector outside the PAuth core.
- Treat its output as just another planner input.
- Store the raw prompt, the questions, the answers, the final structured prompt,
  the generated code, and the validation result.

## Strategy 2: Specialized Imperative-Code Model

Train or tune a model whose only job is to emit restricted imperative `run` code
from the user's task + tool schema.

Flow:

1. Provide the user task and the available tool schema.
2. The dedicated model emits `def run(...): ...`.
3. Run a deterministic validator: grammar, semantic checks, slicing, rule
   compilation.
4. On validation failure, retry with the exact validator error fed back.
5. When the retry budget is exhausted, reject the task.

If this works, the gateway needs no elaborate prompt-template logic. The
complexity moves to the model's training/evaluation and a small deterministic
repair loop.

This strategy is appealing because the runtime architecture stays simple:

- one generation call;
- one validator;
- bounded retries;
- default-deny on failure.

The uncomfortable part is the data. Without enough high-quality
`prompt + tool schema -> restricted run() code` examples, this strategy is just
wishful thinking with good labels. The model must be evaluated not only on
grammar validity but on exact intent capture.

Failure mode:

- Code that is grammatically valid but drops the user's intent.
- Code that satisfies common benchmarks but fails on real user prompts.
- Training data that overfits a toy suite and does not transfer to real MCP
  tools.

Design implication:

- Keep the validator feedback model-agnostic.
- Log every failed generation and validator error as training data.
- Separate the grammar-success metric from the intent-faithfulness metric.

## Strategy 3: Formal Natural-Language Analysis

Minimize the LLM's role as much as possible by formalizing the natural-language
prompt and translating it through syntactic/semantic analysis.

Candidate shape:

1. Restrict the accepted task language.
2. Parse the prompt with a formal grammar. For example, categorial grammar or a
   related compositional semantic parser.
3. Map the parsed semantic form to tool actions, operands, guards, and data
   dependencies.
4. Emit restricted imperative `run` code.
5. Validate with `pauth.prepare()`.

This is the most intellectually clean direction, but also the least
product-ready. It becomes practical only if you deliberately narrow the accepted
task language, or the product can tolerate an explicit controlled language.

Failure mode:

- Real user prompts fall outside the grammar.
- The grammar grows into an unmaintainable pile of exceptions.
- Coverage looks good on hand-picked examples and collapses on production
  language.
- Disambiguation quietly becomes yet another hidden LLM-like component.

Design implication:

- Keep it as a research slot, not the default path.
- Use it for narrow, high-value domains where templates and formal semantics are
  realistic.
- Measure the rejection rate separately from correctness. A formal parser that
  rejects many prompts is still valuable if the prompts it accepts are
  trustworthy.

## Current Implementation Mapping

Current concrete planners:

- `DeterministicRecognizerPlanner`: a strict baseline for known prompt patterns.
- `LLMFreeformPlanner`: a general-purpose model with grammar repair and an
  optional judge.
- `AutoPlanner`: recognizer fast-path, then fallback to `LLMFreeformPlanner`
  (S2).

Planned strategy slots:

- `InteractiveStructuringPlanner`: wraps a user-facing clarification session,
  then delegates to a separate code generator.
- `SpecializedCodegenPlanner`: calls a dedicated imperative-code model, relying
  mainly on validator feedback for retries.
- `FormalSemanticPlanner`: parses controlled natural language into a semantic
  form and emits restricted imperative code.

Both planned strategies must still return restricted imperative code and pass
through `pauth.prepare()`. No strategy is allowed to emit rules directly.
