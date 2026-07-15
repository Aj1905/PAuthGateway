"""Test data split into three layers (L1 / L2 / L3).

The split matches what the system actually accepts/rejects at each layer:

* **L1 (``l1_prompts``)** -- ``(prompt, expected_accept)`` and grading hints.
  Used to measure the recognizer (gateway/planning/core.py) and the LLM A1 path
  (gateway/planning/agentic_a1.py + eval/freeform.py).

* **L2 (``l2_scenarios``)** -- ``(prompt, [(tool, args, expected_permit)])``.
  Used by the end-to-end gateway runner (eval/l2_replay.py): every
  scripted agent step asserts a verdict against the gateway's enforcer.

* **L3 (``l3_references``)** -- ``(reference_code, [forced_injections])``.
  Used to bypass A1 and validate the deterministic A2/A3/B1-B4 directly.
  Mirrors paper sec. 5.1 / 5.2.
"""
