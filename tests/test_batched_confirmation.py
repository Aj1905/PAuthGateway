"""Single-barrier confirmation: gated side-effects are deferred, collected, and
confirmed ONCE -- not gate-by-gate through the run. FN=0 is preserved (a rejected
action never executes); a non-gated side-effect whose result feeds a later call
still runs inline (no spurious deferral). Offline, no API key.
"""

from __future__ import annotations

import dataclasses

from pauth import prepare
from pauth.codegen import ToolDoc
from pauth.enforcer import Enforcer
from pauth.envelope import EnvelopeStore, KeyRing
from pauth.suites.base import SuiteSpec, ToolSpec
from gateway.runtime.confirmation import SourceTrust, broad_taint_map
from gateway.runtime.batched_confirmation import execute_with_batched_confirmation


# ---- a minimal two-action suite: read untrusted bills, then pay two of them ---

class PayEnv:
    def __init__(self):
        self.a, self.b = 100.0, 200.0
        self.paid: list = []
        self.confirm_time_paid: list | None = None


def _pay_runner(env):
    def run(tool, kwargs):
        if tool == "read_bills":
            return {"a": env.a, "b": env.b}
        if tool == "pay":
            env.paid.append((kwargs["recipient"], kwargs["amount"]))
            return {"ok": True}
        raise KeyError(tool)
    return run


def _tool(name, params, ret):
    return ToolSpec(name=name, params=params, signer="s",
                    doc=ToolDoc(name=name, description=name,
                               parameters=[{"name": p, "type": "string", "desc": p} for p in params],
                               returns=ret))


_PAY_TOOLS = {
    "read_bills": _tool("read_bills", [], "object {a: number, b: number}"),
    "pay": _tool("pay", ["recipient", "amount"], "object {ok: boolean}"),
}

PAY_PLAN = '''\
def run():
    bills = read_bills()
    pay("alice", bills.a)
    pay("bob", bills.b)
'''


def _armed_pay():
    suite = SuiteSpec(name="p", tools=_PAY_TOOLS, make_env=PayEnv,
                      runner_factory=_pay_runner, tasks=[])
    prepared = prepare(PAY_PLAN, suite.tool_names(), suite.tool_signer())
    env = suite.make_env()
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    trust = SourceTrust(untrusted_tools=frozenset({"read_bills"}),
                        confirm_untrusted_decisions=True)
    tmap = broad_taint_map(PAY_PLAN, {n: s.doc for n, s in _PAY_TOOLS.items()}, trust)
    return prepared, enf, env, suite, tmap


@dataclasses.dataclass
class _Recorder:
    """Confirms per a fixed verdict list; snapshots env.paid at first confirm so
    the test can prove NOTHING executed before the barrier."""
    verdicts: list
    env: PayEnv
    _i: int = 0

    def confirm(self, pending):
        if self.env.confirm_time_paid is None:
            self.env.confirm_time_paid = list(self.env.paid)
        v = self.verdicts[self._i] if self._i < len(self.verdicts) else False
        self._i += 1
        return v


@dataclasses.dataclass
class _AnnouncingRecorder(_Recorder):
    """Also records the handover announcement: its counts, how many confirms had
    happened by then, and what had been paid at that moment."""
    handovers: list = dataclasses.field(default_factory=list)

    def announce_handover(self, approved, rejected):
        self.handovers.append((approved, rejected, self._i, list(self.env.paid)))


def test_both_gated_pays_deferred_to_one_barrier():
    prepared, enf, env, suite, tmap = _armed_pay()
    conf = _Recorder([True, True], env)
    rep = execute_with_batched_confirmation(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(env),
        taint_map=tmap, docs={n: s.doc for n, s in _PAY_TOOLS.items()}, confirmer=conf)
    # both gated calls were collected BEFORE any confirmation (one barrier)
    assert len(rep.deferred) == 2
    assert env.confirm_time_paid == []            # nothing paid before the barrier
    assert env.paid == [("alice", 100.0), ("bob", 200.0)]  # both committed after approval
    assert not rep.deferred_dependency


def test_rejected_action_never_executes_fn0():
    prepared, enf, env, suite, tmap = _armed_pay()
    conf = _Recorder([True, False], env)          # approve alice, reject bob
    rep = execute_with_batched_confirmation(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(env),
        taint_map=tmap, docs={n: s.doc for n, s in _PAY_TOOLS.items()}, confirmer=conf)
    assert len(rep.deferred) == 2
    assert env.paid == [("alice", 100.0)]         # only the approved one ran
    assert [a.approved for a in rep.deferred] == [True, False]


def test_handover_announced_once_after_barrier_before_commit():
    prepared, enf, env, suite, tmap = _armed_pay()
    conf = _AnnouncingRecorder([True, False], env)
    execute_with_batched_confirmation(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(env),
        taint_map=tmap, docs={n: s.doc for n, s in _PAY_TOOLS.items()}, confirmer=conf)
    # announced exactly once, with the barrier's tally, after BOTH confirms and
    # before ANY commit -- so "no further confirmation" is true when displayed
    assert conf.handovers == [(1, 1, 2, [])]
    assert env.paid == [("alice", 100.0)]         # commit happened after the announce


# ---- non-gated dependency: create returns a trusted id used by share ---------

class DocEnv:
    def __init__(self):
        self.created: list = []
        self.shared: list = []


def _doc_runner(env):
    def run(tool, kwargs):
        if tool == "create_doc":
            env.created.append(kwargs["name"])
            return {"id": "doc-1"}
        if tool == "share_doc":
            env.shared.append((kwargs["doc_id"], kwargs["user"]))
            return {"ok": True}
        raise KeyError(tool)
    return run


_DOC_TOOLS = {
    "create_doc": _tool("create_doc", ["name"], "object {id: string}"),
    "share_doc": _tool("share_doc", ["doc_id", "user"], "object {ok: boolean}"),
}

DOC_PLAN = '''\
def run():
    doc = create_doc("notes")
    share_doc(doc.id, "bob")
'''


def test_non_gated_side_effect_with_result_dependency_runs_inline():
    suite = SuiteSpec(name="d", tools=_DOC_TOOLS, make_env=DocEnv,
                      runner_factory=_doc_runner, tasks=[])
    prepared = prepare(DOC_PLAN, suite.tool_names(), suite.tool_signer())
    env = suite.make_env()
    enf = Enforcer(prepared.rules, EnvelopeStore(KeyRing()), suite.tool_signer())
    trust = SourceTrust(untrusted_tools=frozenset(), confirm_untrusted_decisions=True)
    tmap = broad_taint_map(DOC_PLAN, {n: s.doc for n, s in _DOC_TOOLS.items()}, trust)

    class _NoConfirm:
        def confirm(self, pending):  # must never be called
            raise AssertionError("no gated action -> no confirmation expected")

        def announce_handover(self, approved, rejected):  # nothing was asked
            raise AssertionError("no barrier interaction -> no handover announce")

    rep = execute_with_batched_confirmation(
        prepared.source, enf, suite.tool_params(), suite.runner_factory(env),
        taint_map=tmap, docs={n: s.doc for n, s in _DOC_TOOLS.items()}, confirmer=_NoConfirm())
    assert rep.deferred == []                     # nothing gated -> nothing deferred
    assert env.created == ["notes"]
    assert env.shared == [("doc-1", "bob")]       # share saw create's real id (inline)
    assert not rep.deferred_dependency
