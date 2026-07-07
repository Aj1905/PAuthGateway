"""Grill (HITL confirmation) end-to-end scenarios as regression tests."""

from eval.grill_scenario import (
    scenario_attack,
    scenario_legit,
    scenario_unattended,
)


def test_grill_blocks_attack_on_rejection():
    assert scenario_attack() is True


def test_grill_allows_legit_on_approval():
    assert scenario_legit() is True


def test_grill_fails_safe_when_unattended():
    assert scenario_unattended() is True
