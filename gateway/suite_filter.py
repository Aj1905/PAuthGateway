"""Plan-time suite filtering.

Plan generation (A1) hands the LLM the schemas of every registered
tool. With one or two suites this is fine; with twenty MCPs the prompt
balloons, the LLM gets distracted, and grammar violations rise. This
module narrows the universe to a likely-relevant subset before A1 sees
it.

The default implementation is intentionally cheap and deterministic:
score each suite by overlapping bag-of-words tokens between the user
prompt and the suite's tool descriptions, keep the top-k. There is no
LLM in the filter -- the goal is to *cut* tokens, not to add a second
model that itself needs caching, retries, and grammar discipline.

The selection function is pluggable. A deployment that wants an LLM
filter, an embedding filter, or a hardcoded set per user can plug a
different ``SuiteFilter`` in via the gateway config.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Callable

from pauth.suites.base import SuiteSpec

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in",
    "on", "for", "with", "at", "by", "from", "as", "is", "are", "be", "been",
    "being", "do", "does", "did", "have", "has", "had", "will", "would",
    "should", "may", "might", "can", "could", "must", "shall", "this", "that",
    "these", "those", "i", "me", "my", "you", "your", "we", "our", "they",
    "their", "it", "its", "please", "kindly", "send", "get", "make",
})


def _tokens(text: str) -> set[str]:
    return {tok.lower() for tok in _TOKEN_RE.findall(text or "") if tok.lower() not in _STOPWORDS and len(tok) > 1}


def _suite_keywords(suite: SuiteSpec) -> set[str]:
    """Bag of tokens drawn from every tool name + description in the suite."""
    bag: set[str] = set()
    for spec in suite.tools.values():
        bag.update(_tokens(spec.name))
        bag.update(_tokens(spec.doc.description))
        for p in spec.doc.parameters:
            bag.update(_tokens(p.get("name", "")))
            bag.update(_tokens(p.get("desc", "")))
    return bag


@dataclasses.dataclass
class SuiteScore:
    name: str
    score: int
    overlap: list[str]


@dataclasses.dataclass
class FilterResult:
    selected: list[str]              # source-suite names retained
    scores: list[SuiteScore]         # all suites, sorted descending
    reason: str                      # human-readable for logs


class SuiteFilter:
    """Pluggable filter: ``(prompt, suites) -> selected source names``."""

    def __init__(
        self,
        top_k: int | None = None,
        min_score: int = 1,
        scorer: Callable[[str, SuiteSpec], int] | None = None,
    ) -> None:
        """``top_k`` retains the highest-scoring N suites (None = all that meet
        ``min_score``). ``scorer`` lets a deployment override the default
        bag-of-words overlap with something fancier."""
        self._top_k = top_k
        self._min_score = max(0, min_score)
        self._scorer = scorer

    def filter(self, prompt: str, suites: dict[str, SuiteSpec]) -> FilterResult:
        prompt_tokens = _tokens(prompt)
        scores: list[SuiteScore] = []
        for name, suite in suites.items():
            if self._scorer is not None:
                score = self._scorer(prompt, suite)
                overlap: list[str] = []
            else:
                bag = _suite_keywords(suite)
                overlap_set = prompt_tokens & bag
                score = len(overlap_set)
                overlap = sorted(overlap_set)
            scores.append(SuiteScore(name=name, score=score, overlap=overlap))

        scores.sort(key=lambda s: s.score, reverse=True)

        retained = [s for s in scores if s.score >= self._min_score]
        if self._top_k is not None:
            retained = retained[: self._top_k]
        selected = [s.name for s in retained]

        if not selected:
            # If nothing scored, fall back to the whole universe rather
            # than crash plan generation. A deployment that prefers a
            # hard fail can subclass and override.
            selected = list(suites.keys())
            reason = "no suite scored above threshold; falling back to full universe"
        else:
            top = ", ".join(f"{s.name}={s.score}" for s in scores[:5])
            reason = f"top scores: {top}"

        return FilterResult(selected=selected, scores=scores, reason=reason)
