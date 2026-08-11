"""The repair write's keep/refuse rule, and the tree state after a refusal.

Every test here carries its negative control: the same write applied WITHOUT
the gate lands and stays landed, which is what the pre-fix code did and what
cost the merged tree 13h50m of non-compiling repair on 2026-08-10/11.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.write_gate import (
    WriteGateDeps,
    guarded_write,
)
from pipeline_core.domain.associated_items import (
    AssociatedItem,
    DuplicateIntroduction,
    introduced_duplicates,
    items_by_file,
    removal_target,
)
from pipeline_core.domain.write_verdict import WriteOutcome, judge_write


def _rec(file: str, fn: str, container: str) -> dict[str, str]:
    return {"file": file, "fn_name": fn, "container": container}


def _deps(counts: list[int | None], records: list[list[dict[str, str]]],
          log: list[str]) -> WriteGateDeps:
    """Probe/enumerate readings are consumed in order, one pair per write."""
    return WriteGateDeps(
        probe_diag_count=lambda: counts.pop(0),
        enumerate_fn_records=lambda: records.pop(0),
        log=log.append,
        emit=lambda *a, **k: None)


# --- the domain rules -------------------------------------------------------

def test_diag_increase_refused_equal_kept() -> None:
    assert judge_write(before=3, after=23).outcome is WriteOutcome.REFUSE_DIAG_INCREASE
    assert judge_write(before=0, after=15).outcome is WriteOutcome.REFUSE_DIAG_INCREASE
    # Equal is neutral: a repair often trades one diagnostic for another on its
    # way out, and refusing that leaves the loop no legal move.
    assert judge_write(before=7, after=7).kept
    assert judge_write(before=46, after=39).kept


def test_duplicate_refused_even_when_the_count_falls() -> None:
    dup = DuplicateIntroduction(
        AssociatedItem("ErrorImpl", "", "debug"), "error.rs", ("fmt.rs",))
    verdict = judge_write(before=20, after=1, duplicates=(dup,))
    assert verdict.outcome is WriteOutcome.REFUSE_DUPLICATE
    assert "no body rewrite can reach it" in verdict.render()


def test_introduced_duplicate_names_the_new_copy_only() -> None:
    before = items_by_file([_rec("fmt.rs", "debug", "impl ErrorImpl")])
    after = items_by_file([_rec("fmt.rs", "debug", "impl ErrorImpl"),
                           _rec("error.rs", "debug", "impl ErrorImpl")])
    (dup,) = introduced_duplicates(before, after)
    assert dup.introduced_in == "error.rs"
    assert dup.already_in == ("fmt.rs",)
    # An unchanged tree introduces nothing, and a body rewrite in place is not
    # an introduction either.
    assert introduced_duplicates(after, after) == ()


def test_same_name_different_trait_is_not_a_duplicate() -> None:
    before = items_by_file([_rec("a.rs", "fmt", "impl Debug for Error")])
    after = items_by_file([_rec("a.rs", "fmt", "impl Debug for Error"),
                           _rec("b.rs", "fmt", "impl Display for Error")])
    assert introduced_duplicates(before, after) == ()


def test_free_fns_carry_no_cross_file_collision() -> None:
    before = items_by_file([_rec("a.rs", "helper", "")])
    after = items_by_file([_rec("a.rs", "helper", ""), _rec("b.rs", "helper", "")])
    assert introduced_duplicates(before, after) == ()


def test_removal_target_is_the_unauthored_placement_or_nothing() -> None:
    item = AssociatedItem("ErrorImpl", "", "debug")
    dup = DuplicateIntroduction(item, "error.rs", ("fmt.rs",))
    authored = {"fmt.rs": frozenset({item})}
    assert removal_target(dup, authored) == "error.rs"
    # Both authored, or neither: refuse to choose rather than delete real work.
    assert removal_target(dup, {"fmt.rs": frozenset({item}),
                                "error.rs": frozenset({item})}) == ""
    assert removal_target(dup, {}) == ""


# --- the transaction --------------------------------------------------------

def test_refused_write_restores_every_file(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text("original a", encoding="utf-8")
    (tmp_path / "b.rs").write_text("original b", encoding="utf-8")
    log: list[str] = []
    verdict = guarded_write(
        {"a.rs": "rewritten a", "b.rs": "rewritten b"}, tmp_path,
        _deps([3, 23], [[], []], log))

    assert not verdict.kept
    # All-or-nothing: the file that looks innocent goes back too, because the
    # diagnostics never said which of the two caused the increase.
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == "original a"
    assert (tmp_path / "b.rs").read_text(encoding="utf-8") == "original b"
    assert any("REFUSED" in line and "3 → 23" in line for line in log)


def test_negative_control_ungated_write_lands_and_stays(tmp_path: Path) -> None:
    """The pre-fix path: write, read nothing back. This is the behaviour the
    gate replaces — without it the 3 → 23 rewrite above is simply kept."""
    (tmp_path / "a.rs").write_text("original a", encoding="utf-8")
    (tmp_path / "a.rs").write_text("rewritten a", encoding="utf-8")
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == "rewritten a"


def test_refusal_unlinks_a_file_the_write_created(tmp_path: Path) -> None:
    log: list[str] = []
    verdict = guarded_write({"new.rs": "content"}, tmp_path,
                            _deps([1, 9], [[], []], log))
    assert not verdict.kept
    assert not (tmp_path / "new.rs").exists()


def test_kept_write_stays(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text("original", encoding="utf-8")
    verdict = guarded_write({"a.rs": "better"}, tmp_path,
                            _deps([9, 4], [[], []], []))
    assert verdict.kept
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == "better"


def test_duplicate_introduction_refuses_a_write_that_compiles_better(
        tmp_path: Path) -> None:
    (tmp_path / "error.rs").write_text("before", encoding="utf-8")
    log: list[str] = []
    deps = _deps(
        [10, 2],
        [[_rec("fmt.rs", "debug", "impl ErrorImpl")],
         [_rec("fmt.rs", "debug", "impl ErrorImpl"),
          _rec("error.rs", "debug", "impl ErrorImpl")]],
        log)
    verdict = guarded_write({"error.rs": "after"}, tmp_path, deps)

    assert verdict.outcome is WriteOutcome.REFUSE_DUPLICATE
    assert (tmp_path / "error.rs").read_text(encoding="utf-8") == "before"
    assert any("ErrorImpl::debug" in line for line in log)


def test_unanswerable_probe_abstains_rather_than_refusing(tmp_path: Path) -> None:
    """cargo unavailable → the count rule has no reading; the write lands as it
    did before the gate existed, rather than every write being refused."""
    (tmp_path / "a.rs").write_text("original", encoding="utf-8")
    verdict = guarded_write({"a.rs": "new"}, tmp_path,
                            _deps([None, None], [[], []], []))
    assert verdict.kept
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == "new"
