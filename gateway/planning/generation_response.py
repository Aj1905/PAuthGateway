"""Provider response validation and bounded refusal fallback for code generation."""
from __future__ import annotations
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class GenerationFailure(RuntimeError):
    def __init__(self, model: str, reason: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.model, self.reason = model, reason
        self.prompt_tokens, self.completion_tokens = prompt_tokens, completion_tokens
        super().__init__(f'generation: model={model} stop_reason={reason}')


def call_generator(client: Any, model: str, messages: list[dict[str, str]]) -> tuple[str, int, int]:
    if model.lower().startswith('claude'):
        response = client.messages.create(model=model, max_tokens=4096,
            system='\n\n'.join(m['content'] for m in messages if m['role'] == 'system'),
            messages=[m for m in messages if m['role'] != 'system'])
        usage = response.usage
        pt, ct = getattr(usage, 'input_tokens', 0) or 0, getattr(usage, 'output_tokens', 0) or 0
        reason = getattr(response, 'stop_reason', None)
        text = ''.join(b.text for b in response.content if getattr(b, 'type', '') == 'text')
        if reason == 'refusal':
            raise GenerationFailure(model, 'refusal', pt, ct)
        if reason not in (None, 'end_turn', 'stop_sequence'):
            raise GenerationFailure(model, str(reason), pt, ct)
    else:
        response = client.chat.completions.create(model=model, messages=messages, max_completion_tokens=4096)
        usage = response.usage
        pt, ct = getattr(usage, 'prompt_tokens', 0) or 0, getattr(usage, 'completion_tokens', 0) or 0
        if not response.choices:
            raise GenerationFailure(model, 'empty_choices', pt, ct)
        choice = response.choices[0]
        reason = getattr(choice, 'finish_reason', None)
        if getattr(choice.message, 'refusal', None) or reason == 'content_filter':
            raise GenerationFailure(model, 'refusal', pt, ct)
        if reason not in (None, 'stop'):
            raise GenerationFailure(model, str(reason), pt, ct)
        text = choice.message.content or ''
    if not text.strip():
        raise GenerationFailure(model, 'empty_response', pt, ct)
    return text, pt, ct


class GenerationSession:
    """Track failures and per-model costs across repair turns."""
    def __init__(self, model: str, client: Any, *, fallback_model: str | None,
                 resolve_client: Callable, fallback_client: Any = None,
                 generate: Callable = call_generator):
        if fallback_model == model:
            raise ValueError('fallback_model must differ from the primary model')
        self.model, self.client = model, client
        self.fallback_model, self.fallback_client = fallback_model, fallback_client
        self.resolve_client, self.generate = resolve_client, generate
        self.failure_history: list[str] = []
        self.usage: dict[str, list[int]] = {}
        self.calls = 0
        self._switched = False

    def _account(self, pt: int, ct: int) -> None:
        counts = self.usage.setdefault(self.model, [0, 0])
        counts[0] += pt
        counts[1] += ct

    def __call__(self, messages: list[dict[str, str]]) -> tuple[str, int, int]:
        spent_pt = spent_ct = 0
        while True:
            self.calls += 1
            try:
                text, pt, ct = self.generate(self.client, self.model, messages)
            except GenerationFailure as exc:
                self._account(exc.prompt_tokens, exc.completion_tokens)
                spent_pt += exc.prompt_tokens
                spent_ct += exc.completion_tokens
                self.failure_history.append(str(exc))
                logger.warning('%s', exc)
                if exc.reason != 'refusal' or self._switched or not self.fallback_model:
                    raise
                self._switched = True
                self.model = self.fallback_model
                self.client = self.resolve_client(self.model, self.fallback_client)
                continue
            self._account(pt, ct)
            return text, pt + spent_pt, ct + spent_ct

    def cost(self, price: Callable) -> float | None:
        costs = [price(model, *counts) for model, counts in self.usage.items()]
        return None if any(c is None for c in costs) else sum(costs)
