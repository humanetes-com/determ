"""The tree-write transaction: pre-image on first touch, verify on parse then
build, rollback by byte restore, commit forgets.

Plan sentences pinned here:

* after a refused transaction every touched file is byte-identical to its
  pre-image — regardless of what was written, including text no name-keyed
  revert could locate a span in (the 2026-09-04 corruption shape: an identical
  body span applied twice);
* a file that did not parse before is not held against the transaction, but
  a file that parsed and no longer does is a refusal whatever the build says;
* the build check runs only when every touched file parses;
* a file the transaction created is removed again on rollback.

Negative control: the name-keyed revert over the same corrupted text leaves
a residual — which is why the rollback is a byte restore and not a revert.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.fn_revert import revert_fns_to_stub
from pipeline_core.application.reconstruct.tree_txn import (
    UNPARSEABLE_TREE_EVENT,
    BoundaryOutcome,
    TreeTxn,
    capture,
    guard_tree_boundary,
    parse_guarded_write,
    restore,
    unparseable_files,
)
from pipeline_core.domain.models import FnLocator

_OK = "fn a() {\n    1\n}\n"
_BAD = "fn a() {\n    1\n}\n}\n"        # one brace too many


def _parses(p: Path) -> bool:
    text = p.read_text(encoding="utf-8")
    return text.count("{") == text.count("}")


def test_rollback_restores_every_pre_image_byte_for_byte(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text(_OK, encoding="utf-8")
    (src / "b.rs").write_text("fn b() {\n    2\n}\n", encoding="utf-8")
    txn = TreeTxn(tmp_path, validate_parse=_parses)
    txn.write("src/a.rs", _BAD)
    txn.write("src/b.rs", "garbage that no locator could re-derive a span in")
    txn.write("src/new.rs", "fn n() {}\n")
    assert txn.touched == ("src/a.rs", "src/b.rs", "src/new.rs")
    verdict = txn.close()
    assert not verdict.ok and verdict.broken == ("src/a.rs",)
    assert "unparseable after write: src/a.rs" in verdict.reason
    assert (src / "a.rs").read_text(encoding="utf-8") == _OK
    assert (src / "b.rs").read_text(encoding="utf-8") == "fn b() {\n    2\n}\n"
    assert not (src / "new.rs").exists(), "a file the txn created is removed again"


def test_a_clean_transaction_commits_and_forgets(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_OK, encoding="utf-8")
    builds: list[int] = []

    def _build() -> tuple[bool, str]:
        builds.append(1)
        return True, ""

    txn = TreeTxn(tmp_path, validate_parse=_parses, build_check=_build)
    txn.write("a.rs", "fn a() {\n    2\n}\n")
    assert txn.close().ok and builds == [1]
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == "fn a() {\n    2\n}\n"
    assert txn.touched == ()


def test_the_build_check_never_runs_on_an_unparseable_file(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_OK, encoding="utf-8")
    builds: list[int] = []
    txn = TreeTxn(tmp_path, validate_parse=_parses,
                  build_check=lambda: (builds.append(1) or True, ""))
    txn.write("a.rs", _BAD)
    assert not txn.verify().ok and builds == []


def test_a_build_refusal_rolls_back_too(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_OK, encoding="utf-8")
    txn = TreeTxn(tmp_path, validate_parse=_parses,
                  build_check=lambda: (False, "error[E0308]: mismatched types\n"))
    txn.write("a.rs", "fn a() {\n    2\n}\n")
    verdict = txn.close()
    assert not verdict.ok and verdict.reason == "does not build: error[E0308]: mismatched types"
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == _OK


def test_a_file_that_did_not_parse_before_is_not_held_against_the_txn(
        tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_BAD, encoding="utf-8")
    txn = TreeTxn(tmp_path, validate_parse=_parses)
    txn.write("a.rs", _BAD + "}\n")
    assert txn.verify().ok, "it was unparseable before; the txn made nothing worse"


def test_an_exception_mid_transaction_leaves_the_tree_as_found(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_OK, encoding="utf-8")
    try:
        with TreeTxn(tmp_path, validate_parse=_parses) as txn:
            txn.write("a.rs", "half written")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert (tmp_path / "a.rs").read_text(encoding="utf-8") == _OK


def test_capture_and_restore_are_the_single_file_primitive(tmp_path: Path) -> None:
    p = tmp_path / "a.rs"
    p.write_text(_OK, encoding="utf-8")
    pre = capture(p, _parses)
    assert pre.parsed is True and pre.data == _OK.encode()
    p.write_text("x", encoding="utf-8")
    restore(pre)
    assert p.read_text(encoding="utf-8") == _OK
    missing = capture(tmp_path / "none.rs", _parses)
    assert missing.data is None and missing.parsed is None
    (tmp_path / "none.rs").write_text("created", encoding="utf-8")
    restore(missing)
    assert not (tmp_path / "none.rs").exists()


def test_parse_guarded_write_keeps_its_contract(tmp_path: Path) -> None:
    p = tmp_path / "a.rs"
    p.write_text(_OK, encoding="utf-8")
    assert parse_guarded_write(p, _BAD.encode(), _OK.encode(), _parses) is False
    assert p.read_text(encoding="utf-8") == _OK
    assert parse_guarded_write(p, b"fn a() {}\n", _OK.encode(), _parses) is True


def test_unparseable_files_names_the_broken_ones_relative_to_the_root(
        tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "ok.rs").write_text(_OK, encoding="utf-8")
    (src / "sub" / "bad.rs").write_text(_BAD, encoding="utf-8")
    assert unparseable_files(src, _parses) == ("sub/bad.rs",)


def test_negative_control_a_name_keyed_revert_cannot_undo_a_double_splice(
        tmp_path: Path) -> None:
    """The rollback the re-seed used until 2026-09-04. Apply the same body
    span twice to one text (the site-cardinality double splice), then ask the
    name-keyed revert to undo it: it re-derives spans on the wreck and leaves
    a residual, so the file still does not parse. The byte restore above is
    the fix because THIS is what a re-derivation does."""
    body = "\n        Error::new(ContextError { context, error: self })\n    "
    # The live shape: the second application's end offset (measured on
    # the pre-edit text) falls inside the body just written, so the
    # duplicated tail is `, error: self })` — a `}` with no `{`.
    tail = ", error: self })\n    "
    orig_len = len(body) - len(tail)
    orig = "\n" + "x" * (orig_len - 6) + "\n    "
    text = "impl A {\n    fn f(&self) -> u8 {" + orig + "}\n}\n"
    start = text.index("{", text.index("fn f")) + 1
    end = start + len(orig)
    edits = [(start, end, body), (start, end, body)]
    wrecked = text
    for s, e, b in sorted(edits, reverse=True):
        wrecked = wrecked[:s] + b + wrecked[e:]
    assert wrecked.endswith(body + tail + "}\n}\n")
    (tmp_path / "a.rs").write_text(wrecked, encoding="utf-8")
    assert not _parses(tmp_path / "a.rs")
    txn = TreeTxn(tmp_path)              # no validator: the revert is the subject
    revert_fns_to_stub([FnLocator("a.rs", "f")], txn)
    after = (tmp_path / "a.rs").read_text(encoding="utf-8")
    assert after != text, "the name-keyed revert does not recover the pre-image"
    assert not _parses(tmp_path / "a.rs"), "and the file still does not parse"


# --- the boundary read every sweep owner makes --------------------------------

def _boundary(tmp_path: Path, *, heal, validate=_parses):
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    out = guard_tree_boundary(
        tmp_path, validate_parse=validate, heal_file=heal,
        log=logs.append,
        emit=lambda ev, *, duration_ms=None, data=None: events.append((ev, dict(data or {}))),
        tag="[t]", when="before the sweep", iter_no=3, cluster="c")
    return out, logs, events


def test_guard_tree_boundary_heals_the_broken_file_before_the_sweep(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text(_BAD, encoding="utf-8")
    (src / "b.rs").write_text(_OK, encoding="utf-8")
    healed: list[str] = []

    def _heal(rel: str) -> bool:
        healed.append(rel)
        (tmp_path / rel).write_text(_OK, encoding="utf-8")
        return True

    out, logs, events = _boundary(tmp_path, heal=_heal)
    assert out == BoundaryOutcome(("src/a.rs",), ("src/a.rs",)) and not out.clean
    assert healed == ["src/a.rs"] and _parses(src / "a.rs")
    assert any("[t] iter 3: TREE UNPARSEABLE before the sweep — src/a.rs" in ln
               and "healing now" in ln for ln in logs)
    assert events == [(UNPARSEABLE_TREE_EVENT, {
        "cluster": "c", "iter": 3, "when": "before the sweep",
        "files": ["src/a.rs"], "healed": ["src/a.rs"]})]


def test_control_no_heal_wired_reports_and_leaves_the_tree_broken(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text(_BAD, encoding="utf-8")
    out, logs, events = _boundary(tmp_path, heal=None)
    assert out.files == ("src/a.rs",) and out.healed == ()
    assert not _parses(src / "a.rs"), "the sweep will read the broken tree"
    assert any("NO heal wired" in ln for ln in logs)
    assert events and events[0][1]["healed"] == []


def test_control_no_validator_is_no_reading(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.rs").write_text(_BAD, encoding="utf-8")
    out, logs, events = _boundary(tmp_path, heal=None, validate=None)
    assert out == BoundaryOutcome() and out.clean
    assert logs == [] and events == []


def test_a_clean_tree_is_a_silent_boundary(tmp_path: Path) -> None:
    (tmp_path / "a.rs").write_text(_OK, encoding="utf-8")
    out, logs, events = _boundary(tmp_path, heal=None)
    assert out.clean and logs == [] and events == []


def test_pre_image_text_keeps_the_bytes_a_text_round_trip_folds(tmp_path: Path) -> None:
    """Why the restore is bytes and never text: `read_text` folds `\r\n`,
    so a text captured that way and written back is not what was there."""
    crlf = b"fn a() {\r\n    1\r\n}\r\n"
    p = tmp_path / "a.rs"
    p.write_bytes(crlf)
    pre = capture(p, None)
    assert pre.text() == crlf.decode()
    assert p.read_text(encoding="utf-8") != crlf.decode(), "the text view folds"
    p.write_bytes(b"corrupt")
    restore(pre)
    assert p.read_bytes() == crlf
    assert capture(tmp_path / "none.rs", None).text() == ""
