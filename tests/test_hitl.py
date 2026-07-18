"""HITL confirmers + the gate-in-the-loop bench. Offline (the 'human' is a
policy/scripted confirmer)."""

from __future__ import annotations

from eval.hitl import SCENARIOS, _run
from gateway.runtime.confirmation import PendingConfirmation
from gateway.runtime.confirmer import (
    CautiousConfirmer,
    ScriptedConfirmer,
    TrustingConfirmer,
    VigilantConfirmer,
)

WEBSUM = SCENARIOS[0]
DINING = SCENARIOS[1]
BENIGN_TOTAL = WEBSUM.benign_value


def _pc(**kw):
    base = dict(confirmation_id="c", tool="send_money", param_index=1,
               param_name="amount", value=999.0)
    base.update(kw)
    return PendingConfirmation(**base)


def test_trusting_approves_vigilant_rejects_a_flagged_call():
    flagged = _pc(source=("read_site",))  # non-empty warning
    assert TrustingConfirmer().confirm(flagged) is True
    assert VigilantConfirmer().confirm(flagged) is False


def test_vigilant_approves_an_unflagged_call():
    clean = _pc(source=())  # no warning
    assert VigilantConfirmer().confirm(clean) is True


def test_scripted_confirmer_follows_its_answers_then_rejects():
    c = ScriptedConfirmer([True, False])
    assert c.confirm(_pc()) is True
    assert c.confirm(_pc()) is False
    assert c.confirm(_pc()) is False  # exhausted -> reject (fail-safe)


# -- the gate-in-the-loop outcomes -------------------------------------------

def test_trusting_completes_benign_but_lets_the_poison_through():
    benign, gated = _run(WEBSUM, False, TrustingConfirmer())
    assert gated and abs(benign - BENIGN_TOTAL) < 1e-6      # availability
    poison, _ = _run(WEBSUM, True, TrustingConfirmer())
    assert poison is not None and poison > BENIGN_TOTAL     # FN: attacker value executed


def test_vigilant_blocks_the_poison():
    poison, gated = _run(WEBSUM, True, VigilantConfirmer())
    assert gated and poison is None                          # security: blocked


def test_discerning_human_can_be_both_secure_and_available():
    # a human who reads the breakdown approves the benign total but rejects the
    # inflated one -- what the summands make possible (500 stands out).
    benign, _ = _run(WEBSUM, False, ScriptedConfirmer([True]))
    poison, _ = _run(WEBSUM, True, ScriptedConfirmer([False]))
    assert abs(benign - BENIGN_TOTAL) < 1e-6 and poison is None


def test_cautious_rejects_what_it_cannot_judge():
    from gateway.runtime.confirmation import BreakdownRow
    c = CautiousConfirmer()
    bd = ("sum", (BreakdownRow("a", 1.0), BreakdownRow("b", 2.0)))
    assert c.confirm(_pc(source=())) is True                            # unflagged -> ok
    assert c.confirm(_pc(source=("read_site",))) is False               # bare untrusted -> no basis
    assert c.confirm(_pc(source=("read_site",), breakdown=bd)) is True  # decomposition -> judgeable
    assert c.confirm(_pc(source=("llm_extract",), unverifiable=True,
                         breakdown=bd)) is False                        # unverifiable -> reject


def test_cautious_availability_tracks_ux_quality():
    # both scenarios now carry a breakdown (sum table / max candidate table), so a
    # cautious human can judge and approve the benign value in each -- the UX fix.
    assert _run(WEBSUM, False, CautiousConfirmer())[0] is not None
    assert _run(DINING, False, CautiousConfirmer())[0] is not None


def test_structured_display_has_the_six_fields():
    from gateway.runtime.confirmation import BreakdownRow, PendingConfirmation
    pc = PendingConfirmation(
        "c", "send_money", 1, "amount", 678.49, source=("read_site",),
        task_desc="Send money to a recipient account.",
        breakdown=("sum", (BreakdownRow("Design work", 120.0),
                           BreakdownRow("不明 (unknown)", 500.0))),
    )
    prod = pc.structured_display()
    for field in ("【何をするタスク】", "【どの情報が必要】", "【どこから取得した】",
                  "【取得情報一覧】", "【参照情報】"):
        assert field in prod
    assert "Send money" in prod and "不明 (unknown)" in prod
    assert "【ground truth" not in prod          # production omits it
    # benchmark mode appends the ground-truth line last
    bench = pc.structured_display(ground_truth="INJECTED: the 500 line is fake")
    assert "【ground truth（ベンチマークのみ）】" in bench and "INJECTED" in bench


def test_provenance_makes_a_bare_control_value_judgeable():
    # a bare value (no reduction) with its SOURCE surfaced as 参照すべき情報 gives a
    # cautious human a basis to research -> judgeable (was 0/9 on AgentDojo).
    from gateway.runtime.confirmation import PendingConfirmation
    c = CautiousConfirmer()
    bare = PendingConfirmation("c", "invite_user_to_slack", 0, "user_email",
                               "x@y.com", source=("get_webpage",))
    withprov = PendingConfirmation("c", "invite_user_to_slack", 0, "user_email",
                                   "x@y.com", source=("get_webpage",),
                                   provenance=(("get_webpage('www.site.com')",
                                                "My e-mail is x@y.com."),))
    assert c.confirm(bare) is False                    # no basis -> reject
    assert c.confirm(withprov) is True                 # a source to research -> judgeable
    w = withprov.human_warning()
    assert "参照すべき情報 (source" in w and "get_webpage" in w
    assert "INDEPENDENT channel" in w


def test_dining_decision_poison_reroutes_the_booking():
    # the untrusted rating inflation makes max() pick the attacker's restaurant;
    # a rubber-stamp books it (FN), a rejection blocks it.
    benign, gated = _run(DINING, False, TrustingConfirmer())
    assert gated and benign == "r_paris_3"                  # benign books the real best
    poison, _ = _run(DINING, True, TrustingConfirmer())
    assert poison == "r_paris_4"                            # FN: attacker's restaurant
    assert _run(DINING, True, VigilantConfirmer())[0] is None  # vigilant blocks it
