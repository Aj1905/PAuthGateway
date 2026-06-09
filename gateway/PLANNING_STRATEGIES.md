# Planning strategies

The gateway must not bet the product on one prompt-to-code method. PAuth's
stable boundary is:

```text
user intent -> restricted imperative run() code -> pauth.prepare() -> rules
```

Everything before `pauth.prepare()` is replaceable A1 strategy.

## Strategy Names

Use these canonical names in config, JSON messages, or environment variables:

| Name | Status | Meaning |
|---|---|---|
| `deterministic` | implemented | Regex recognizer for known prompt patterns. |
| `llm-freeform` | implemented | General LLM A1 with grammar repair and optional judge. |
| `interactive-structuring` | registered | Clarification loop before code generation. |
| `specialized-codegen` | registered | Dedicated imperative-code model plus validator retries. |
| `formal-semantic` | registered | Formal NL parser / semantic analysis path. |

Default selection is `deterministic`.

Runtime selection:

```bash
PAUTH_PLANNER_STRATEGY=llm-freeform
PAUTH_PLANNER_SUITE=shopping
PAUTH_PLANNER_MODEL=gpt-4.1
PAUTH_PLANNER_MAX_RETRIES=3
```

The registered-but-unimplemented strategies intentionally reject with a clear
message. They are strategy slots, not silent fallbacks.

## Strategy 1: Interactive Structuring

Use a guided "Grill me" style interaction before A1 generation.

Flow:

1. Start from the user's raw natural-language task.
2. Ask targeted questions for missing operands, conditions, tools, dates,
   quantities, recipients, and disambiguation.
3. Produce a structured task prompt or structured intent object.
4. Pass that structured representation to a code generator.
5. Validate the generated imperative code with `pauth.prepare()`.

This is useful when the input task is underspecified or ambiguous. It is also
good for early product work because it makes missing intent visible instead of
letting a model silently invent values.

Hard constraint: this strategy requires a user-interaction surface before
planning. It is not the same as "network config only" for an already-running
agent unless the gateway owns a user-facing prompt step.

Failure mode:

- It can become a form-filling product disguised as an agent firewall.
- If the questions are too broad, users will give vague answers and the planner
  still fabricates details.
- If the resulting structured prompt is not auditable, the strategy only moves
  hallucination from code generation into prompt rewriting.

Design implication:

- Keep the interactive collector outside the PAuth core.
- Treat its output as another planner input.
- Store the raw prompt, questions, answers, final structured prompt, generated
  code, and validation result.

## Strategy 2: Specialized Imperative-Code Model

Train or tune a model whose only job is to emit restricted imperative `run`
code from the user's task plus tool schemas.

Flow:

1. Provide the user task and available tool schemas.
2. Specialized model emits `def run(...): ...`.
3. Run the deterministic validator: grammar, semantic checks, slicing, rule
   compilation.
4. If validation fails, feed the exact validator error back and retry.
5. If retry budget is exhausted, reject the task.

If this works, the gateway does not need elaborate prompt-template logic. The
complexity moves into model training/evaluation and a small deterministic
repair loop.

This strategy is attractive because the runtime architecture stays simple:

- one generation call;
- one validator;
- bounded retries;
- default-deny on failure.

The uncomfortable part is data. Without enough high-quality
`prompt + tool schema -> restricted run() code` examples, this strategy is just
wishful thinking with a nicer label. The model must be evaluated on exact intent
capture, not only grammar validity.

Failure mode:

- Grammar-valid code that drops user intent.
- Code that satisfies common benchmarks but fails on real user prompts.
- Training data that overfits to toy suites and does not transfer to real MCP
  tools.

Design implication:

- Keep validator feedback model-agnostic.
- Log every failed generation and validator error as training data.
- Separate grammar success metrics from intent-faithfulness metrics.

## Strategy 3: Formal Natural-Language Analysis

Reduce the LLM's role as much as possible by formalizing the natural-language
prompt and translating it through syntactic/semantic analysis.

Candidate shape:

1. Restrict the accepted task language.
2. Parse the prompt with a formal grammar, for example categorial grammar or a
   related compositional semantic parser.
3. Map parsed semantic forms to tool actions, operands, guards, and data
   dependencies.
4. Emit restricted imperative `run` code.
5. Validate with `pauth.prepare()`.

This is the most intellectually clean direction, but it is also the least
product-ready. It only becomes practical if the accepted task language is
deliberately narrow or the product can tolerate explicit controlled language.

Failure mode:

- Real user prompts fall outside the grammar.
- The grammar grows into an unmaintainable pile of exceptions.
- Coverage looks good on hand-picked examples and collapses on production
  language.
- Ambiguity resolution quietly becomes another hidden LLM-like component.

Design implication:

- Keep it as a research slot, not as the default path.
- Use it for narrow high-value domains where templates and formal semantics are
  realistic.
- Measure rejection rate separately from correctness. A formal parser that
  rejects many prompts can still be valuable if accepted prompts are reliable.

## Current Implementation Mapping

Current concrete planners:

- `DeterministicRecognizerPlanner`: strict baseline for known prompt patterns.
- `LLMFreeformPlanner`: general model plus grammar repair and optional judge.

Planned strategy slots:

- `InteractiveStructuringPlanner`: wraps a user-facing clarification session
  and then delegates to another code generator.
- `SpecializedCodegenPlanner`: calls a specialized imperative-code model and
  relies mainly on validator feedback for retries.
- `FormalSemanticPlanner`: parses controlled natural language into a semantic
  form and emits restricted imperative code.

Both planned strategies must still return restricted imperative code and pass
through `pauth.prepare()`. No strategy gets to emit rules directly.
