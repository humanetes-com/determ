"""Stage A — the TDD loop floor survives a process restart, but only when the
measurement it describes is still the same measurement."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pipeline_core.domain.measurement import code_identity, combine_source_hashes
from pipeline_core.domain.tdd_floor import (
    UNSET_PASS,
    TddFloor,
    cursor_mismatch,
    render_pass_floor,
    resume_floor,
    resume_mismatch,
)

_FLOOR = TddFloor(cluster="nightly", best_pass=93, stall=2,
                  tree_sha="e6939c86", oracle_size=123, code_id="abc123def456")
_KEY = {"cluster": "nightly", "tree_sha": "e6939c86", "oracle_size": 123,
        "code_id": "abc123def456"}
_ID = {k: v for k, v in _KEY.items() if k != "tree_sha"}


def test_an_identical_measurement_resumes_both_halves() -> None:
    # Live evidence this is not hypothetical: run 1683765 iter 1 and run 2759
    # iter 1 both measured tree_sha=e6939c86 on the same 123-test oracle. The
    # key would have matched and legitimately carried the streak — instead
    # `nightly` scored exactly pass=93 fail=30 oracle=123 in three separate
    # processes across five sweeps and never exceeded stall 1/3, because a
    # process death sat in the gaps. task2a could not be marked done and the
    # run could not reach task2b, no matter how good the fills were.
    assert resume_mismatch(_FLOOR, **_ID) is None
    r = resume_floor(_FLOOR, **_KEY)
    assert (r.stall, r.best_pass) == (2, 93)


def test_a_changed_tree_still_carries_the_streak_but_not_the_peak() -> None:
    # THE fix. A floor is written straight after a sweep and the loop then
    # spends 25-224 min filling; the process dies in the fill phase, so the
    # next boot's tree is virtually never the saved one. Gating the STREAK on
    # tree_sha therefore permitted a resume only when the previous fill phase
    # produced no net change — the case where the streak matters least — and
    # refused it exactly when fills landed and the score did not move, which
    # is the genuine stall it exists to count. Live: run 589477 refused on
    # tree_sha (68f939adfae51660 != 6665a82428c800c3) after six minutes.
    r = resume_floor(_FLOOR, **{**_KEY, "tree_sha": "ff"})
    assert r.stall == 2
    # ...but the PEAK is not resumable across a changed tree: `_bratchet_snap`
    # is in-memory and died with its process, and `checkpoint_fill_tree` is
    # gated on a full oracle, which `nightly` never has. Adopting 93 here
    # would pin the loop under a ceiling nothing can restore — three stalls,
    # then the cluster checkpointed done at a regressed tree while claiming a
    # floor it never re-achieved.
    assert r.best_pass is None
    assert "tree_sha" in r.note


def test_a_changed_oracle_code_or_cluster_resumes_nothing() -> None:
    # A --clean restart re-runs task0c augmentation and the oracle changes
    # IDENTITY (111 -> 115 -> 124 -> 122 on this crate, ~1/3 of the names
    # differing). A streak carried across one would be meaningless.
    assert "oracle_size" in str(resume_mismatch(_FLOOR, **{**_ID, "oracle_size": 121}))
    # THE one that inverts the feature if omitted: restarts here exist mainly
    # to pick up a code fix. Resuming stall=2 across a restart that shipped a
    # fill improvement would exit the loop after one sweep and checkpoint the
    # cluster at the plateau the fix was meant to break.
    assert "code_id" in str(resume_mismatch(_FLOOR, **{**_ID, "code_id": "new"}))
    assert "cluster" in str(resume_mismatch(_FLOOR, **{**_ID, "cluster": "stable"}))
    for bad in ({"oracle_size": 121}, {"code_id": "new"}, {"cluster": "stable"}):
        r = resume_floor(_FLOOR, **{**_KEY, **bad})
        assert (r.stall, r.best_pass) == (None, None)


def test_a_changed_oracle_drops_the_score_halves_and_KEEPS_the_pass_cursor() -> None:
    """THE CURSOR IS NOT A SCORE, and routing it through `resume_mismatch`
    treated it as one.

    The cursor is what makes the flat-score guard's eager stop a PAUSE: the
    guard can stop a run having attempted 12 units of 59, and the relaunch has
    to open on units the stopped run never reached or `order_frontier`'s
    determinism turns the stop into a lock. `oracle_size` is `pass_count +
    fail_count`, so it moves whenever a test binary's crash state flips — one
    landed body did it on run `1920662-40f711b2` (filling `object_drop_front`
    took `tests/test_downcast.rs` from 0/7 to 4/3 and the oracle 101 -> 106),
    and 26 of 401 consecutive sweep pairs in the whole event store change it.
    Gated on that, the cursor was voided in roughly one stop in fifteen — in the
    exact transition it exists for."""
    saved = replace(_FLOOR, attempted=("src/error.rs::new", "src/kind.rs::new"))
    r = resume_floor(saved, **{**_KEY, "oracle_size": 121})
    assert (r.stall, r.best_pass) == (None, None), (
        "the SCORE halves must still die on a changed oracle")
    assert r.attempted == ("src/error.rs::new", "src/kind.rs::new")
    assert "pass cursor=2 unit(s)" in r.note
    # …and the ONE identity that describes the frontier's population still
    # voids it, because a different cluster is a different set of units.
    assert resume_floor(saved, **{**_KEY, "cluster": "stable"}).attempted is None
    assert cursor_mismatch(saved, cluster="nightly") is None
    assert "cluster" in str(cursor_mismatch(saved, cluster="stable"))
    # A moved code identity no longer voids the cursor — it TRIMS it, and the
    # trim is per unit (`test_cursor_lifetime.py` carries that argument and the
    # measurement behind it). Here only the shape: nothing landed and nothing
    # was declined on this floor, so every attempted unit is released.
    moved = resume_floor(saved, **{**_KEY, "code_id": "new"})
    assert moved.attempted == ()
    assert moved.released == ("src/error.rs::new", "src/kind.rs::new")


def test_landed_record_rides_the_cursor_and_an_old_floor_parses_empty() -> None:
    """The cursor marks ATTEMPTED at dispatch; the landed record is the
    subset that actually TOOK a semantic body change, and the pass audit
    reads it when the traversal closes. The 2026-08-19 crawl's completed pass
    is why the two must not be conflated: byte-identical re-authors, peeled
    fills and immutable-by-rule units advanced the cursor for roughly half
    the denominator while landing nothing, so a flat sweep after "43/43"
    refuted the dispatch for barely half the units it claimed. Spread over
    the one-batch-per-relaunch crawl, the record is only whole if it
    persists WITH the cursor, under the cursor's identity rule."""
    saved = replace(_FLOOR,
                    attempted=("src/error.rs::new", "src/kind.rs::new"),
                    landed=("src/error.rs::new",))
    # Round-trips…
    parsed = TddFloor.from_dict(saved.to_dict())
    assert parsed is not None and parsed.landed == ("src/error.rs::new",)
    # …an old floor (no key) parses empty, totally…
    old = dict(_FLOOR.to_dict())
    old.pop("landed")
    parsed = TddFloor.from_dict(old)
    assert parsed is not None and parsed.landed == ()
    # …and it follows the CURSOR's identity rule exactly: rides an oracle or
    # tree change, dies with the cluster.
    assert resume_floor(saved, **_KEY).landed == ("src/error.rs::new",)
    assert resume_floor(
        saved, **{**_KEY, "oracle_size": 121}).landed == ("src/error.rs::new",)
    assert resume_floor(
        saved, **{**_KEY, "tree_sha": "ffffffff"}).landed == ("src/error.rs::new",)
    assert resume_floor(saved, **{**_KEY, "cluster": "stable"}).landed is None
    # A moved code identity keeps the landed record and drops the rest: the
    # landed unit IS the carried cursor, and the record has to survive with it
    # or the NEXT relaunch cannot tell a tried unit from an untried one.
    moved = resume_floor(saved, **{**_KEY, "code_id": "new"})
    assert moved.landed == ("src/error.rs::new",)
    assert moved.attempted == ("src/error.rs::new",)


def test_no_saved_floor_is_a_fresh_start_not_a_crash() -> None:
    r = resume_floor(None, **_KEY)
    assert (r.stall, r.best_pass) == (None, None)
    assert "no persisted floor" in r.note


def test_a_corrupt_floor_costs_a_fresh_start_not_the_run() -> None:
    assert TddFloor.from_dict(_FLOOR.to_dict()) == _FLOOR
    for bad in (None, [], {}, {"cluster": "x"}, {**_FLOOR.to_dict(), "best_pass": "?"}):
        assert TddFloor.from_dict(bad) is None


def test_code_identity_covers_what_actually_runs() -> None:
    # `pipeline.py=<md5_8>` sat unchanged at bd16a271 through a session that
    # added 279 lines to retrigger_layout.py and rewrote three other modules:
    # the one file it covers is the composition root, which no longer holds the
    # logic. This identity must move when any of them does.
    assert combine_source_hashes([]) == combine_source_hashes([])
    a = combine_source_hashes([("pipeline.py", b"x"), ("pipeline_core/a.py", b"y")])
    # Order-independent...
    assert a == combine_source_hashes([("pipeline_core/a.py", b"y"), ("pipeline.py", b"x")])
    # ...but path-sensitive: moving a body between modules changes identity.
    assert a != combine_source_hashes([("pipeline.py", b"x"), ("pipeline_core/b.py", b"y")])
    assert a != combine_source_hashes([("pipeline.py", b"x"), ("pipeline_core/a.py", b"z")])


def test_code_identity_ignores_data_and_missing_trees(tmp_path: Path) -> None:
    (tmp_path / "pipeline_core").mkdir()
    (tmp_path / "pipeline.py").write_text("main()")
    (tmp_path / "pipeline_core" / "m.py").write_text("def f(): ...")
    before = code_identity(tmp_path)
    # A changed prompt fixture or state file is DATA: folding it in would
    # invalidate a resume for a reason that cannot change behaviour.
    (tmp_path / "pipeline_core" / "prompt.txt").write_text("hello")
    assert code_identity(tmp_path) == before
    # A changed module is not.
    (tmp_path / "pipeline_core" / "m.py").write_text("def f(): return 1")
    assert code_identity(tmp_path) != before
    # An empty root is still an answer, not a crash.
    assert code_identity(tmp_path / "nope") == combine_source_hashes([])


def test_an_unset_floor_never_reaches_a_reader_as_a_number() -> None:
    """`-1` is correct arithmetic and a lie in prose: it renders as a pass
    count. The sentinel stays for the comparison; the log line says `unset`."""
    assert render_pass_floor(UNSET_PASS) == "unset"
    assert render_pass_floor(-1) == "unset"
    assert render_pass_floor(0) == "0"
    assert render_pass_floor(112) == "112"


def test_the_sentinel_is_beaten_by_every_real_count() -> None:
    """What the sentinel is FOR: the first sweep improves without a special
    case, including a genuine zero."""
    assert UNSET_PASS < 0


# Sorted by test name: `from_dict` normalizes to sorted order, so a
# round-tripped floor compares equal only in the canonical spelling.
_EVIDENCE = (("test_boxed_str", ("src/error.rs::from",)),
             ("test_rev", ("src/chain.rs::next", "src/chain.rs::next_back")))


def test_futility_evidence_round_trips_and_an_old_floor_parses_empty() -> None:
    """The escalation's evidence must survive the process boundary — run
    `2773540-e47c3754` (2026-08-17) computed the escalated frontier on the
    exact sweep the flat-score guard stopped, and the process-local evidence
    died with it, so no relaunch could ever dispatch the escalated slice."""
    f = replace(_FLOOR, futile_units=_EVIDENCE)
    r = TddFloor.from_dict(f.to_dict())
    assert r is not None and r.futile_units == _EVIDENCE
    # A floor persisted by a pre-fix build has no key; `from_dict` stays total.
    old = dict(_FLOOR.to_dict())
    old.pop("futile_units")
    parsed = TddFloor.from_dict(old)
    assert parsed is not None and parsed.futile_units == ()
    # ...and so does a malformed one.
    bad = dict(_FLOOR.to_dict())
    bad["futile_units"] = ["not", "a", "dict"]
    parsed = TddFloor.from_dict(bad)
    assert parsed is not None and parsed.futile_units == ()


def test_futility_evidence_follows_the_cursor_identity_rule() -> None:
    """Like the cursor, the evidence records what this cluster's dispatches
    already established: it answers to the CLUSTER alone and self-validates
    at the live sweep (the escalation re-compares it against the CURRENT
    attribution and withholds on a stubbed accessor), so an oracle move, a
    tree change AND a code move must NOT void it — the flat-score stop, its
    whole reason to exist, is a code move every time (a validation relaunch
    ships a fix)."""
    f = replace(_FLOOR, futile_units=_EVIDENCE)
    # Identical measurement: everything resumes.
    assert resume_floor(f, **_KEY).futility == _EVIDENCE
    # A changed tree: the peak dies, the evidence rides.
    r = resume_floor(f, **{**_KEY, "tree_sha": "ffffffff"})
    assert r.best_pass is None and r.futility == _EVIDENCE
    # A changed oracle: the score halves die, cursor AND evidence ride.
    r = resume_floor(f, **{**_KEY, "oracle_size": 103})
    assert r.stall is None and r.futility == _EVIDENCE
    assert "futility evidence resumed" in r.note


def test_futility_survives_a_code_move_unconditionally() -> None:
    """THE FIX. The evidence describes the TREE ("rewriting these accessors
    moved no verdict"), not the pipeline build, so it rides across a
    `code_id` move on the cluster identity alone — no fingerprint gate. The
    stricter file-fingerprint premise this replaces never held once: the
    escalated producers live in the SAME file as the accessors they replace
    (`src/error.rs` holds `downcast`/`is`/`from` and the `context_*`/
    `object_*` chain fns), so the escalated dispatch the evidence exists to
    reach splices that file after the stamp and the next entry mismatches by
    construction — seven consecutive relaunches, zero resumes, each re-buying
    the 7-accessor traversal (2h03m/2h18m/4h02m in one night, runs
    `189964-ae3c1379` → `264297` → `349271`, 2026-08-25)."""
    f = replace(_FLOOR, futile_units=_EVIDENCE)
    r = resume_floor(f, **{**_KEY, "code_id": "a-different-build"})
    assert r.futility == _EVIDENCE
    assert "across the code move" in r.note
    # The live-sweep self-validation — a re-stubbed accessor or a changed
    # attributor firing nothing — is the tree-level premise, and it lives in
    # the escalation trigger, not here; it is pinned end-to-end in
    # tests/test_tdd_escalation_persistence.py and the walk.


def test_a_pre_fix_floor_that_stamped_futile_files_still_parses() -> None:
    """Forward-compat: a floor written by the build that persisted
    `futile_files` fingerprints resumes — the retired key is simply not
    read, and the evidence rides on `futile_units` alone."""
    old = dict(_FLOOR.to_dict())
    old["futile_units"] = {t: list(u) for t, u in _EVIDENCE}
    old["futile_files"] = {"src/error.rs": "bbbb"}
    parsed = TddFloor.from_dict(old)
    assert parsed is not None and parsed.futile_units == _EVIDENCE
    assert not hasattr(parsed, "futile_files")
    # And a round trip no longer writes the key at all.
    assert "futile_files" not in parsed.to_dict()


# --- the service record rides the floor ------------------------------------

def _floor_with(**kw):
    from pipeline_core.domain.tdd_floor import TddFloor
    base = {"cluster": "stable", "best_pass": 98, "stall": 0,
            "tree_sha": "t" * 16, "oracle_size": 105, "code_id": "c" * 12}
    base.update(kw)
    return TddFloor(**base)


def test_served_round_trips_and_an_old_floor_reads_empty() -> None:
    from pipeline_core.domain.tdd_floor import TddFloor
    f = _floor_with(served=("src/error.rs::downcast", "src/error.rs::msg"))
    d = f.to_dict()
    assert d["served"] == ["src/error.rs::downcast", "src/error.rs::msg"]
    assert TddFloor.from_dict(d) == f
    # A floor written before the field: parses as missing, not as empty.
    del d["served"]
    assert TddFloor.from_dict(d) is not None
    assert TddFloor.from_dict(d).served is None


def test_resume_carries_served_on_cluster_identity_and_none_for_an_old_floor() -> None:
    """The record `263474-b0e608d3` left: `futile_units` names
    `test_altdebug: [src/fmt.rs::debug]` from a fire on a unit it never
    dispatched. The resume hands the loop the service record SEPARATELY so
    the first-sweep fire is checked against dispatches, not against the
    evidence's own claim — and an old floor hands it None, so the loop
    falls back to the open cursor rather than trusting the claim."""
    from pipeline_core.domain.tdd_floor import resume_floor
    saved = _floor_with(
        served=("src/error.rs::downcast",),
        futile_units=(("test_altdebug", ("src/fmt.rs::debug",)),
                      ("test_downcast", ("src/error.rs::downcast",))))
    same = resume_floor(saved, cluster="stable", tree_sha="t" * 16,
                        oracle_size=105, code_id="c" * 12)
    assert same.served == ("src/error.rs::downcast",)
    moved = resume_floor(saved, cluster="stable", tree_sha="t" * 16,
                         oracle_size=105, code_id="d" * 12)
    assert moved.served == ("src/error.rs::downcast",)   # a dispatch is a dispatch
    assert moved.futility is not None
    other = resume_floor(saved, cluster="nightly", tree_sha="t" * 16,
                         oracle_size=105, code_id="c" * 12)
    assert other.served is None and other.futility is None
    old = resume_floor(_floor_with(), cluster="stable", tree_sha="t" * 16,
                       oracle_size=105, code_id="c" * 12)
    assert old.served is None


def test_empty_served_is_not_a_missing_field() -> None:
    """A kill mid-fill persists `served: []` — the field is present and empty,
    not absent. Collapsing that into the cursor fallback would count the
    attempted units as served and re-open the `308884` → `2932` fire."""
    from pipeline_core.domain.tdd_floor import TddFloor, resume_floor
    f = _floor_with(served=(), attempted=("src/a.rs::fmt",))
    d = f.to_dict()
    assert d["served"] == []
    parsed = TddFloor.from_dict(d)
    assert parsed is not None and parsed.served == ()
    r = resume_floor(parsed, cluster="stable", tree_sha="t" * 16,
                     oracle_size=105, code_id="c" * 12)
    assert r.served == ()
    assert r.attempted == ("src/a.rs::fmt",)


_PRODUCT = (
    ("t2",
     ("src/a.rs::fmt",),
     (("src/b.rs::producer", ("t2",)),),
     ("src/b.rs::producer",)),
)


def test_escalation_products_round_trip_and_an_old_floor_reads_empty() -> None:
    from pipeline_core.domain.tdd_floor import TddFloor
    f = _floor_with(escalation_products=_PRODUCT)
    d = f.to_dict()
    assert TddFloor.from_dict(d) == f
    del d["escalation_products"]
    assert TddFloor.from_dict(d) is not None
    assert TddFloor.from_dict(d).escalation_products == ()


def test_resume_carries_products_on_cluster_identity() -> None:
    from pipeline_core.domain.tdd_floor import resume_floor
    saved = _floor_with(escalation_products=_PRODUCT,
                        served=("src/a.rs::fmt",))
    same = resume_floor(saved, cluster="stable", tree_sha="t" * 16,
                        oracle_size=105, code_id="c" * 12)
    assert same.products == _PRODUCT
    moved = resume_floor(saved, cluster="stable", tree_sha="t" * 16,
                         oracle_size=105, code_id="d" * 12)
    assert moved.products == _PRODUCT
    other = resume_floor(saved, cluster="nightly", tree_sha="t" * 16,
                         oracle_size=105, code_id="c" * 12)
    assert other.products is None


def test_malformed_product_entries_parse_as_empty() -> None:
    """A floor written with a non-dict body or non-dict additions must not
    take down resume — skip those entries."""
    from pipeline_core.domain.tdd_floor import TddFloor
    raw = _floor_with().to_dict()
    raw["escalation_products"] = {
        "t_bad": "not-a-dict",
        "t_adds": {"earned": [], "additions": ["not", "a", "dict"],
                   "producers": []},
        "t_ok": {"earned": ["src/a.rs::fmt"],
                 "additions": {"src/b.rs::producer": ["t_ok"]},
                 "producers": ["src/b.rs::producer"]},
    }
    parsed = TddFloor.from_dict(raw)
    assert parsed is not None
    names = {t for t, *_ in parsed.escalation_products}
    assert names == {"t_ok"}


# --- the verified service view rides beside the inclusive record -----------

def test_served_verified_round_trips_and_never_falls_back_to_served() -> None:
    """The inclusive `served` is contaminated for the life of the floor —
    before the route-scoped decode the loop stamped every batch unit served
    (run `9933-efe68f37` iter 1: 12 stamped, `wanted` was `{context, msg}`),
    the stamps are monotone, and no later record distinguishes them (the
    ledger rows of the same window grade the same lie). The ranking's view
    therefore parses ONLY its own field: an old floor reads None and the
    never-served bucket starts empty and re-earns, instead of trusting a
    stamp nothing can verify."""
    from pipeline_core.domain.tdd_floor import TddFloor, resume_floor
    f = _floor_with(served=("src/a.rs::x", "src/a.rs::y"),
                    served_verified=("src/a.rs::x",))
    d = f.to_dict()
    assert d["served_verified"] == ["src/a.rs::x"]
    assert TddFloor.from_dict(d) == f
    r = resume_floor(f, cluster="stable", tree_sha="t" * 16,
                     oracle_size=105, code_id="c" * 12)
    assert r.served == ("src/a.rs::x", "src/a.rs::y")
    assert r.served_verified == ("src/a.rs::x",)
    # Negative control — the pre-split floor: `served` present, no verified
    # field. The fire's view keeps every stamp; the ranking's view is None,
    # NOT a copy of `served`.
    del d["served_verified"]
    parsed = TddFloor.from_dict(d)
    assert parsed is not None and parsed.served_verified is None
    old = resume_floor(parsed, cluster="stable", tree_sha="t" * 16,
                       oracle_size=105, code_id="c" * 12)
    assert old.served == ("src/a.rs::x", "src/a.rs::y")
    assert old.served_verified is None


def test_served_verified_survives_a_code_move_and_dies_with_the_cluster() -> None:
    from pipeline_core.domain.tdd_floor import resume_floor
    saved = _floor_with(served=("src/a.rs::x",),
                        served_verified=("src/a.rs::x",))
    moved = resume_floor(saved, cluster="stable", tree_sha="t" * 16,
                         oracle_size=105, code_id="d" * 12)
    assert moved.served_verified == ("src/a.rs::x",)
    other = resume_floor(saved, cluster="nightly", tree_sha="t" * 16,
                         oracle_size=105, code_id="c" * 12)
    assert other.served_verified is None
