"""The joint-transaction half of the guidance screen: a pair's own revert
record renders for the SAME transaction and for nobody else.

The class (run `537093-ffcfd139`, 2026-09-01): a reverted pair left nothing
any later prompt could read, so its next dispatch was a temperature resample
of a byte-identical question. The durable record the pair now writes is read
back under the one screening every stored lesson goes through, with two new
stamps that make that read honest:

- `joint_members` — the member set of the transaction that earned the
  record. A regression-derived lesson read back by the SAME member set is
  exempt from `regression_scoped` (the joint acceptance gate re-judges the
  reddened tests, so they ARE tests the transaction answers for); a per-half
  read keeps today's withhold — that is the poison the rule exists for, and
  the on-disk `src/fmt.rs::*impl*::ErrorImpl` record (regressed[5], guidance
  prescribing the exact inverse of the paying shape) is the live instance,
  checked here VERBATIM from the recorded store.
- `joint_body_sha` — the kept member bodies the diagnosis was written
  against. The record's checkable premise (so `tree_premise_absent` does not
  retire it on an endgame tree with no stub callees), and its expiry: the
  moment any route lands a different body in either member, the state the
  lesson explains is gone (`joint_premise_moved`).
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.retro_gate import (
    JOINT_BODY_SHA,
    JOINT_MEMBERS,
    JOINT_PREMISE_MOVED,
    NO_TREE_PREMISE,
    REGRESSION_SCOPED,
    ConvictionEvidence,
    screen_guidance,
    tree_premise_absent,
)

_FIXTURES = Path(__file__).parent / "fixtures"

_SCOPE = frozenset({"lib.rs::fmt", "render.rs::render"})
_SHA = "ab12cd34ef56ab78"


def _joint_record(**over: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "category": "WRONG_APPROACH",
        "guidance": "Gate the extra section on the measured status so the "
                    "plain arm keeps its existing contract.",
        "signal": "ok",
        "error_count": 0,
        "failing_test_count": 2,
        "fully_exhausted": False,
        "convicted_by": ["t_a", "t_b"],
        "regressed": ["t_keep1", "t_keep2"],
        JOINT_MEMBERS: sorted(_SCOPE),
        JOINT_BODY_SHA: _SHA,
    }
    rec.update(over)
    return rec


def _screen(rec, *, members=frozenset(), sha=""):
    return screen_guidance(
        rec, "pub fn x() {}\n",
        evidence=ConvictionEvidence(frozenset({"t_a", "t_b"})),
        joint_members=members, joint_body_sha=sha)


def test_same_transaction_read_renders_despite_disjoint_regressed():
    """The pair's own record: regressed tests disjoint from the attributed
    evidence would retire it as regression_scoped, but the joint gate
    re-judges those tests, so the same member set reads it back."""
    v = _screen(_joint_record(), members=_SCOPE, sha=_SHA)
    assert v.retired_by == ""
    assert "Gate the extra section" in v.text


def test_a_per_half_read_keeps_the_regression_scoped_withhold():
    """No member scope passed (every non-joint injection site): the same
    record is a regression-derived prescription about tests the reading
    unit does not answer for — withheld, today's rule unchanged."""
    v = _screen(_joint_record())
    assert v.retired_by == REGRESSION_SCOPED
    assert v.text == ""


def test_a_different_member_set_is_not_the_same_transaction():
    v = _screen(_joint_record(),
                members=frozenset({"lib.rs::fmt", "other.rs::show"}),
                sha=_SHA)
    assert v.retired_by == REGRESSION_SCOPED


def test_a_moved_member_body_expires_the_lesson():
    """The premise is the kept bodies the diagnosis was written against;
    a different current sha means some route landed a body since — the
    state the lesson explains is gone."""
    v = _screen(_joint_record(), members=_SCOPE, sha="feed0000feed0000")
    assert v.retired_by == JOINT_PREMISE_MOVED
    assert v.text == ""


def test_the_joint_sha_is_a_tree_premise():
    """`tree_premise_absent` accepts the joint sha as the checkable premise
    (endgame trees stamp no stub blockers); without it the record is the
    ninth-way class exactly as before."""
    assert not tree_premise_absent(_joint_record(blocked_on=[]))
    bare = _joint_record(blocked_on=[])
    del bare[JOINT_BODY_SHA]
    del bare[JOINT_MEMBERS]
    # Without the joint stamps the same record is premise-less: regression
    # scope is exempted here (same evidence) to reach the ninth rule.
    assert tree_premise_absent(bare)
    v = screen_guidance(
        bare, "pub fn x() {}\n",
        evidence=ConvictionEvidence(frozenset({"t_keep1", "t_a", "t_b"})))
    assert v.retired_by == NO_TREE_PREMISE


def test_the_recorded_inverse_shape_record_is_withheld_verbatim():
    """The computed gate the plan demanded before Fix 8 could ship: the
    on-disk `src/fmt.rs::*impl*::ErrorImpl` record — regressed[5], convicted
    by the two backtrace leftovers, guidance prescribing the exact inverse
    of the paying shape — fed through the screens VERBATIM as the recorded
    store holds it. It carries no joint stamps, so a pair read (which passes
    its own member scope) still applies `regression_scoped`, and the record
    is withheld. If this ever renders, the screen is the next slice."""
    rec = json.loads(
        (_FIXTURES / "pair_inverse_record_stable.json").read_text())
    assert rec["regressed"], "fixture must be the regression-derived record"
    # The implementer's own evidence at the recorded read: the two leftovers.
    evidence = ConvictionEvidence(frozenset(rec["convicted_by"]))
    for scope in (frozenset(),
                  frozenset({"src/error.rs::fmt", "src/fmt.rs::debug"})):
        v = screen_guidance(rec, "pub fn debug() {}\n", evidence=evidence,
                            joint_members=scope, joint_body_sha="00ff00ff00ff00ff")
        assert v.text == "", v
        assert v.retired_by == REGRESSION_SCOPED
