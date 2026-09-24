"""Tests for per_fn_retry use-case (V1 extraction).

Covers:
- fix_one_ref guards (tests/ path, gen_tests file, missing file, no-locate)
- fix_one_ref B2 contract: transport/model/splice errors RAISE (not swallowed)
- apply_per_fn_fixes B2 isolation: one fn's exception skipped, others continue
- apply_per_fn_fixes return count
"""
from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock

import pytest

from pipeline_core.adapters.fill_engine_roles import MutatorAdapter
from pipeline_core.application.reconstruct.per_fn_retry import (
    BodyGate,
    RetryContext,
    RetryDeps,
    apply_per_fn_fixes,
    fix_one_ref,
)
from pipeline_core.application.reconstruct.splice import (
    locate_fn_by_record,
    splice_fn_body,
    splice_fn_body_at,
)
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.retry_outcome import ClusterReading, TreeReading
from pipeline_core.domain.rust_fn_headers import locate_all_fns
from tests.conversation_fakes import idle_conversation


def _ctx(tmp_path: Path) -> RetryContext:
    return RetryContext(
        project_dir=tmp_path,
        src_root=tmp_path / "src",
        cluster_label="stable",
        containers={},
        type_recs=[],
        fr_path=None,
        tests_dir_files=[],
    )


def _deps(**overrides) -> RetryDeps:
    base: dict[str, object] = {
        "locate_fn": lambda text, fn: None,
        "locate_all_fns": lambda text, fn: [],
        "gather_fix_context": MagicMock(return_value=MagicMock(
            container="", types_section="", notes_section="", bound_methods_section="")),
        "build_per_fn_fix_prompt": lambda **kw: "prompt",
        "ask_per_fn_body": lambda prompt, fn, log, **kw: (None, ""),
        "open_conversation": idle_conversation,
        "splice_fn_body": lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
        "splice_body_scoped": lambda fp, fn, container, sig, body, **kw: (
            SpliceOutcome(True, "ok", 0)),
        "find_tests_for_fn": lambda tdf, fn, **kw: [],
        "focus_cargo_errors": lambda out, rel_f: "",
        "is_gen_tests_file": lambda name: name.startswith("gen_"),
        "log": lambda msg: None,
    }
    base.update(overrides)
    return RetryDeps(**base)  # type: ignore[arg-type]


# ── fix_one_ref guards ─────────────────────────────────────────────────────────

def test_fix_one_ref_skips_tests_path(tmp_path):
    assert fix_one_ref("tests/foo.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_skips_gen_tests_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "gen_foo.rs").write_text("fn bar() {}")
    assert fix_one_ref("gen_foo.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_skips_missing_file(tmp_path):
    assert fix_one_ref("src/nonexistent.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_returns_false_when_fn_not_located(tmp_path):
    (tmp_path / "src.rs").write_text("fn other() {}")
    assert fix_one_ref("src.rs", "missing_fn", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_returns_false_when_model_returns_none(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: (None, ""),
    )
    assert fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d) is False


def test_fix_one_ref_returns_true_on_success(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ Ok(()) }", ""),
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    assert fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d) is True


# ── contract: model/transport errors must RAISE out of fix_one_ref ─────────

def test_fix_one_ref_raises_on_model_error(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")

    def _ask_raise(*a, **kw):
        raise RuntimeError("transport down")

    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=_ask_raise,
    )
    with pytest.raises(RuntimeError, match="transport down"):
        fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d)


def test_fix_one_ref_raises_on_splice_error(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")

    def _splice_raise(fp, fn, container, sig, body, **kw):
        raise ValueError("bad splice")

    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ 42 }", ""),
        splice_body_scoped=_splice_raise,
    )
    with pytest.raises(ValueError, match="bad splice"):
        fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d)


# ── the write goes through the container-scoped path ───────────────────────

def test_fix_one_ref_splices_through_the_container_scoped_path(tmp_path):
    """The container and signature the prompt was built from must reach the
    splice. A bare-name write refuses `ambiguous_name` on any file holding two
    fns of that name, and on the reconstructed anyhow crate that is the common
    case — `src/error.rs::from` occurs 4x, `src/kind.rs::new` 3x."""
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    seen: dict[str, str] = {}

    def _scoped(fp, fn, container, sig, body, **kw):
        seen.update(container=container, sig=sig, body=body)
        return SpliceOutcome(True, "ok", 0)

    ctx = RetryContext(
        project_dir=tmp_path, src_root=tmp_path / "src", cluster_label="stable",
        containers={("src.rs", "bar"): "impl Display for Error"},
        type_recs=[], fr_path=None, tests_dir_files=[],
    )
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ 42 }", ""),
        splice_body_scoped=_scoped,
    )
    assert fix_one_ref("src.rs", "bar", "", ctx, d) is True
    assert seen["container"] == "impl Display for Error"
    assert seen["sig"] == "fn bar()"


def test_fix_one_ref_lands_a_body_when_the_file_holds_two_fns_of_that_name(
        tmp_path):
    """End-to-end against the REAL scoped splice: the body reaches disk in the
    fn the prompt was built from, and the same-named sibling is untouched.

    Run against the pre-fix code this fails the way the 2026-08-05 run did —
    `splice refused for src/context.rs::source: ambiguous_name`, a full model
    call spent and its answer discarded.
    """
    src = tmp_path / "src"
    src.mkdir()
    f = src / "context.rs"
    f.write_text(
        "impl StdError for ContextError<C, E> {\n"
        "    fn source(&self) -> Option<&dyn StdError> { unimplemented!() }\n"
        "}\n"
        "impl StdError for ContextError<C, Error> {\n"
        "    fn source(&self) -> Option<&dyn StdError> { self.second() }\n"
        "}\n"
    )
    text = f.read_text()
    first = text.index("unimplemented!()")
    # Records as the syn extractor emits them: byte offsets, container, sig.
    recs = [
        {"file": "context.rs", "fn_name": "source",
         "container": "impl StdError for ContextError < C , E >",
         "sig_text": "fn source(&self) -> Option<&dyn StdError>",
         "sig_byte_start": text.index("    fn source"),
         "body_byte_start": first, "body_byte_end": first + len("unimplemented!()"),
         "body_text": "unimplemented!()", "has_unimplemented_body": True},
        {"file": "context.rs", "fn_name": "source",
         "container": "impl StdError for ContextError < C , Error >",
         "sig_text": "fn source(&self) -> Option<&dyn StdError>",
         "sig_byte_start": text.rindex("    fn source"),
         "body_byte_start": text.index("self.second()"),
         "body_byte_end": text.index("self.second()") + len("self.second()"),
         "body_text": "self.second()", "has_unimplemented_body": False},
    ]
    mut = MutatorAdapter(
        locate_fn_by_record_fn=partial(locate_fn_by_record, lambda _sd: recs),
        locate_fn_fn=lambda t, n: (locate_all_fns(t, n) or [None])[0],
        splice_fn_body_at_fn=splice_fn_body_at,
        splice_fn_body_fn=splice_fn_body,
        replace_fn_signature_fn=lambda *a: False,
        extract_signature_block_fn=lambda r: None,
        lock_for_fn=lambda p: Lock(),
    )
    ctx = RetryContext(
        project_dir=tmp_path, src_root=src, cluster_label="stable",
        containers={("context.rs", "source"):
                    "impl StdError for ContextError<C, E>"},
        type_recs=[], fr_path=None, tests_dir_files=[],
    )
    d = _deps(
        locate_fn=mut.locate_fn,
        ask_per_fn_body=lambda *a, **kw: ("{ self.first() }", ""),
        splice_body_scoped=mut.splice_body_scoped,
    )
    assert fix_one_ref("src/context.rs", "source", "", ctx, d) is True
    out = f.read_text()
    assert "self.first()" in out, "the model's body never reached disk"
    assert "unimplemented!()" not in out, "the stub was not the fn that got written"
    assert "self.second()" in out, "the same-named sibling was overwritten"


# ── apply_per_fn_fixes isolation ───────────────────────────────────────────

def test_apply_skips_failing_fn_and_continues(tmp_path):
    """fn#1 raises → logged and skipped; fn#2 succeeds → count=1."""
    (tmp_path / "ok.rs").write_text("fn ok_fn() { unimplemented!() }")
    calls: list[str] = []

    def ask(prompt, fn, log, **kw):
        if fn == "boom_fn":
            raise RuntimeError("model exploded")
        calls.append(fn)
        return ("{ 1 }", "")

    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 8, 22),
        ask_per_fn_body=ask,
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    ctx = _ctx(tmp_path)
    # boom_fn references missing file (→ False, no raise) to isolate the raise
    # to the ask_per_fn_body path for boom_fn.
    # We need boom_fn to actually reach ask; give it a real file.
    (tmp_path / "boom.rs").write_text("fn boom_fn() { unimplemented!() }")
    count = apply_per_fn_fixes(
        [("boom.rs", "boom_fn"), ("ok.rs", "ok_fn")], "", ctx, d
    )
    assert count == 1
    assert calls == ["ok_fn"]


def test_apply_returns_zero_when_all_raise(tmp_path):
    def ask(*a, **kw):
        raise ConnectionError("offline")

    (tmp_path / "a.rs").write_text("fn a() { unimplemented!() }")
    (tmp_path / "b.rs").write_text("fn b() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=ask,
    )
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b")], "", _ctx(tmp_path), d
    )
    assert count == 0


def test_apply_returns_correct_count(tmp_path):
    """Two fns both succeed → count=2."""
    for name in ("a.rs", "b.rs"):
        (tmp_path / name).write_text(f"fn {name[0]}() {{ unimplemented!() }}")
    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=lambda *a, **kw: ("{ () }", ""),
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b")], "", _ctx(tmp_path), d
    )
    assert count == 2


# ── the per-body accept gate ──────────────────────────────────────────────────

def _writing_deps(marker: str = "NEW"):
    """Deps whose splice actually writes, so the gate has something to revert."""
    def _splice(fp, fn, container, sig, body, **kw):
        fp.write_text(f"fn {fn}() {{ {marker} }}\n", encoding="utf-8")
        return SpliceOutcome(True, "ok", 1)
    return _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=lambda *a, **kw: ("{ () }", ""),
        splice_body_scoped=_splice,
    )


def _reading(**per_cluster: tuple[bool, int]) -> TreeReading:
    return TreeReading(per_cluster=tuple(
        ClusterReading(cluster=name, ran=ran, failures=f)
        for name, (ran, f) in per_cluster.items()))


def test_gate_reverts_only_the_body_that_regressed_another_cluster(tmp_path):
    """The primary cluster is the repair signal, but a body
    that fixes it and breaks the OTHER cluster is refused — and only that body
    is lost, not the batch around it.

    Pre-fix this could not be observed at all: the retry rebuilt the test
    command for one cluster and every improvement check, floor comparison and
    revert read that cluster alone.
    """
    for name in ("a.rs", "b.rs", "c.rs"):
        (tmp_path / name).write_text(f"fn {name[0]}() {{ OLD }}\n")
    readings = [
        _reading(nightly=(True, 1), stable=(True, 1)),   # after a.rs — better
        _reading(nightly=(True, 0), stable=(True, 2)),   # after b.rs — stable worse
        _reading(nightly=(True, 1), stable=(True, 1)),   # after c.rs — held
    ]
    logged: list[str] = []
    gate = BodyGate(
        read_tree=lambda: readings.pop(0),
        baseline=_reading(nightly=(True, 2), stable=(True, 1)),
        log=logged.append,
    )
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b"), ("c.rs", "c")], "", _ctx(tmp_path),
        _writing_deps(), gate=gate)
    assert count == 2
    assert "NEW" in (tmp_path / "a.rs").read_text()
    assert (tmp_path / "b.rs").read_text() == "fn b() { OLD }\n", \
        "the regressing body was not reverted"
    assert "NEW" in (tmp_path / "c.rs").read_text(), \
        "one bad body must not cost the bodies after it"
    assert any("reverted b.rs::b" in m and "regressed" in m for m in logged)


def test_gate_reverts_a_body_that_stops_the_tree_reporting(tmp_path):
    """A body that breaks the build costs ONE body. On 2026-08-05 it cost 17
    model calls and 42 minutes: the batch was measured once, at the end."""
    for name in ("a.rs", "b.rs"):
        (tmp_path / name).write_text(f"fn {name[0]}() {{ OLD }}\n")
    readings = [
        _reading(nightly=(False, 0), stable=(False, 0)),  # after a.rs — no verdicts
        _reading(nightly=(True, 1), stable=(True, 1)),    # after b.rs — fine
    ]
    logged: list[str] = []
    gate = BodyGate(read_tree=lambda: readings.pop(0),
                    baseline=_reading(nightly=(True, 2), stable=(True, 1)),
                    log=logged.append)
    count = apply_per_fn_fixes([("a.rs", "a"), ("b.rs", "b")], "",
                               _ctx(tmp_path), _writing_deps(), gate=gate)
    assert count == 1
    assert (tmp_path / "a.rs").read_text() == "fn a() { OLD }\n"
    assert any("broke_build" in m for m in logged)


def test_gate_stops_the_frontier_once_every_cluster_passes(tmp_path, monkeypatch):
    """The remaining frontier entries would each buy a ~2-minute model call
    against a tree with nothing left to fix. Sequential here: with generation
    overlapped, the calls already in flight are the bound on what an early stop
    can throw away — see the windowing test below."""
    monkeypatch.setenv("TASK3_RETRY_PARALLELISM", "1")
    for name in ("a.rs", "b.rs"):
        (tmp_path / name).write_text(f"fn {name[0]}() {{ OLD }}\n")
    asked: list[str] = []
    d = _writing_deps()
    d = _deps(
        locate_fn=d.locate_fn,
        ask_per_fn_body=lambda prompt, fn, log, **kw: (
            asked.append(fn) or ("{ () }", "")),
        splice_body_scoped=d.splice_body_scoped,
    )
    gate = BodyGate(
        read_tree=lambda: _reading(nightly=(True, 0), stable=(True, 0)),
        baseline=_reading(nightly=(True, 1), stable=(True, 0)),
        log=lambda _m: None)
    count = apply_per_fn_fixes([("a.rs", "a"), ("b.rs", "b")], "",
                               _ctx(tmp_path), d, gate=gate)
    assert count == 1 and asked == ["a"]


# ── a same-named sibling is addressable at all (kind.rs holds three `new`) ────

def test_a_non_first_occurrence_can_be_targeted(tmp_path):
    """`kind.rs` declares `Adhoc::new`, `Trait::new` and `Boxed::new`. The
    frontier key is (file, bare name), so before this the loop re-authored
    `Adhoc::new` every time and `Boxed::new` — on the path of the crate's last
    failing test — could not be named, prompted for, or written.
    """
    src = tmp_path / "src"
    src.mkdir()
    f = src / "kind.rs"
    f.write_text(
        "impl Adhoc {\n"
        "    pub fn new<M>(self, message: M) -> Error { first() }\n"
        "}\n"
        "impl Boxed {\n"
        "    pub fn new(self, error: Box<dyn StdError>) -> Error { unimplemented!() }\n"
        "}\n"
    )
    text = f.read_text()
    recs = [
        {"file": "kind.rs", "fn_name": "new", "container": "impl Adhoc",
         "sig_text": "pub fn new<M>(self, message: M) -> Error",
         "sig_byte_start": text.index("    pub fn new<M>"),
         "body_byte_start": text.index("first()"),
         "body_byte_end": text.index("first()") + len("first()"),
         "body_text": "first()", "has_unimplemented_body": False},
        {"file": "kind.rs", "fn_name": "new", "container": "impl Boxed",
         "sig_text": "pub fn new(self, error: Box<dyn StdError>) -> Error",
         "sig_byte_start": text.rindex("    pub fn new("),
         "body_byte_start": text.index("unimplemented!()"),
         "body_byte_end": text.index("unimplemented!()") + len("unimplemented!()"),
         "body_text": "unimplemented!()", "has_unimplemented_body": True},
    ]
    mut = MutatorAdapter(
        locate_fn_by_record_fn=partial(locate_fn_by_record, lambda _sd: recs),
        locate_fn_fn=lambda t, n: (locate_all_fns(t, n) or [None])[0],
        splice_fn_body_at_fn=splice_fn_body_at,
        splice_fn_body_fn=splice_fn_body,
        replace_fn_signature_fn=lambda *a: False,
        extract_signature_block_fn=lambda r: None,
        lock_for_fn=lambda p: Lock(),
    )
    shown: dict[str, str] = {}
    ctx = RetryContext(
        project_dir=tmp_path, src_root=src, cluster_label="stable",
        containers={("kind.rs", "new"): "impl Adhoc"},
        type_recs=[], fr_path=None, tests_dir_files=[],
        occurrences={("kind.rs", "new"): (
            ("impl Adhoc", "pub fn new<M>(self, message: M) -> Error"),
            ("impl Boxed", "pub fn new(self, error: Box<dyn StdError>) -> Error"))},
    )
    d = _deps(
        locate_fn=mut.locate_fn,
        locate_all_fns=locate_all_fns,
        build_per_fn_fix_prompt=lambda **kw: shown.update(
            sig=kw["sig_text"], prev=kw["prev_body"]) or "prompt",
        ask_per_fn_body=lambda *a, **kw: ("{ Error::from_boxed(error) }", ""),
        splice_body_scoped=mut.splice_body_scoped,
    )
    assert fix_one_ref("src/kind.rs", "new", "", ctx, d, occurrence=1) is True
    out = f.read_text()
    assert "from_boxed" in out, "the second definition was never written"
    assert "first()" in out, "the first definition was overwritten"
    assert "unimplemented!()" not in out
    assert shown["sig"] == "pub fn new(self, error: Box<dyn StdError>) -> Error", \
        "the model was shown the wrong definition's signature"
    assert shown["prev"].strip() == "unimplemented!()"


def test_occurrence_zero_is_the_old_first_definition_behaviour(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    seen: dict[str, str] = {}
    ctx = RetryContext(
        project_dir=tmp_path, src_root=tmp_path / "src", cluster_label="stable",
        containers={("src.rs", "bar"): "impl A"},
        type_recs=[], fr_path=None, tests_dir_files=[],
        occurrences={("src.rs", "bar"): (("impl A", "fn bar()"),)},
    )
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ 42 }", ""),
        splice_body_scoped=lambda fp, fn, container, sig, body, **kw: (
            seen.update(container=container) or SpliceOutcome(True, "ok", 0)),
    )
    assert fix_one_ref("src.rs", "bar", "", ctx, d) is True
    assert seen["container"] == "impl A"


def test_generation_overlaps_while_the_writes_stay_serial(tmp_path, monkeypatch):
    """`C10 parallel-streams SKIP no concurrent llm calls observed` during the
    2026-08-05 task3 retry, against `PASS … max 4 concurrent` during task2c on
    the same machine and server. The calls must overlap — and the splices must
    not, because each body's verdict is only meaningful against a tree with the
    previous body already in it.
    """
    import threading
    monkeypatch.setenv("TASK3_RETRY_PARALLELISM", "4")
    names = [f"f{i}.rs" for i in range(4)]
    for n in names:
        (tmp_path / n).write_text(f"fn {n[:2]}() {{ OLD }}\n")
    live = threading.Semaphore(0)
    peak = {"n": 0}
    lock = threading.Lock()
    inflight = {"n": 0}
    splice_threads: list[int] = []

    def _ask(prompt, fn, log, **kw):
        with lock:
            inflight["n"] += 1
            peak["n"] = max(peak["n"], inflight["n"])
        live.release()
        # Hold until the whole window is in flight (or time out on a
        # single-threaded implementation, which is the failure this pins).
        for _ in range(4):
            live.acquire(timeout=0.5)
        with lock:
            inflight["n"] -= 1
        return "{ () }", ""

    def _splice(fp, fn, container, sig, body, **kw):
        splice_threads.append(threading.get_ident())
        fp.write_text(f"fn {fn}() {{ NEW }}\n", encoding="utf-8")
        return SpliceOutcome(True, "ok", 1)

    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=_ask,
        splice_body_scoped=_splice,
    )
    count = apply_per_fn_fixes(
        [(n, n[:2]) for n in names], "", _ctx(tmp_path), d)
    assert count == 4
    assert peak["n"] > 1, "the model calls never overlapped"
    assert set(splice_threads) == {threading.get_ident()}, \
        "the writes must happen on the caller's thread, one at a time"


def test_the_gate_revert_is_a_byte_restore(tmp_path):
    """The reverted body's file comes back byte for byte — the pre-image is
    captured and restored through the transaction primitive, never as a
    text the newline translation already folded."""
    crlf = b"fn b() {\r\n    OLD\r\n}\r\n"
    (tmp_path / "a.rs").write_text("fn a() { OLD }\n", encoding="utf-8")
    (tmp_path / "b.rs").write_bytes(crlf)
    readings = [
        _reading(nightly=(True, 1)),   # after a.rs — better
        _reading(nightly=(True, 3)),   # after b.rs — worse
    ]
    gate = BodyGate(read_tree=lambda: readings.pop(0),
                    baseline=_reading(nightly=(True, 2)), log=lambda m: None)
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b")], "", _ctx(tmp_path), _writing_deps(), gate=gate)
    assert count == 1
    assert "NEW" in (tmp_path / "a.rs").read_text(encoding="utf-8")
    assert (tmp_path / "b.rs").read_bytes() == crlf
