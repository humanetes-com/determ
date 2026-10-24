"""What a red test owes, and when nothing new can be asked
(`domain/candidate_tiers`).

The recorded fixture is run `14141-9a904822`'s fill-8 slice map with the
baked book's reach: 16 of 21 leftovers failed through `src/ensure.rs::render`,
which no opened run wrote from fill 3 to fill 8, while shared functions such
as `src/error.rs::construct` were rewritten every fill."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.candidate_tiers import (
    UNREAD,
    AskLedger,
    ask_fingerprint,
    culprits,
    due_reading,
    evidence_text,
    feature_reach,
    merge_slices,
    render_failed,
    tiers_of,
)
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.test_sight import (
    Sight,
    SightReading,
    SightSource,
    UnseenReason,
)

_FIX = json.loads((Path(__file__).parent / "fixtures" / "owed"
                   / "14141-9a904822_fill8.json").read_text())
_REACH = {u: frozenset(fs) for u, fs in _FIX["reach"].items()}
_SLICES = {t: frozenset(us) for t, us in _FIX["slices"].items()}


def _owed(tests: list[str], ledger: AskLedger, *,
          evidence: dict[str, str] | None = None,
          held: dict[str, frozenset[str]] | None = None):  # type: ignore[no-untyped-def]
    return due_reading(tests, slices=_SLICES, reach=_REACH,
                       evidence=evidence or {}, held=held or {}, ledger=ledger)


def _ask_all(reading, ledger: AskLedger) -> None:  # type: ignore[no-untyped-def]
    for t, r in reading.by_test.items():
        for u, fp in r.owed.items():
            ledger.record(u, t, fp)


def test_the_first_owed_tier_of_an_ensure_test_holds_render_and_not_the_shared_spine() -> None:
    """Plan: a red test's tiers by reach; `construct` on an ensure slice is
    not due. Control: the whole slice (what 'due until green' would owe)
    holds `construct`, `drop` and Display `fmt`."""
    r = _owed(["test_atom"], AskLedger()).by_test["test_atom"]
    assert "src/ensure.rs::render" in r.owed
    assert "src/ensure.rs::__dispatch_ensure" in r.owed
    assert not {"src/error.rs::construct", "src/error.rs::drop",
                "src/error.rs::fmt"} & set(r.owed)
    assert {"src/error.rs::construct", "src/error.rs::fmt"} <= _SLICES["test_atom"]


def test_a_probe_test_owes_the_same_ensure_functions() -> None:
    t = "behaviour_probe_stable_ensure_option_equality_0"
    r = _owed([t], AskLedger()).by_test[t]
    assert {"src/ensure.rs::render", "src/ensure.rs::__dispatch_ensure"} <= set(r.owed)


def test_a_test_with_no_exclusive_function_still_reaches_its_candidate() -> None:
    """`test_boxed_str`: its features own no function alone, so the
    exclusive set is empty. Tiers still ask `construct_from_boxed` — first,
    since its reach (two features) is the narrowest on the slice — and then
    the unexercised `from_boxed`. Control: the exclusive set is empty."""
    for feat in ("anyhow!", "downcast_ref"):
        assert not (set(_SLICES["test_boxed_str"]) & culprits(feat, _REACH))
    ledger = AskLedger()
    seen: list[set[str]] = []
    for _ in range(6):
        reading = _owed(["test_boxed_str"], ledger)
        r = reading.by_test["test_boxed_str"]
        if not r.owed:
            break
        seen.append(set(r.owed))
        _ask_all(reading, ledger)
    assert seen[0] == {"src/error.rs::construct_from_boxed"}
    assert seen[1] == {"src/error.rs::from_boxed"}


def test_altdebug_asks_the_debug_dispatcher_before_display_fmt() -> None:
    ledger = AskLedger()
    order: list[str] = []
    for _ in range(10):
        reading = _owed(["test_altdebug"], ledger)
        r = reading.by_test["test_altdebug"]
        if not r.owed:
            break
        order.extend(sorted(r.owed))
        _ask_all(reading, ledger)
    assert order.index("src/fmt.rs::debug") < order.index("src/error.rs::fmt")


def test_tiers_are_never_empty_and_unexercised_functions_come_second() -> None:
    reach = {"a": frozenset({"F"}), "b": frozenset({"F", "G"}), "c": frozenset()}
    tiers = tiers_of(["a", "b", "c", "d"], reach)
    assert tiers == (frozenset({"a"}), frozenset({"c", "d"}), frozenset({"b"}))
    assert all(tiers)
    assert tiers_of([], reach) == ()


def test_a_held_test_owes_its_stub_first_and_render_only_once_it_runs() -> None:
    """The order: while every convicting test of `render` is
    held in the stubbed Display `fmt`, `fmt` is owed and `render` is not.
    Once the test runs past the stub (new evidence), `render` is owed and
    `fmt` is not owed again for that failure."""
    held = {"test_atom": frozenset({"src/error.rs::fmt"})}
    ledger = AskLedger()
    ev = {"test_atom": "panicked at src/error.rs: not implemented"}
    first = _owed(["test_atom"], ledger, held=held, evidence=ev)
    assert set(first.by_test["test_atom"].owed) == {"src/error.rs::fmt"}
    assert not first.exhausted
    _ask_all(first, ledger)
    ran = {"test_atom": "assertion `left == right` failed left: false right: true"}
    after = _owed(["test_atom"], ledger, evidence=ran).by_test["test_atom"]
    assert "src/ensure.rs::render" in after.owed
    assert "src/error.rs::fmt" not in after.owed


def test_a_test_still_held_at_a_spent_stub_is_spent_not_open_forever() -> None:
    """A stub asked under the test's evidence, the test still held there
    with the same words: nothing new can be asked for it."""
    held = {"test_atom": frozenset({"src/error.rs::fmt"})}
    ledger = AskLedger()
    r = _owed(["test_atom"], ledger, held=held)
    _ask_all(r, ledger)
    again = _owed(["test_atom"], ledger, held=held)
    assert again.exhausted and not again.units
    assert "held in still-stubbed src/error.rs::fmt" in render_failed(again)[0]


def test_nothing_new_to_ask_is_the_exit_and_a_new_observed_value_reopens_it() -> None:
    """The exit: every tier asked under the current evidence. A new
    observed value owes the tiers once more; the same one does not."""
    ledger = AskLedger()
    ev = {"test_atom": "thread 'test_atom' (111) panicked: left: false right: true"}
    for _ in range(10):
        reading = _owed(["test_atom"], ledger, evidence=ev)
        if reading.exhausted:
            break
        _ask_all(reading, ledger)
    assert reading.exhausted
    # Same evidence with a different thread id: still nothing new.
    same = {"test_atom": "thread 'test_atom' (999) panicked: left: false right: true"}
    assert _owed(["test_atom"], ledger, evidence=same).exhausted
    moved = {"test_atom": "thread 'test_atom' (5) panicked: left: \"a\" right: \"b\""}
    assert not _owed(["test_atom"], ledger, evidence=moved).exhausted


def test_an_oscillating_reading_does_not_reopen_a_state_already_asked() -> None:
    ledger = AskLedger()
    a, b = {"test_atom": "left: 1"}, {"test_atom": "left: 2"}
    for ev in (a, b):
        for _ in range(10):
            reading = _owed(["test_atom"], ledger, evidence=ev)
            if reading.exhausted:
                break
            _ask_all(reading, ledger)
    assert _owed(["test_atom"], ledger, evidence=a).exhausted


def test_a_red_test_with_no_slice_holds_the_cluster_open_for_its_misses_then_is_named() -> None:
    """A red test nothing attributes keeps the cluster open while the next
    sweeps may still attribute it; after `misses` sweeps unread under the
    same evidence it is named as never offered, and does not hold the
    cluster open forever."""
    ledger = AskLedger()
    tests = ["test_atom", "test_nobody_runs"]
    for _ in range(12):
        reading = _owed(tests, ledger)
        if reading.exhausted:
            break
        _ask_all(reading, ledger)
        for t in reading.unread:
            ledger.miss(UNREAD, t, reading.fingerprints[t])
    assert reading.exhausted
    assert reading.unread_spent == {"test_nobody_runs"} and not reading.unread
    assert any("test_nobody_runs: no function attributed" in line
               for line in render_failed(reading))


def test_the_crate_function_a_failure_is_inside_is_the_last_slice_fallback() -> None:
    seen = SightReading({"t": Sight(
        "t", frozenset({SightSource.FAILURE_SITE}),
        frozenset({FnSite("src/a.rs", "f", "")}))})
    sl = merge_slices({}, {}, ["t"], sight=seen)
    assert sl == {"t": frozenset({"src/a.rs::f"})}
    assert merge_slices({}, {"t": ["src/b.rs::g"]}, ["t"],
                        sight=seen) == {"t": frozenset({"src/b.rs::g"})}


def test_an_unseen_test_owes_no_guess() -> None:
    """Run `1947461-b4eb65c3` (2026-10-24): the frontier was the
    recently-filled ring attributed to every failing test, and the fallback
    made it the 35-unit slice of a test nothing observed. An unseen test
    gets no slice and lands on UNREAD. Negative control: sight not taken
    keeps the attributed slice, which is the pre-change reading."""
    unseen = SightReading({"t": Sight(
        "t", reason=UnseenReason.NOT_BUILT_IN_PROBED_WORLD)})
    ring = {"t": ["src/ensure.rs::render", "src/fmt.rs::write_str"]}
    assert merge_slices({}, ring, ["t"], sight=unseen) == {}
    reading = due_reading(["t"], slices=merge_slices({}, ring, ["t"], sight=unseen),
                          reach={}, evidence={"t": "x"}, held={},
                          ledger=AskLedger())
    assert reading.unread == {"t"} and not reading.by_test
    old = SightReading(unseen.by_test, taken=False)
    assert merge_slices({}, ring, ["t"], sight=old) == {
        "t": frozenset(ring["t"])}


def test_asks_are_recorded_only_for_owed_functions_a_route_opened() -> None:
    reading = _owed(["test_atom"], AskLedger())
    render = FnLocator("src/ensure.rs", "render")
    asks = reading.asks({render, FnLocator("src/error.rs", "construct")})
    assert [u for u, _t, _fp in asks] == ["src/ensure.rs::render"]


def test_the_ledger_resets_on_a_moved_oracle_and_round_trips() -> None:
    led = AskLedger(oracle_fp="o1")
    led.record("u", "t", ask_fingerprint("x"))
    assert AskLedger.from_dict(led.to_dict()).asked("u", "t", ask_fingerprint("x"))
    assert led.for_oracle("o1") is led
    assert not led.for_oracle("o2").asked("u", "t", ask_fingerprint("x"))


def test_evidence_noise_is_masked_and_real_values_are_not() -> None:
    assert evidence_text("thread 'x' (123) at 0xdeadbeef") == "thread 'x' at 0x_"
    assert ask_fingerprint("left: 1") != ask_fingerprint("left: 2")


def test_culprits_are_the_functions_only_one_features_examples_run() -> None:
    class _S:
        def __init__(self, bid: str, ex: set[str], feats: tuple[str, ...]) -> None:
            self.behaviour_id, self.exercises, self.features = bid, ex, feats
    specs = [_S("a", {"render", "fmt"}, ("ensure!",)),
             _S("b", {"fmt", "msg"}, ("anyhow!",))]
    reach = feature_reach(specs)
    assert culprits("ensure!", reach) == {"render"}
    assert culprits("anyhow!", reach) == {"msg"}


def test_an_empty_suite_splits_the_unexercised_slice_by_spread() -> None:
    """With no reach at all every slice was one tier: 35 of 35 functions of
    the fill-8 map owed at once, every one past the cursor and every writer
    forced open. Split by how many red slices hold each function, each red
    test first owes the functions only its own slice holds."""
    reading = due_reading(sorted(_SLICES), slices=_SLICES, reach={},
                          evidence={}, held={}, ledger=AskLedger())
    union = set().union(*_SLICES.values())
    assert len(reading.units) < len(union)
    atom = reading.by_test["test_atom"]
    # Computed on the fixture: the slice's spread-1 functions, then `render`
    # with the four others the ensure probe also runs; the shared spine
    # (`construct`, `drop`, `vtable`, Display `fmt`) is the last tier.
    assert set(atom.owed) == {"src/chain.rs::len", "src/ptr.rs::read"}
    assert "src/ensure.rs::render" in atom.tiers[1]
    assert {"src/error.rs::construct", "src/error.rs::vtable",
            "src/error.rs::fmt"} <= atom.tiers[-1]
    spread = {u: sum(u in sl for sl in _SLICES.values()) for u in union}
    for r in reading.by_test.values():
        assert {spread[u] for u in r.owed} == {min(spread[u] for u in
                                                    set().union(*r.tiers))}


def test_only_a_landing_asks_and_misses_spend_after_the_bound() -> None:
    """One offer that landed nothing does not spend the function; a landing
    does; `misses` offers with nothing landed do. A function the batch cut
    is neither."""
    render = FnLocator("src/ensure.rs", "render")
    dispatch = FnLocator("src/ensure.rs", "__dispatch_ensure")
    ledger = AskLedger()
    reading = _owed(["test_atom"], ledger)
    asks, misses = reading.answered(offered={render}, landed=set())
    assert not asks and [u for u, _t, _f in misses] == ["src/ensure.rs::render"]
    for u, t, fp in misses:
        ledger.miss(u, t, fp)
    assert "src/ensure.rs::render" in _owed(["test_atom"], ledger).by_test["test_atom"].owed
    for _ in range(2):
        for u, t, fp in _owed(["test_atom"], ledger).answered({render}, set())[1]:
            ledger.miss(u, t, fp)
    after = _owed(["test_atom"], ledger).by_test["test_atom"].owed
    assert "src/ensure.rs::render" not in after
    assert "src/ensure.rs::__dispatch_ensure" in after
    asks, misses = _owed(["test_atom"], ledger).answered({dispatch}, {dispatch})
    assert [u for u, _t, _f in asks] == ["src/ensure.rs::__dispatch_ensure"]


def test_a_reading_on_an_unchanged_tree_keeps_its_fingerprint() -> None:
    """Noise the mask misses (a hash order) cannot re-owe: the same tree
    reads the same fingerprint. A moved tree reads the new one."""
    ledger = AskLedger()
    a = _owed(["test_atom"], ledger, evidence={"test_atom": "{1, 2}"})
    led_tree = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                           evidence={"test_atom": "{1, 2}"}, held={},
                           ledger=ledger, tree="T1")
    _ask_all(led_tree, ledger)
    noisy = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                        evidence={"test_atom": "{2, 1}"}, held={},
                        ledger=ledger, tree="T1")
    assert noisy.fingerprints == led_tree.fingerprints
    assert noisy.by_test["test_atom"].owed_tier > led_tree.by_test["test_atom"].owed_tier
    moved = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                        evidence={"test_atom": "{2, 1}"}, held={},
                        ledger=ledger, tree="T2")
    assert moved.fingerprints != led_tree.fingerprints
    assert a.fingerprints  # the tree-less reading is unchanged behaviour


def test_a_test_the_original_fails_is_withdrawn_and_named() -> None:
    reading = due_reading(["test_atom", "test_boxed_str"], slices=_SLICES,
                          reach=_REACH, evidence={}, held={},
                          ledger=AskLedger(), withdrawn={"test_boxed_str"})
    assert set(reading.by_test) == {"test_atom"}
    assert reading.withdrawn == {"test_boxed_str"}
    only = due_reading(["test_boxed_str"], slices=_SLICES, reach=_REACH,
                       evidence={}, held={}, ledger=AskLedger(),
                       withdrawn={"test_boxed_str"})
    assert not only.exhausted and not only.units, \
        "only withdrawn tests left: the reference floor closes, not this exit"


def test_a_flat_score_with_an_unasked_tier_does_not_end_the_cluster() -> None:
    """The 49 -> 49 -> 108 control: the fail count is flat and the test's
    words are unchanged, but a tier is still unasked, so the cluster stays
    open until every tier is spent."""
    ledger = AskLedger()
    ev = {"test_atom": "left: false right: true"}
    first = _owed(["test_atom"], ledger, evidence=ev)
    _ask_all(first, ledger)
    second = _owed(["test_atom"], ledger, evidence=ev)
    assert not second.exhausted and second.units
    assert second.by_test["test_atom"].owed_tier > first.by_test["test_atom"].owed_tier


def test_the_ledger_round_trips_misses_and_tree_readings() -> None:
    led = AskLedger(oracle_fp="o")
    led.miss("u", "t", "f")
    led.settle("t", "T", "f")
    back = AskLedger.from_dict(led.to_dict())
    assert back.missed("u", "t", "f") == 1 and back.last == {"t": ("T", "f")}
    # The label-keyed joint set is gone: an old file's key is ignored and a
    # new file never writes it (a candidate is spent by its route row).
    old = AskLedger.from_dict({**led.to_dict(), "joint_repaired": ["pair a"]})
    assert "joint_repaired" not in old.to_dict()


def _row(label: str, reverted, newly_red, newly_green):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace
    return SimpleNamespace(label=label, reverted=reverted, newly_red=newly_red,
                           newly_green=newly_green)


def test_a_reverted_run_that_gained_is_owed_again_jointly_once() -> None:
    """The pair `src/error.rs::fmt + src/fmt.rs::debug` was reverted on runs
    `450922`, `486447` and `14141` for reddening a sibling; no record kept
    what it had turned green. A candidate's units are owed first, together,
    for the tests it gained, once; then the tiers resume."""
    from pipeline_core.domain.candidate_tiers import joint_candidates, joint_note
    rows = [_row("pair fmt+debug",
                 (("src/error.rs", "fmt"), ("src/fmt.rs", "debug")),
                 ("test_display_sibling",), ("tests/test_fmt.rs::test_altdebug",))]
    cands = joint_candidates(rows, ["test_altdebug"])
    assert len(cands) == 1 and cands[0].gained == {"test_altdebug"}
    ledger = AskLedger()
    r = due_reading(["test_altdebug"], slices=_SLICES, reach=_REACH,
                    evidence={}, held={}, ledger=ledger, joint=cands)
    assert set(r.by_test["test_altdebug"].owed) == {
        "src/error.rs::fmt", "src/fmt.rs::debug"}
    _ask_all(r, ledger)
    again = due_reading(["test_altdebug"], slices=_SLICES, reach=_REACH,
                        evidence={}, held={}, ledger=ledger, joint=cands)
    assert set(again.by_test["test_altdebug"].owed) != {
        "src/error.rs::fmt", "src/fmt.rs::debug"}, "once per candidate"
    note = joint_note(cands, "test_altdebug")
    assert "turned this test green" in note and "test_display_sibling" in note


def test_control_a_revert_with_no_gain_or_a_gain_no_longer_red_owes_nothing() -> None:
    from pipeline_core.domain.candidate_tiers import joint_candidates
    no_gain = [_row("file a", (("src/a.rs", "f"),), ("s",), ())]
    assert joint_candidates(no_gain, ["t"]) == ()
    not_red = [_row("file a", (("src/a.rs", "f"),), ("s",), ("u",))]
    assert joint_candidates(not_red, ["t"]) == ()
    kept = [_row("file a", (), ("s",), ("t",))]
    assert joint_candidates(kept, ["t"]) == ()


def test_the_union_gate_accepts_only_when_gains_and_losses_are_green() -> None:
    """Accept: the candidate's gains stayed green and its newly red tests
    came back. Refute: a gained test is red after (the joint answer lost the
    candidate's own gain). A test outside the candidate's gained and lost
    sets says nothing about it: the rest of the fill wrote other bodies.
    Control: an unrelated test going red neither accepts nor refutes more
    than the candidate's own tests decide."""
    from pipeline_core.domain.candidate_tiers import JointCandidate, joint_accept
    ok = joint_accept(gained={"t_gain"}, lost={"t_lost"},
                      after_green={"t_kept", "t_gain", "t_lost"})
    assert ok.accept and not ok.refute
    empty = joint_accept(gained=set(), lost={"t_lost"}, after_green={"t_kept"})
    assert not empty.accept and not empty.refute
    assert empty.why == "empty gained owes nothing"
    gain_lost = joint_accept(gained={"t_gain"}, lost={"t_lost"},
                             after_green={"t_lost"})
    assert gain_lost.refute and not gain_lost.accept
    open_ = joint_accept(gained={"t_gain"}, lost={"t_lost"},
                         after_green={"t_gain"})
    assert not open_.accept and not open_.refute
    unrelated = joint_accept(gained={"t_gain"}, lost={"t_lost"},
                             after_green={"t_gain", "t_lost"})
    assert unrelated.accept, "a green elsewhere turning red is not the joint's"
    bare = JointCandidate("pair", frozenset({"a"}), frozenset({"g"}),
                          frozenset({"l"}))
    same = JointCandidate("pair", frozenset({"a"}), frozenset({"g"}),
                          frozenset({"l"}), bodies={})
    with_body = JointCandidate("pair", frozenset({"a"}), frozenset({"g"}),
                               frozenset({"l"}), bodies={"a": "fn a() {}"})
    assert bare.digest == same.digest and with_body.digest != bare.digest
    relabelled = JointCandidate("session #4", frozenset({"a"}), frozenset({"g"}),
                                frozenset({"l"}))
    assert relabelled.digest == bare.digest, "a label is not an identity"


def test_the_ladder_asks_the_session_after_the_unit_route() -> None:
    """A unit landing leaves the function owed on the session rung. A
    session offer no route planned does not spend that rung and does not
    erase a unit-rung reason. A planned session that lands nothing does
    spend it, and the next tier follows. A one-rung ladder spends on the
    first landing."""
    from pipeline_core.domain.candidate_tiers import LADDER
    led = AskLedger()
    first = _owed(["test_atom"], led)
    t1 = first.by_test["test_atom"]
    for u, fp in t1.owed.items():
        led.record(u, "test_atom", fp, rung="unit")
    lad = due_reading(["test_atom"], slices=_SLICES, reach=_REACH, evidence={},
                      held={}, ledger=led, ladder=LADDER)
    r = lad.by_test["test_atom"]
    assert set(r.owed) == set(t1.owed) and set(r.rungs.values()) == {"session"}
    one = _owed(["test_atom"], led)
    assert set(one.by_test["test_atom"].owed) != set(t1.owed)
    for u, fp in r.owed.items():
        led.miss(u, "test_atom", fp, "compile", rung="unit")
        counts = [led.miss(u, "test_atom", fp, "not_planned", rung="session")
                  for _ in range(3)]
        assert counts == [1, 2, 3], "an unplanned offer is still an offer"
        assert led.reason(u, "test_atom", fp) == "compile", \
            "an unplanned session offer does not erase the unit-rung reason"
        assert led.route_absent(u, "test_atom"), \
            "a session never planned for its bound reads absent"
    nxt = due_reading(["test_atom"], slices=_SLICES, reach=_REACH, evidence={},
                      held={}, ledger=led, ladder=LADDER)
    assert nxt.by_test["test_atom"].owed_tier > r.owed_tier
    assert set(nxt.by_test["test_atom"].rungs.values()) == {"unit"}


def test_a_session_never_planned_still_ends_the_test() -> None:
    """Liveness: 50 session offers no route planned used to leave the
    function owed forever, so the cluster could not end. They now spend the
    rung after the bound, and the test is exhausted."""
    from pipeline_core.domain.candidate_tiers import LADDER
    sl = {"t": frozenset({"a::f"})}
    led = AskLedger()
    r = due_reading(["t"], slices=sl, reach={}, evidence={}, held={},
                    ledger=led, ladder=LADDER)
    fp = r.fingerprints["t"]
    led.record("a::f", "t", fp, rung="unit")
    for _ in range(50):
        led.miss("a::f", "t", fp, "not_planned", rung="session")
    assert due_reading(["t"], slices=sl, reach={}, evidence={}, held={},
                       ledger=led, ladder=LADDER).exhausted


def test_a_session_route_that_cannot_be_built_is_spent_once() -> None:
    from pipeline_core.domain.candidate_tiers import LADDER
    led = AskLedger()
    first = _owed(["test_atom"], led)
    owed = dict(first.by_test["test_atom"].owed)
    for u, fp in owed.items():
        for _ in range(3):
            led.miss(u, "test_atom", fp, "not_planned", rung="unit")
        led.note_absent(u, "test_atom", fp, "session")
    nxt = due_reading(["test_atom"], slices=_SLICES, reach=_REACH, evidence={},
                      held={}, ledger=led, ladder=LADDER)
    assert not set(nxt.by_test["test_atom"].owed) & set(owed)
    assert nxt.by_test["test_atom"].owed_tier > first.by_test["test_atom"].owed_tier


def test_a_held_stub_climbs_to_the_session_and_unplanned_offers_spend_it_by_the_bound() -> None:
    from pipeline_core.domain.candidate_tiers import LADDER
    led = AskLedger()
    held = {"test_atom": frozenset({"src/ensure.rs::render"})}
    first = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                        evidence={}, held=held, ledger=led, ladder=LADDER)
    assert first.by_test["test_atom"].rungs["src/ensure.rs::render"] == "unit"
    fp = first.by_test["test_atom"].owed["src/ensure.rs::render"]
    led.record("src/ensure.rs::render", "test_atom", fp, rung="unit")
    second = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                         evidence={}, held=held, ledger=led, ladder=LADDER)
    assert second.by_test["test_atom"].rungs["src/ensure.rs::render"] == "session"
    led.miss("src/ensure.rs::render", "test_atom", fp, "not_planned",
             rung="session")
    third = due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                        evidence={}, held=held, ledger=led, ladder=LADDER)
    assert third.by_test["test_atom"].rungs["src/ensure.rs::render"] == "session"
    for _ in range(2):
        led.miss("src/ensure.rs::render", "test_atom", fp, "not_planned",
                 rung="session")
    assert due_reading(["test_atom"], slices=_SLICES, reach=_REACH,
                       evidence={}, held=held, ledger=led, ladder=LADDER).exhausted


def test_a_repair_matches_the_folded_fingerprint_and_a_pending_digest_is_not_one() -> None:
    led = AskLedger()
    led.note_repair("src/a.rs::f", "t", "fp", "saw")
    assert led.repair_seen("src/a.rs::f", "t", "fp")
    assert led.repair_seen("src/a.rs::f", "t", "fp+joint")
    folded = AskLedger()
    folded.note_repair("src/a.rs::f", "t", "fp+extra", "saw")
    assert folded.repair_seen("src/a.rs::f", "t", "fp+other")
    assert not folded.repair_seen("src/a.rs::f", "t", "nope")
    pending = AskLedger()
    assert pending.note_compile_pending("src/a.rs::f", "t", "fp")
    assert pending.reason("src/a.rs::f", "t", "fp") == "compile"
    assert pending.missed("src/a.rs::f", "t", "fp") == 0
    assert not pending.repair_seen("src/a.rs::f", "t", "fp")
    assert not pending.note_compile_pending("src/a.rs::f", "t", "fp"), \
        "pending once per fingerprint: the caller charges the next one"
    assert pending.note_compile_pending("src/a.rs::f", "t", "fp2")


def test_a_withdrawn_test_is_named_when_the_cluster_fails() -> None:
    reading = due_reading(["t_orig"], slices={}, reach={}, evidence={}, held={},
                          ledger=AskLedger(), withdrawn={"t_orig"})
    assert any(line.startswith("t_orig: withdrawn:")
               for line in render_failed(reading))


def test_a_candidate_keeps_bodies_from_a_mapping_or_from_tuples() -> None:
    from pipeline_core.domain.candidate_tiers import joint_candidates

    class _Row(dict):
        label = "pair"
        reverted = (("src/a.rs", "f"),)
        newly_red = ("sib",)
        newly_green = ("t",)

    mapped = _Row({"candidate_bodies": {
        "src/a.rs::f": "fn f() {}", "src/a.rs::empty": ""}})
    (c,) = joint_candidates([mapped], ["t"])
    assert c.bodies == {"src/a.rs::f": "fn f() {}"}
    tuples = _row("pair", (("src/a.rs", "g"), ("src/a.rs", "h"),
                           ("src/a.rs", "skip")), ("sib",), ("t",))
    tuples.candidate_bodies = [
        ("src/a.rs", "g", "fn g() {}"),
        ("src/a.rs::h", "fn h() {}"),
        ("src/a.rs", "skip", ""),
        ("bare",),
    ]
    (c2,) = joint_candidates([tuples], ["t"])
    assert c2.bodies == {"src/a.rs::g": "fn g() {}", "src/a.rs::h": "fn h() {}"}


def _iteration_8_row(run_id: str = "4012759-5c5177c6"):  # type: ignore[no-untyped-def]
    """The route row of run `4012759-5c5177c6`, iteration 8, as recorded in
    `logs/route_ledger/anyhow/index.jsonl`: the session kept `from_boxed`,
    reverted `debug`, turned `test_debug` green and three display/debug
    tests red, and stored both bodies."""
    from types import SimpleNamespace
    return SimpleNamespace(
        label="session[4 test(s), 6 unit(s)] #0", run_id=run_id,
        oracle_fp="o", kept=(("src/error.rs", "from_boxed"),),
        reverted=(("src/fmt.rs", "debug"),),
        newly_green=("test_debug",),
        newly_red=("test_display_error_from_option_context_debug",
                   "test_display_error_from_with_context_display_only",
                   "wrapper_message_error_debug_alternate"),
        candidate_bodies={"src/error.rs::from_boxed": "FROM_BOXED",
                          "src/fmt.rs::debug": "DEBUG"},
        joint="", joint_outcome="", joint_repaired=False)


def test_the_seed_holds_only_the_bodies_the_run_reverted() -> None:
    """Fill 9 of run `4012759-5c5177c6` re-spliced the kept `from_boxed` over
    itself and printed "taken back" for a write that changed nothing. The
    candidate's seed is the reverted body alone."""
    from pipeline_core.domain.candidate_tiers import joint_candidates
    (c,) = joint_candidates([_iteration_8_row()], ["test_debug"])
    assert c.bodies == {"src/fmt.rs::debug": "DEBUG"}
    assert c.units == frozenset({"src/fmt.rs::debug"})


def test_a_candidate_outlives_its_process_and_is_spent_by_its_digest() -> None:
    """Rows of an earlier run under the same oracle still owe the candidate;
    the same answer recorded by two runs is one candidate; a judged joint
    row with its digest spends it, and an unmeasured one does not."""
    from types import SimpleNamespace

    from pipeline_core.domain.candidate_tiers import joint_candidates, joint_spent
    rows = [_iteration_8_row("old-run"), _iteration_8_row("older-run")]
    (c,) = joint_candidates(rows, ["test_debug"])
    assert not joint_spent(rows, c.digest, "o")
    unmeasured = SimpleNamespace(joint=c.digest, oracle_fp="o",
                                 joint_outcome="unmeasured", reverted=(),
                                 newly_red=(), newly_green=())
    assert not joint_spent([*rows, unmeasured], c.digest, "o")
    judged = SimpleNamespace(joint=c.digest, oracle_fp="o",
                             joint_outcome="gains_held", reverted=(),
                             newly_red=(), newly_green=())
    assert joint_spent([*rows, judged], c.digest, "o")
    assert not joint_spent([*rows, judged], c.digest, "other oracle")


def test_a_joint_row_is_not_itself_a_candidate() -> None:
    from pipeline_core.domain.candidate_tiers import joint_candidates
    row = _iteration_8_row()
    row.joint = "jdeadbeef"
    assert joint_candidates([row], ["test_debug"]) == ()


def test_a_compile_reason_from_an_earlier_evidence_state_does_not_count_now() -> None:
    led = AskLedger()
    led.miss("u", "t", "old_fp", "compile")
    assert led.reason_on("u", "t", "old_fp") == "compile"
    assert led.reason_on("u", "t", "old_fp+L123") == "compile", "a fold of it"
    assert led.reason_on("u", "t", "new_fp") == "", \
        "a new evidence state does not inherit the old diagnostic"


def test_the_latest_reverted_body_is_the_one_kept() -> None:
    from pipeline_core.domain.fill_attempts import FillAttempts
    att = FillAttempts.__new__(FillAttempts)
    import threading
    att._lock = threading.Lock()
    att._bodies = {}
    att.remember_bodies({"src/a.rs::f": "FIRST"})
    att.remember_bodies({"src/a.rs::f": "SECOND", "src/a.rs::g": ""})
    assert att.remembered_bodies([FnLocator("src/a.rs", "f"),
                                  FnLocator("src/a.rs", "g")]) == {
        "src/a.rs::f": "SECOND"}


def test_the_joint_verdict_word_classifies_the_end_reading() -> None:
    from pipeline_core.domain.candidate_tiers import joint_outcome_of
    kw = {"gained": {"t_gain"}, "lost": {"t_lost"}}
    assert joint_outcome_of(after_green={"t_gain", "t_lost"}, **kw) == "accepted"
    assert joint_outcome_of(after_green={"t_gain"}, **kw) == "gains_held"
    assert joint_outcome_of(after_green={"t_lost"}, **kw) == "refuted"
