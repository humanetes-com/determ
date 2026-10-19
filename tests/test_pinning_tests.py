"""The end gate's retro: a pinning test per blocking body.

Run `1680589-7a6824f8` (2026-10-19) ended with every oracle test passing and
two reachable `provide` bodies still `unimplemented!()`; nothing could turn
that into work for t2a. The pass writes a test that reaches the body through
the public API, admits it only when the original crate passes it and the
rebuilt tree fails it inside the body, and moves the oracle seal with it.

Invented sites and shapes only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pipeline_core.adapters.pin_inputs import (
    example_header,
    route_lines,
    worlds_compiling,
)
from pipeline_core.adapters.pin_runs import _empty_bodies, reading_of
from pipeline_core.application.pinning_tests import (
    PinningDeps,
    admit_oracle_file,
    pin_file_stem,
    run_pinning_pass,
)
from pipeline_core.domain.end_verdict import EndVerdict
from pipeline_core.domain.fn_routes import FnRoutes, PublicRoute, WorldFacts
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.pinning_test import (
    PinRefusal,
    TestRun,
    judge_pin,
)

_SITE = FnSite("src/wrap.rs", "probe", "impl Fetch for Wrapper < T >")
_FILE = "use krate::*;\n\n#[test]\nfn reaches_probe() {\n    assert!(fetch().is_some());\n}\n"
_OK_ORIG = TestRun(True, frozenset({"reaches_probe"}))
_RED_REBUILT = TestRun(True, failed=frozenset({"reaches_probe"}))


# --- the judgement ------------------------------------------------------------

def test_a_test_the_original_passes_and_the_stub_fails_is_admitted() -> None:
    j = judge_pin(_FILE, _OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))
    assert j.admitted and j.pinning == {"reaches_probe"}


def test_a_test_that_passes_on_the_rebuilt_tree_is_refused() -> None:
    green = TestRun(True, passed=frozenset({"reaches_probe"}))
    j = judge_pin(_FILE, _OK_ORIG, green, frozenset())
    assert not j.admitted and j.refusal is PinRefusal.REBUILT_PASSED


def test_a_red_test_whose_panic_is_elsewhere_is_refused() -> None:
    j = judge_pin(_FILE, _OK_ORIG, _RED_REBUILT, frozenset())
    assert j.refusal is PinRefusal.REBUILT_FAILED_ELSEWHERE


def test_a_test_the_original_fails_is_refused_before_the_rebuilt_reading() -> None:
    bad = TestRun(True, failed=frozenset({"reaches_probe"}), excerpt="left: 1 right: 2")
    j = judge_pin(_FILE, bad, _RED_REBUILT, frozenset({"reaches_probe"}))
    assert j.refusal is PinRefusal.ORIGINAL_FAILED and "left: 1" in j.detail


def test_a_file_that_does_not_build_anywhere_is_refused_with_the_compiler_words() -> None:
    j = judge_pin(_FILE, TestRun(False, excerpt="error[E0425]"), _RED_REBUILT,
                  frozenset({"reaches_probe"}))
    assert j.refusal is PinRefusal.ORIGINAL_DID_NOT_RUN and "E0425" in j.detail
    j = judge_pin(_FILE, _OK_ORIG, TestRun(False, excerpt="error[E0599]"),
                  frozenset({"reaches_probe"}))
    assert j.refusal is PinRefusal.REBUILT_DID_NOT_RUN


def test_should_panic_is_refused_even_when_both_runs_would_admit_it() -> None:
    """The exploit: `#[should_panic]` passes on exactly the stub it claims to
    pin. Negative control: the same runs without the attribute admit."""
    weak = _FILE.replace("#[test]\n", "#[test]\n#[should_panic]\n")
    j = judge_pin(weak, _OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))
    assert j.refusal is PinRefusal.SHAPE
    assert judge_pin(_FILE, _OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"})).admitted


# --- the pass -------------------------------------------------------------------

def _deps(tmp_path: Path, replies: list[str], runs: list[tuple[TestRun, TestRun, frozenset[str]]],
          prompts: list[str], admitted: list[str], *, worlds: list[Any] | None = None) -> PinningDeps:
    world = SimpleNamespace(name="alt", cfg_true=["alt"], cfg_false=[])

    def _rebuilt(*a: Any) -> tuple[TestRun, dict[FnSite, frozenset[str]]]:
        sites = a[5]
        return runs[0][1], {sites[0]: runs.pop(0)[2]}

    return PinningDeps(
        ask=lambda p: (prompts.append(p), replies.pop(0))[1],
        worlds_for=lambda _pd, _s: [world] if worlds is None else worlds,
        routes_for=lambda _pd, _w, _s: ["// Reached from the public API: `fetch`"],
        signature_of=lambda _pd, _s: "fn probe(&self) -> Option<u8> { unimplemented!() }",
        api_surface=lambda _pd, _m: "pub fn fetch() -> Option<u8>",
        example_header=lambda _pd, _w: "#![feature(alt_api)]\nuse krate::*;",
        gate_line=lambda _w: "#![cfg(alt)]\n",
        run_original=lambda *_a: runs[0][0],
        run_rebuilt=_rebuilt,
        run_original_emptied=lambda *_a: (
            TestRun(True, failed=frozenset({"reaches_probe"}),
                    excerpt="assertion failed"),
            frozenset()),
        admit=lambda _pd, _m, rel, text: admitted.append(rel + "\n" + text) is None,
        log=lambda _m: None, emit=lambda *_a, **_k: None, max_attempts=3)


def test_a_refused_candidate_goes_back_with_its_reason_and_the_next_is_admitted(
        tmp_path: Path) -> None:
    prompts: list[str] = []
    admitted: list[str] = []
    green = TestRun(True, passed=frozenset({"reaches_probe"}))
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```", f"```rust\n{_FILE}```"],
                 [(_OK_ORIG, green, frozenset()),
                  (_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))],
                 prompts, admitted)
    n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps)
    assert n == 1 and len(prompts) == 2
    assert "rebuilt_passed" in prompts[1] and "CANDIDATE 1" in prompts[1]
    rel, text = admitted[0].split("\n", 1)
    assert rel == "tests/pin_probe_alt_gen_1.rs"
    assert text.startswith("#![cfg(alt)]\n// determ-pipeline generated integration test\n")
    # What the writer is shown: routes, signature, header, API — never a body
    # other than the stub's own `unimplemented!()`.
    assert "Reached from the public API" in prompts[0] and "#![feature(alt_api)]" in prompts[0]


def test_nothing_admitted_is_zero_and_the_loop_ends_red(tmp_path: Path) -> None:
    prompts: list[str] = []
    green = TestRun(True, passed=frozenset({"reaches_probe"}))
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 3,
                 [(_OK_ORIG, green, frozenset())] * 3, prompts, [])
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps) == 0
    assert len(prompts) == 3


def test_a_body_no_world_compiles_is_not_asked_for(tmp_path: Path) -> None:
    prompts: list[str] = []
    deps = _deps(tmp_path, [], [], prompts, [], worlds=[])
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps) == 0
    assert prompts == []


def test_a_candidate_written_for_one_body_that_pins_another_is_admitted_for_it(
        tmp_path: Path) -> None:
    """Live shape (run `1680589-7a6824f8`'s merged tree, 2026-10-19): a test
    written for one `provide` impl first panicked in the other one. That is
    still a pin, of the other body; the first stays unpinned and is asked
    for on its own. Negative control: a hit on a body that is NOT blocking is
    no pin at all."""
    other = FnSite("src/wrap.rs", "probe", "impl Fetch for Wrapper < Other >")
    prompts: list[str] = []
    admitted: list[str] = []
    world = SimpleNamespace(name="alt", cfg_true=["alt"], cfg_false=[])
    calls: list[tuple[FnSite, ...]] = []

    def _rebuilt(*a: Any) -> tuple[TestRun, dict[FnSite, frozenset[str]]]:
        sites = tuple(a[5])
        calls.append(sites)
        return _RED_REBUILT, {s: (frozenset({"reaches_probe"}) if s == other else frozenset())
                              for s in sites}

    deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 4, [], prompts, admitted)
    got_pins: list[tuple[FnSite, ...]] = []
    deps = PinningDeps(**{**{f: getattr(deps, f) for f in deps.__dataclass_fields__},
                          "run_original": lambda *_a: _OK_ORIG, "run_rebuilt": _rebuilt,
                          "worlds_for": lambda _pd, _s: [world],
                          "on_pinned": got_pins.append})
    n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE, other)), deps)
    assert n == 1 and calls[0] == (_SITE, other)
    assert all(c == (_SITE,) for c in calls[1:])        # only the unpinned one after
    assert len(prompts) == 3      # max_attempts for _SITE, the first of which pinned `other`
    assert got_pins == [(other,)]  # the body the panic landed in, not the one asked


def test_the_file_name_takes_the_first_free_index() -> None:
    w = SimpleNamespace(name="alt")
    assert pin_file_stem(_SITE, w, ["pin_probe_alt_gen_1"]) == "pin_probe_alt_gen_2"


# --- the inputs -------------------------------------------------------------------

def test_the_worlds_are_the_ones_whose_cfg_set_compiles_the_body() -> None:
    base = SimpleNamespace(name="base", cfg_true=[], cfg_false=["alt"])
    alt = SimpleNamespace(name="alt", cfg_true=["alt"], cfg_false=[])
    preds = [(FnSite(_SITE.file, _SITE.fn_name, "impl Fetch for Wrapper<T>"), ("alt",))]
    assert worlds_compiling(_SITE, preds, [base, alt]) == [alt]
    assert worlds_compiling(_SITE, [], [base, alt]) == [base, alt]


def test_routes_match_across_the_extractors_container_spacing() -> None:
    w = WorldFacts("alt", routes={"src/wrap.rs::probe [impl Fetch for Wrapper<T>]": FnRoutes(
        (PublicRoute("`fetch`", ("`Wrapper::new`",), "builds `Wrapper`"),))})
    assert route_lines([w], "alt", _SITE)[0].startswith("// Reached from the public API")
    assert route_lines([w], "base", _SITE) == []


def test_the_example_header_is_the_worlds_file_with_the_most_crate_attributes(
        tmp_path: Path) -> None:
    t = tmp_path / "tests"
    t.mkdir()
    (t / "a_gen_1.rs").write_text("#![cfg(alt)]\nuse krate::*;\n#[test]\nfn x() {}\n")
    (t / "b_gen_1.rs").write_text("#![cfg(alt)]\n#![feature(alt_api)]\nuse krate::*;\n"
                                  "#[test]\nfn y() {\n    use std::fmt;\n}\n")
    (t / "c_gen_1.rs").write_text("#![feature(other)]\n#![feature(more)]\nuse krate::*;\n")
    head = example_header(tmp_path, "#![cfg(alt)]\n")
    assert head == "#![feature(alt_api)]\nuse krate::*;"


def test_a_run_that_reported_no_test_is_not_a_run() -> None:
    assert not reading_of("error[E0433]: failed to resolve", finished=True).ran
    out = ("running 1 test\ntest reaches_probe ... FAILED\n\nfailures:\n\n"
           "---- reaches_probe stdout ----\n\nthread 'reaches_probe' panicked at "
           "src/wrap.rs:9:9:\nnot implemented\n\nfailures:\n    reaches_probe\n\n"
           "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")
    r = reading_of(out, finished=True)
    assert r.ran and r.failed == {"reaches_probe"} and "not implemented" in r.excerpt


# --- the test must observe the body --------------------------------------------

def _with(deps: PinningDeps, **kw: Any) -> PinningDeps:
    return PinningDeps(**{**{f: getattr(deps, f) for f in deps.__dataclass_fields__}, **kw})


def _observing(tmp_path: Path, emptied: TestRun, *,
               unit: bool = True) -> tuple[int, list[str], list[str]]:
    prompts: list[str] = []
    admitted: list[str] = []
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 3,
                 [(_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))] * 3,
                 prompts, admitted)
    seen: list[tuple[FnSite, ...]] = []
    deps = _with(deps, run_original_emptied=lambda *a: (
        seen.append(tuple(a[5])),
        (emptied, frozenset(a[5])) if unit else (TestRun(False), frozenset()))[1])
    n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps)
    return n, prompts, admitted


def test_a_poke_the_original_passes_with_the_body_emptied_is_refused(tmp_path: Path) -> None:
    """`let _ = call();` fails on the stub and passes on any body that
    returns: on the original with the blocking bodies emptied it still
    passes, so it observes nothing. Negative control: a candidate the
    emptied original fails is admitted."""
    green = TestRun(True, passed=frozenset({"reaches_probe"}))
    n, prompts, _ = _observing(tmp_path, green)
    assert n == 0 and len(prompts) == 3 and "unobserved" in prompts[1]
    red = TestRun(True, failed=frozenset({"reaches_probe"}), excerpt="assertion failed")
    assert _observing(tmp_path, red)[0] == 1


def test_a_body_that_could_not_be_emptied_is_refused(tmp_path: Path) -> None:
    """No reading is not a yes. Nothing could be emptied, or that run did
    not build, so the pin is refused. Negative control: the same candidate
    the emptied original fails is admitted (`test_a_poke_...`)."""
    n, prompts, _ = _observing(tmp_path, TestRun(False), unit=False)
    assert n == 0 and len(prompts) == 3 and "unmeasured" in prompts[1]


def test_emptying_drops_the_body_of_a_named_unit_fn_only(tmp_path: Path) -> None:
    """Only the named site, and only when its declaration returns nothing:
    `{}` is not a body a value-returning fn can take."""
    src = tmp_path / "src"
    src.mkdir()
    text = ("fn a() { work() }\nfn b() { work() }\n"
            "fn c() -> u8 { work() }\n")
    (src / "wrap.rs").write_text(text)

    def _rec(name: str, sig: str) -> dict[str, Any]:
        at = text.index(f"fn {name}")
        o = text.index("{", at) + 1
        return {"file": "src/wrap.rs", "fn_name": name, "container": "", "sig_text": sig,
                "body_byte_start": o, "body_byte_end": text.index("}", o)}

    recs = [_rec("a", "fn a()"), _rec("b", "fn b()"), _rec("c", "fn c() -> u8")]
    got = _empty_bodies(tmp_path, [FnSite("src/wrap.rs", "a", ""),
                                   FnSite("src/wrap.rs", "c", "")], lambda _r: recs)
    assert got == {FnSite("src/wrap.rs", "a", "")}
    assert (src / "wrap.rs").read_text() == ("fn a() {}\nfn b() { work() }\n"
                                             "fn c() -> u8 { work() }\n")


# --- admission is the seal move -------------------------------------------------

def _admit(tmp_path: Path, *, commits: list[bool], clean: bool) -> tuple[bool, list[str]]:
    made: list[str] = []
    log: list[str] = []

    def _commit(msg: str) -> bool:
        made.append(msg)
        return commits.pop(0)

    ok = admit_oracle_file(tmp_path, "tests/pin_probe_alt_gen_1.rs", "#[test] fn t() {}\n",
                           commit=_commit, tag=lambda: None,
                           seal_clean=lambda: clean, log=log.append)
    return ok, made + log


def test_admission_is_true_only_when_the_seal_reads_clean(tmp_path: Path) -> None:
    ok, _ = _admit(tmp_path, commits=[True], clean=True)
    assert ok and (tmp_path / "tests/pin_probe_alt_gen_1.rs").exists()


def test_a_failed_commit_withdraws_the_file(tmp_path: Path) -> None:
    ok, said = _admit(tmp_path, commits=[False], clean=True)
    assert not ok and not (tmp_path / "tests/pin_probe_alt_gen_1.rs").exists()
    assert any("HEALTH" in x for x in said)


def test_an_unmoved_seal_withdraws_and_commits_the_removal(tmp_path: Path) -> None:
    ok, said = _admit(tmp_path, commits=[True, True], clean=False)
    assert not ok and not (tmp_path / "tests/pin_probe_alt_gen_1.rs").exists()
    assert any("withdrawn" in x for x in said)


def test_the_observation_run_empties_every_blocking_body_of_the_world(tmp_path: Path) -> None:
    """Live shape (run `1680589-7a6824f8`'s merged tree): the rebuilt
    `context()` wraps every error as the crate's own first, so a test whose
    original path runs one `provide` impl panics in the other on the rebuilt
    tree. Emptying only the body the rebuilt run hit leaves the original's
    path untouched; the run empties the world's whole blocking set."""
    other = FnSite("src/wrap.rs", "probe", "impl Fetch for Wrapper < Other >")
    asked: list[tuple[FnSite, ...]] = []
    red = TestRun(True, failed=frozenset({"reaches_probe"}))
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 3,
                 [(_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))] * 3, [], [])
    deps = _with(deps, run_original_emptied=lambda *a: (
        asked.append(tuple(a[5])), (red, frozenset(a[5])))[1])
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE, other)), deps) >= 1
    assert asked[0] == (_SITE, other)
