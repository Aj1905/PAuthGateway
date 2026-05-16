"""PAuth -- Precise Task-Scoped Authorization for Agents.

A faithful reimplementation of the deterministic core of PAuth (Sharma, Jiang,
Lin & Chen, arXiv:2603.17170): NL-slice derivation, Algorithm-1 rule
compilation, signed envelopes and the runtime enforcer.  The LLM-dependent
A1 step lives in :mod:`pauth.codegen`.
"""

from .enforcer import (
    CallEvent,
    Decision,
    Enforcer,
    ExecReport,
    check_injection,
    execute_generated_code,
)
from .envelope import Envelope, EnvelopeStore, KeyRing, flatten, make_envelope, verify
from .evaluator import Evaluator, NotConcretizable, values_match
from .grammar import (
    RestrictedGrammarError,
    parse_and_validate,
    strip_dead_code,
    validate_semantics,
)
from .pipeline import PreparedTask, prepare
from .rules import Rule, compile_rules
from .slicing import Slice, derive_slices

__all__ = [
    "CallEvent",
    "Decision",
    "Enforcer",
    "Envelope",
    "EnvelopeStore",
    "Evaluator",
    "ExecReport",
    "KeyRing",
    "NotConcretizable",
    "PreparedTask",
    "RestrictedGrammarError",
    "Rule",
    "Slice",
    "check_injection",
    "compile_rules",
    "derive_slices",
    "execute_generated_code",
    "flatten",
    "make_envelope",
    "parse_and_validate",
    "prepare",
    "strip_dead_code",
    "validate_semantics",
    "values_match",
    "verify",
]
