"""Pins for the durable best-tree mark and the cross-process ratchet it makes
possible.

The defect these cover, measured on `anyhow` 2026-08-07: `.best_tree/stable`
held a tree passing 81 of a 108-test oracle when run `241132-f9fae0a6` ended.
Run `259637-36db9aad` resumed that tree, its crash retrigger deliberately
re-stubbed 56 bodies, and the 21-passing sweep that followed replaced the
stored tree — then 23, then 26 — while `_tdd_floor_stable.json` still read
`best_pass: 81`. The slot is heal's BEST_TREE_COPY tier, so the tier a
corrupted file is restored from had become the worse of the two trees on disk.

The cause was not the comparison but its seed: `_checkpoint_best`/
`_checkpoint_sha` began every process at `UNSET_PASS`/`None`, and `snapshot_due`
reads a `None` sha as "no snapshot yet" — true of the variables, false of the
directory.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from pipeline_core.application.workspace_lifecycle import (
    load_cluster_best_tree_mark,
    save_cluster_best_tree,
)
from pipeline_core.domain.best_tree_mark import UNKNOWN_TREE_SHA, BestTreeMark


def _filled(root: Path, cluster: str, body: str) -> Path:
    d = root / ".skeleton-filled" / cluster / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "error.rs").write_text(body, encoding="utf-8")
    return root / ".skeleton-filled" / cluster


def _mark(pass_count: int, sha: str = "sha", it: int = 1) -> BestTreeMark:
    return BestTreeMark(pass_count=pass_count, iteration=it, tree_sha=sha,
                        oracle_size=108)


# --- the record ------------------------------------------------------------

def test_mark_round_trips_through_the_slot(tmp_path) -> None:
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "a"),
                           _mark(81, "208bdf5ec9a2ec18", it=4))
    got = load_cluster_best_tree_mark(tmp_path, "stable")
    assert got == BestTreeMark(pass_count=81, iteration=4,
                               tree_sha="208bdf5ec9a2ec18", oracle_size=108)


def test_absent_slot_reads_as_none(tmp_path) -> None:
    assert load_cluster_best_tree_mark(tmp_path, "stable") is None


def test_corrupt_meta_reads_as_none_rather_than_raising(tmp_path) -> None:
    # This runs at loop entry; a corrupt state file must cost a fresh
    # comparison, never the run.
    slot = tmp_path / ".best_tree" / "stable"
    slot.mkdir(parents=True)
    (slot / "_meta.json").write_text("{not json", encoding="utf-8")
    assert load_cluster_best_tree_mark(tmp_path, "stable") is None


def test_meta_written_before_the_sha_existed_keeps_its_pass_count(tmp_path) -> None:
    # The live slot on 2026-08-07 carried {"pass_count": 26, "iteration": 4}
    # and nothing else. Refusing it whole would re-open the defect: the seed
    # would be empty and the next sweep would replace a tree it never compared
    # itself against. The count is the half worth honouring.
    slot = tmp_path / ".best_tree" / "stable"
    slot.mkdir(parents=True)
    (slot / "_meta.json").write_text('{"pass_count": 81, "iteration": 4}',
                                     encoding="utf-8")
    got = load_cluster_best_tree_mark(tmp_path, "stable")
    assert got is not None
    assert got.pass_count == 81
    assert got.tree_sha == UNKNOWN_TREE_SHA


def test_unknown_sha_never_equals_a_content_hash(tmp_path) -> None:
    # `snapshot_due` compares shas for equality. An unknown one must read as
    # "different tree" (re-snapshot, the safe direction), never as "same tree".
    from pipeline_core.domain.measurement import tree_content_sha
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("fn a() {}", encoding="utf-8")
    assert tree_content_sha(src) != UNKNOWN_TREE_SHA


# --- the ratchet -----------------------------------------------------------

def test_a_worse_sweep_cannot_replace_a_better_stored_tree(tmp_path) -> None:
    """The 81 -> 21 case, at the level the driver decides it.

    A fresh process seeds its comparison from the slot, so `snapshot_due`
    answers False for the 21-passing sweep instead of True-because-`None`."""
    from pipeline_core.application.reconstruct.tdd_ratchet import snapshot_due

    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "81"),
                           _mark(81, "208bdf5ec9a2ec18"))
    mark = load_cluster_best_tree_mark(tmp_path, "stable")
    assert mark is not None

    # What the driver now starts a process with...
    seeded_best, seeded_sha = mark.pass_count, mark.tree_sha
    assert not snapshot_due(21, seeded_best, "8666139309d108b1", seeded_sha)
    assert not snapshot_due(23, seeded_best, "6ec1d7c52c9a7f88", seeded_sha)
    assert not snapshot_due(26, seeded_best, "a9b50a7c6cedff58", seeded_sha)
    # ...and what it started them with before, which is the defect.
    assert snapshot_due(21, -1, "8666139309d108b1", None)

    # The climb back past the stored peak still lands.
    assert snapshot_due(83, seeded_best, "newsha", seeded_sha)


def test_equal_count_on_a_moved_tree_still_re_snapshots(tmp_path) -> None:
    # The rule `snapshot_due` exists for: an iter that lands work at an equal
    # pass count regressed nothing, so that work becomes the new baseline.
    # Seeding must not turn that into a refusal.
    from pipeline_core.application.reconstruct.tdd_ratchet import snapshot_due

    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "81"),
                           _mark(81, "208bdf5ec9a2ec18"))
    mark = load_cluster_best_tree_mark(tmp_path, "stable")
    assert mark is not None
    assert snapshot_due(81, mark.pass_count, "a-different-tree", mark.tree_sha)
    assert not snapshot_due(81, mark.pass_count, "208bdf5ec9a2ec18", mark.tree_sha)


def test_save_replaces_the_slot_so_the_guard_is_the_only_protection(tmp_path) -> None:
    # Stated as a pin because it is why the seed matters: the write is not a
    # merge. Whatever calls save owns the slot outright.
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "good"),
                           _mark(81))
    stale = tmp_path / ".best_tree" / "stable" / "src" / "gone.rs"
    stale.write_text("orphan", encoding="utf-8")
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "new"),
                           _mark(83))
    assert not stale.exists()
    assert (tmp_path / ".best_tree/stable/src/error.rs").read_text() == "new"


def test_other_clusters_survive_a_save(tmp_path) -> None:
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "s"),
                           _mark(81))
    save_cluster_best_tree(tmp_path, "nightly", _filled(tmp_path, "nightly", "n"),
                           _mark(100))
    assert (tmp_path / ".best_tree/stable/src/error.rs").read_text() == "s"
    assert load_cluster_best_tree_mark(tmp_path, "stable") is not None
    assert load_cluster_best_tree_mark(tmp_path, "nightly") is not None


# --- the slot is never populated-but-mark-less -----------------------------
#
# The save used to rmtree the slot, copy file by file, then write `_meta.json`
# LAST. A death anywhere in that window — the process this slot exists to
# survive — left a populated slot with no mark, and that is not a neutral
# state: `load_cluster_best_tree_mark` returns None, the driver seeds
# `_checkpoint_sha` to None, and `snapshot_due`'s "there is no snapshot yet"
# clause is TRUE of the variable while false of the directory. The next
# process's first sweep then claimed the slot however much worse it was.

def test_a_death_mid_save_does_not_leave_the_slot_without_a_mark(tmp_path,
                                                                 monkeypatch) -> None:
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "95"),
                           _mark(95, "968e763ff3f8cf37"))
    import shutil as _sh
    real_copy, calls = _sh.copy2, {"n": 0}

    def _die_partway(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk went away mid-snapshot")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(_sh, "copy2", _die_partway)
    filled = _filled(tmp_path, "stable", "13")
    (filled / "src" / "extra.rs").write_text("13", encoding="utf-8")
    with contextlib.suppress(OSError):        # the death this test simulates
        save_cluster_best_tree(tmp_path, "stable", filled, _mark(13, "42e333d7"))
    monkeypatch.undo()

    mark = load_cluster_best_tree_mark(tmp_path, "stable")
    assert mark is not None, "slot left mark-less — the next run claims it free"
    assert mark.pass_count == 95, "the 95 slot was replaced by a partial 13"
    assert (tmp_path / ".best_tree/stable/src/error.rs").read_text() == "95"


def test_an_empty_fill_tree_does_not_wipe_a_good_slot(tmp_path) -> None:
    """An empty tree is not evidence the stored one is stale."""
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "95"),
                           _mark(95))
    empty = tmp_path / "empty"
    (empty / "src").mkdir(parents=True)

    assert save_cluster_best_tree(tmp_path, "stable", empty, _mark(0)) == 0

    assert (tmp_path / ".best_tree/stable/src/error.rs").read_text() == "95"
    mark = load_cluster_best_tree_mark(tmp_path, "stable")
    assert mark is not None and mark.pass_count == 95


def test_best_tree_mark_from_dict_skips_malformed_payloads() -> None:
    """A half-written mark must cost the row, not the run."""
    assert BestTreeMark.from_dict(None) is None
    assert BestTreeMark.from_dict({"iteration": 1}) is None
    mark = BestTreeMark.from_dict(
        {"pass_count": 81, "iteration": "x", "oracle_size": "y",
         "missing_count": "z", "tree_sha": 12})
    assert mark is not None and mark.pass_count == 81
    assert mark.iteration == 0 and mark.oracle_size == 0
    assert mark.missing_count == 0


def test_no_staging_or_retired_dirs_survive_a_clean_save(tmp_path) -> None:
    """Heal walks `.best_tree/<cluster>`; a leftover sibling would shadow
    nothing but would be committed by the checkpoint's `git add`."""
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "a"),
                           _mark(81))
    save_cluster_best_tree(tmp_path, "stable", _filled(tmp_path, "stable", "b"),
                           _mark(83))

    assert [p.name for p in (tmp_path / ".best_tree").iterdir()] == ["stable"]
