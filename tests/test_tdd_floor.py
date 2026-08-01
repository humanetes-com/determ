"""Stage A — the TDD loop floor survives a process restart, but only when the
measurement it describes is still the same measurement."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.measurement import code_identity, combine_source_hashes
from pipeline_core.domain.tdd_floor import (
    UNSET_PASS,
    TddFloor,
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
