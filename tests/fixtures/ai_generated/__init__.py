"""AI-GENERATED test fixtures pending human review.

Everything in this package was produced by an LLM during a design session.
The cases are PLAUSIBLE-LOOKING, not vetted. They exist so a human reviewer
can scan them, keep the useful ones, and discard or rewrite the rest.

Conventions:

* Every collection name is prefixed ``AI_``.
* Every case carries ``ai_generated=True`` (or lives in this directory --
  the directory itself is the marker).
* The canonical hand-curated fixtures live in ``tests/fixtures/l1_*.py``,
  ``l2_*.py``, ``l3_*.py``. Promote a case by moving it there and
  dropping the ``AI_`` prefix.

Reviewer's checklist (informal):

* Does the prompt sound like something a real user would type?
* Is ``expected_accept`` actually what the implementation produces?
* For L2: does the attack vector match a realistic threat?
* For L3: does the ``reference_code`` compile via ``pauth.prepare``?
  Run ``python tests/fixtures/ai_generated/validate_l3.py`` to check.
"""
