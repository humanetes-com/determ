"""The end gate's retro: a pinning test per blocking body.

Run `1680589-7a6824f8` (2026-10-19) ended with every oracle test passing and
two reachable `provide` bodies still `unimplemented!()`; nothing could turn
that into work for t2a. The pass writes a test that reaches the body through
the public API, admits it only when the original crate passes it and the
rebuilt tree fails it inside the body, and moves the oracle seal with it.

Invented sites and shapes only, except one recorded candidate kept as
bytes: the remover has to cut that file, and a stand-in with the same
outline does not.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.fn_routes import FnRoutes, PublicRoute, WorldFacts
from pipeline_core.domain.generation import PIPELINE_GEN_TEST_ORIGIN
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.pinning_test import (
    PIN_CRITERIA,
    PinCriterion,
    PinRefusal,
    TestRun,
    check_criteria,
    compiler_diagnostics,
    judge_pin,
    repair_of,
    withdraw_original_failures,
)
from pipeline_core.domain.rust_scan import attributed_test_fn_names, without_test_fns
from pipeline_core.domain.test_report import HEAD_CUT_MARK
from pipeline_core.prompts.pinning_test import build_pinning_test_prompt

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
        log=lambda _m: None, emit=lambda *_a, **_k: None,
        settle=lambda _pd, _m, _s, text, _w: text, max_attempts=3)


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
    # The failure body is kept from the head. A panic message longer than
    # the bound is shortened and marked, never dropped: it is the observed
    # value, and a cut back to the last line end kept the header alone.
    long = "x" * 2000
    wide = ("running 1 test\ntest reaches_probe ... FAILED\n\nfailures:\n\n"
            "---- reaches_probe stdout ----\n\nthread 'reaches_probe' panicked at "
            "src/wrap.rs:9:9:\n" + long + "\nKEPT-OFF\n\nfailures:\n    "
            "reaches_probe\n\ntest result: FAILED. 0 passed; 1 failed\n")
    wide_reading = reading_of(wide, finished=True)
    assert wide_reading.excerpt.startswith(
        "reaches_probe: thread 'reaches_probe' panicked at src/wrap.rs:9:9:\nxxx")
    assert wide_reading.excerpt.endswith(HEAD_CUT_MARK) and len(wide_reading.excerpt) == 1500
    assert "KEPT-OFF" not in wide_reading.excerpt


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


# --- the writer is told every check the judge applies -------------------------

def _prompt(**kw: Any) -> str:
    args: dict[str, Any] = {
        "crate": "krate", "world": "alt", "world_flags": "--cfg alt",
        "body": site_label(_SITE),
        "signature": "fn probe(&self) -> Option<u8> { unimplemented!() }",
        "routes": [], "api_surface": "pub fn fetch() -> Option<u8>", "example_header": "",
        "blocking": [site_label(_SITE)], "previous": []}
    return build_pinning_test_prompt(**{**args, **kw})


def test_the_prompt_states_every_check_the_judge_refuses_on() -> None:
    """Run `3991945-1b7ae3e9` (2026-10-19): the prompt listed two checks and
    the judge applied a third, the run on the original with the blocking
    bodies emptied. All six candidates discarded what the reaching call
    handed back, and two were refused `unobserved`. Every refusal the judge
    can issue now has its sentence in the prompt, the emptied-body check
    included, and the rule the candidates read as "assert on the reaching
    call" is gone."""
    prompt = _prompt()
    for refusal in PinRefusal:
        stated = [c for c in PIN_CRITERIA if refusal in c.refusals]
        assert len(stated) == 1 and stated[0].statement in prompt, refusal


_DIAG = "error[E0425]: cannot find function `fetch` in this scope"
_COMPILE_WHY = (f"{PinRefusal.ORIGINAL_DID_NOT_RUN}: the file did not build "
                f"or run on the original crate: {_DIAG}")


def test_a_compile_failure_is_sent_back_as_the_same_file() -> None:
    """Run `4184066-ce008393` (2026-10-19): five replies did not compile, and
    the next attempt was asked to write a new file. The attempt after a
    compile failure is that file and its diagnostics. A file that compiled,
    a sibling's compile failure, and a did-not-run with no compiler line
    stay a request for a new file."""
    body = site_label(_SITE)
    other = site_label(FnSite("src/wrap.rs", "other", "impl Fetch for Wrapper < U >"))
    repair = _prompt(previous=[(body, _FILE, _COMPILE_WHY)])
    assert "did not compile" in repair and "Write ONE" not in repair
    assert "fn reaches_probe()" in repair and _DIAG in repair
    assert "How the file is judged" in repair
    fresh = _prompt(previous=[(body, _FILE,
                               f"{PinRefusal.ORIGINAL_FAILED}: left: None")])
    assert "Write ONE" in fresh and "CANDIDATE 1" in fresh
    sibling = _prompt(previous=[(other, _FILE, _COMPILE_WHY)])
    assert "Write ONE" in sibling and "CANDIDATE 1" in sibling
    timeout = _prompt(previous=[(body, _FILE,
                                 f"{PinRefusal.ORIGINAL_DID_NOT_RUN}: did not run: timed out")])
    assert "Write ONE" in timeout
    assert compiler_diagnostics(_COMPILE_WHY) == (
        "the file did not build or run on the original crate: " + _DIAG)
    assert compiler_diagnostics(
        f"{PinRefusal.REBUILT_DID_NOT_RUN}: error: could not compile `krate`") is not None
    second = "#[test]\nfn reaches_probe() {\n    assert!(kept_marker());\n}\n"
    assert repair_of(body, [(body, _FILE, _COMPILE_WHY),
                            (body, second, _COMPILE_WHY)]) == (second, compiler_diagnostics(_COMPILE_WHY))
    assert repair_of(body, [(body, _FILE, _COMPILE_WHY),
                            (body, _FILE, f"{PinRefusal.ORIGINAL_FAILED}: left")]) is None


def test_the_pass_returns_a_file_that_did_not_compile(tmp_path: Path) -> None:
    """The attempt prompt is the builder. After a compile refusal the next
    prompt the pass sends is the repair, not a second fresh request."""
    no_run = TestRun(False, excerpt=_DIAG + "\n")
    prompts: list[str] = []
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```", f"```rust\n{_FILE}```"],
                 [(no_run, TestRun(False), frozenset()),
                  (no_run, TestRun(False), frozenset())],
                 prompts, [])
    deps = _with(deps, max_attempts=2)
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps) == 0
    assert len(prompts) == 2
    assert "Write ONE" in prompts[0] and "Write ONE" not in prompts[1]
    assert "did not compile" in prompts[1] and _DIAG in prompts[1]
    assert "fn reaches_probe()" in prompts[1]


def test_a_judge_check_with_no_sentence_for_the_writer_fails() -> None:
    """Negative control: the pre-fix contract, which stated only the two
    runs, leaves `unobserved`, `unmeasured` and `shape` unstated."""
    two_checks = tuple(c for c in PIN_CRITERIA
                       if PinRefusal.UNOBSERVED not in c.refusals
                       and PinRefusal.SHAPE not in c.refusals)
    try:
        check_criteria(two_checks)
    except RuntimeError as exc:
        assert "unobserved" in str(exc) and "shape" in str(exc)
    else:
        raise AssertionError("an unstated refusal was accepted")
    dup = PIN_CRITERIA + (PinCriterion(frozenset({PinRefusal.SHAPE}), "x"),)
    try:
        check_criteria(dup)
    except RuntimeError as exc:
        assert "stated twice ['shape']" in str(exc)
    else:
        raise AssertionError("a refusal stated twice was accepted")


def test_the_prompt_names_the_whole_blocking_set_it_empties() -> None:
    other = FnSite("src/wrap.rs", "probe", "impl Fetch for Wrapper < Other >")
    prompt = _prompt(blocking=[site_label(_SITE), site_label(other)])
    assert f"- {site_label(other)}" in prompt


def test_a_refusal_reached_for_one_body_is_shown_to_its_sibling(tmp_path: Path) -> None:
    """Run `3991945-1b7ae3e9`: the second `provide` body's first attempt
    opened with none of the three refusals of the first, although the judge
    empties both. Earlier candidates are kept per world. Negative control: a
    body in another world is not shown them."""
    other = FnSite("src/wrap.rs", "probe", "impl Fetch for Wrapper < Other >")
    green = TestRun(True, passed=frozenset({"reaches_probe"}))

    def _run(worlds_of: dict[FnSite, list[Any]]) -> list[str]:
        prompts: list[str] = []
        deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 6,
                     [(_OK_ORIG, green, frozenset())] * 6, prompts, [])
        deps = _with(deps, worlds_for=lambda _pd, s: worlds_of[s])
        assert run_pinning_pass(tmp_path, "krate",
                                EndVerdict(blocking=(_SITE, other)), deps) == 0
        return prompts

    alt = SimpleNamespace(name="alt", cfg_true=["alt"], cfg_false=[])
    shared = _run({_SITE: [alt], other: [alt]})
    assert len(shared) == 6
    assert f"written for {site_label(_SITE)} (refused: rebuilt_passed" in shared[3]
    assert shared[3].count("CANDIDATE ") == 3
    base = SimpleNamespace(name="base", cfg_true=[], cfg_false=["alt"])
    apart = _run({_SITE: [alt], other: [base]})
    assert "CANDIDATE " not in apart[3]


# --- a test the original fails is withdrawn, not held against the file --------

_TWO = ("use krate::*;\n\n"
        "#[test]\nfn reaches_probe() {\n    let v = fetch();\n    assert!(v.is_some());\n}\n\n"
        "/// Guessed: the original returns nothing here.\n"
        "#[test]\nfn reaches_probe_guessed() {\n    assert!(fetch_other().is_some(), \"guessed\");\n}\n")


def test_the_named_test_goes_whole_and_the_others_stay() -> None:
    """The removed test takes its attribute and its doc line, and a test at
    the end of the file leaves no dangling text. Negative controls: a name
    that is not a test of the file, and a file with nothing left to
    compare, are refused rather than guessed."""
    rest = without_test_fns(_TWO, ["reaches_probe_guessed"])
    assert rest is not None
    assert "reaches_probe_guessed" not in rest and "///" not in rest
    assert "fn reaches_probe()" in rest and rest.rstrip().endswith("}")
    assert without_test_fns(_TWO, ["fetch"]) is None
    assert without_test_fns(_TWO, []) is None
    marked = "// determ-pipeline generated integration test\n#[test]\nfn a() {}\n#[test]\nfn b() {}\n"
    kept = without_test_fns(marked, ["a"])
    assert kept == "// determ-pipeline generated integration test\n#[test]\nfn b() {}\n"
    nested = "mod t {\n    #[test]\n    fn a() {}\n    #[test]\n    fn b() {}\n}\n"
    assert without_test_fns(nested, ["b"]) == "mod t {\n    #[test]\n    fn a() {}\n}\n"


def test_a_test_that_cannot_be_cut_alone_is_not_cut() -> None:
    """Each shape where the cut would take more or less than the named test
    is refused: an item sharing its line with the test before or after it,
    two same-named tests in sibling modules, and a same-named test left
    behind at another depth. A refused cut leaves the file to the judge,
    which refuses it whole."""
    assert without_test_fns("#[test]\nfn a() {} #[test] fn b() {}\n", ["b"]) is None
    assert without_test_fns("#[test]\nfn a() {} fn helper() {}\n#[test]\nfn b() {}\n",
                            ["a"]) is None
    siblings = ("mod m {\n    #[test]\n    fn a() {}\n}\nmod n {\n    #[test]\n"
                "    fn a() {}\n}\n#[test]\nfn b() {}\n")
    assert without_test_fns(siblings, ["a"]) is None
    shadowed = "#[test]\nfn a() {}\nmod m {\n    #[test]\n    fn a() {}\n}\n#[test]\nfn b() {}\n"
    assert without_test_fns(shadowed, ["a"]) is None
    one_line = "#[test]\nfn a() {} #[test] fn b() {}\n"
    red = TestRun(True, frozenset({"a"}), frozenset({"b"}))
    assert withdraw_original_failures(one_line, red) is None


def test_only_measured_failures_are_withdrawn_and_never_the_whole_file() -> None:
    both = TestRun(True, frozenset({"reaches_probe"}), frozenset({"reaches_probe_guessed"}),
                   excerpt="guessed")
    cut = withdraw_original_failures(_TWO, both)
    assert cut is not None and cut.withdrawn == {"reaches_probe_guessed"}
    assert "reaches_probe_guessed" in cut.detail and "guessed" in cut.detail
    every = TestRun(True, failed=frozenset({"reaches_probe", "reaches_probe_guessed"}))
    assert withdraw_original_failures(_TWO, every) is None
    assert withdraw_original_failures(_TWO, TestRun(False, excerpt="error[E0425]")) is None
    unreported = TestRun(True, frozenset({"reaches_probe"}))
    assert withdraw_original_failures(_TWO, unreported) is None


def test_a_file_whose_other_test_pins_is_admitted_without_the_failing_one(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Run `4184066-ce008393` (2026-10-19): the one candidate that built
    declared two tests; the original failed one, and the other passed every
    check on its own. The file-level rule refused both and the pass ended
    with no pin. Withdrawn, the rest is judged and admitted, and the
    withdrawn test is on the record later attempts are shown. Negative
    control: skipping the withdrawal does not refuse the file. The judge
    raises, because the failing test can still be cut out."""
    import pipeline_core.application.pinning_tests as pt

    def _run(withdraw: bool) -> tuple[int, list[str], list[str], list[str]]:
        prompts: list[str] = []
        admitted: list[str] = []
        events: list[str] = []
        if not withdraw:
            monkeypatch.setattr(pt, "withdraw_original_failures", lambda *_a: None)
        else:
            monkeypatch.setattr(pt, "withdraw_original_failures", withdraw_original_failures)
        deps = _deps(tmp_path, [f"```rust\n{_TWO}```"] * 3,
                     [(_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))] * 3,
                     prompts, admitted)
        deps = _with(
            deps,
            run_original=lambda *a: (
                TestRun(True, frozenset({"reaches_probe"}),
                        frozenset({"reaches_probe_guessed"}), excerpt="guessed")
                if "reaches_probe_guessed" in a[3]
                else TestRun(True, frozenset({"reaches_probe"}))),
            emit=lambda name, **_k: events.append(name))
        n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps)
        return n, prompts, admitted, events

    n, prompts, admitted, events = _run(True)
    assert n == 1 and len(prompts) == 1
    assert events[:2] == ["task4.pin_withdrawn", "task4.pin_admitted"]
    text = admitted[0].split("\n", 1)[1]
    assert "fn reaches_probe()" in text and "reaches_probe_guessed" not in text
    with pytest.raises(RuntimeError, match="can be removed"):
        _run(False)


def test_the_withdrawn_test_is_shown_to_the_next_attempt(tmp_path: Path) -> None:
    """When the rest of the file is refused too, the next prompt carries both
    records: the withdrawn test with the original's words, and the rest
    with its own refusal."""
    prompts: list[str] = []
    green = TestRun(True, passed=frozenset({"reaches_probe"}))
    deps = _deps(tmp_path, [f"```rust\n{_TWO}```"] * 2,
                 [(_OK_ORIG, green, frozenset())] * 2, prompts, [])
    deps = _with(deps, max_attempts=2, run_original=lambda *a: (
        TestRun(True, frozenset({"reaches_probe"}),
                frozenset({"reaches_probe_guessed"}), excerpt="left: None")
        if "reaches_probe_guessed" in a[3] else TestRun(True, frozenset({"reaches_probe"}))))
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps) == 0
    assert "refused: original_failed: on the original crate reaches_probe_guessed" in prompts[1]
    assert "left: None" in prompts[1] and "refused: rebuilt_passed" in prompts[1]


def test_a_cuttable_mixed_file_is_not_judged_as_a_whole_file_failure() -> None:
    """The pre-fix judge returned original_failed for a file whose other
    test had passed. That return is gone: the failure can be cut out, so
    judging the uncut file raises. A failure that shares its line with the
    next item still cannot be cut, and the file stays original_failed."""
    both = TestRun(True, frozenset({"reaches_probe"}),
                   frozenset({"reaches_probe_guessed"}), excerpt="guessed")
    with pytest.raises(RuntimeError, match="can be removed"):
        judge_pin(_TWO, both, _RED_REBUILT, frozenset({"reaches_probe"}))
    shared = "#[test]\nfn a() {} fn helper() {}\n#[test]\nfn b() {}\n"
    red = TestRun(True, frozenset({"b"}), frozenset({"a"}))
    judged = judge_pin(shared, red, TestRun(True, failed=frozenset({"b"})),
                       frozenset({"b"}))
    assert judged.refusal is PinRefusal.ORIGINAL_FAILED


def test_a_long_refusal_reaches_the_next_prompt_intact(tmp_path: Path) -> None:
    """The reason stored for the next attempt is the whole judgement. A
    second slice at 1500 characters dropped the end of a diagnostic that
    was already bounded once. The marker sits past that slice."""
    marker = "UNIQUE-TAIL-MARKER"
    excerpt = ("help: look at the bound\n" * 80) + marker
    assert len(excerpt) > 1500
    prompts: list[str] = []
    deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 2,
                 [(_OK_ORIG, _RED_REBUILT, frozenset())] * 2, prompts, [])
    deps = _with(deps, max_attempts=2,
                 run_original=lambda *_a: TestRun(False, excerpt=excerpt))
    assert run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps) == 0
    refused = prompts[1].index("refused: ")
    assert prompts[1].index(marker) - refused > 1500


def test_the_recorded_candidate_loses_only_the_failed_test() -> None:
    """The candidate the 2026-10-19 pin pass built, as bytes. The original
    failed the later test. The cut keeps the earlier test, the gate and the
    origin line. Judging the uncut file raises; judging the cut file with
    the remaining test passed on the original and failed in the body admits."""
    body = (Path(__file__).resolve().parent / "fixtures" / "pin"
            / "recorded_two_test_candidate.rs").read_text()
    names = attributed_test_fn_names(body)
    assert len(names) == 2
    kept, failed = names[0], names[1]
    gated = "#![cfg(nightly)]\n// " + PIPELINE_GEN_TEST_ORIGIN + "\n" + body
    original = TestRun(True, frozenset({kept}), frozenset({failed}),
                       excerpt="panicked at the assert")
    cut = withdraw_original_failures(gated, original)
    assert cut is not None and cut.withdrawn == {failed}
    assert f"fn {failed}(" not in cut.text
    assert f"fn {kept}(" in cut.text
    assert PIPELINE_GEN_TEST_ORIGIN in cut.text
    assert cut.text.startswith("#![cfg(nightly)]\n")
    with pytest.raises(RuntimeError, match="can be removed"):
        judge_pin(gated, original, TestRun(True, failed=frozenset({kept})),
                  frozenset({kept}))
    admitted = judge_pin(cut.text, TestRun(True, frozenset({kept})),
                         TestRun(True, failed=frozenset({kept})),
                         frozenset({kept}))
    assert admitted.admitted and admitted.pinning == {kept}


_THREE = _TWO + "\n#[test]\nfn reaches_probe_after() {\n    assert!(fetch().is_some());\n}\n"


def test_a_failure_the_re_run_reveals_is_withdrawn_too(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The re-run on the rest can fail a test that passed beside the
    withdrawn one. It is withdrawn in a second round and the rest is judged.
    Negative control: one round leaves a mixed file and the judge raises,
    which nothing above the pin pass catches."""
    import pipeline_core.application.pinning_tests as pt

    def _original(*a: Any) -> TestRun:
        text = a[3]
        if "reaches_probe_guessed" in text:
            return TestRun(True, frozenset({"reaches_probe", "reaches_probe_after"}),
                           frozenset({"reaches_probe_guessed"}), excerpt="guessed")
        if "reaches_probe_after" in text:
            return TestRun(True, frozenset({"reaches_probe"}),
                           frozenset({"reaches_probe_after"}), excerpt="state")
        return TestRun(True, frozenset({"reaches_probe"}))

    def _run() -> tuple[int, list[str], list[str]]:
        prompts: list[str] = []
        admitted: list[str] = []
        events: list[str] = []
        deps = _deps(tmp_path, [f"```rust\n{_THREE}```"] * 3,
                     [(_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))] * 3,
                     prompts, admitted)
        deps = _with(deps, run_original=_original,
                     emit=lambda name, **_k: events.append(name))
        n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps)
        assert n == 1, events
        return n, admitted, events

    _n, admitted, events = _run()
    assert events[:3] == ["task4.pin_withdrawn", "task4.pin_withdrawn", "task4.pin_admitted"]
    text = admitted[0].split("\n", 1)[1]
    assert "fn reaches_probe()" in text
    assert "reaches_probe_guessed" not in text and "reaches_probe_after" not in text

    once = withdraw_original_failures

    def _first_round_only(text: str, original: TestRun) -> Any:
        return once(text, original) if "reaches_probe_guessed" in text else None

    monkeypatch.setattr(pt, "withdraw_original_failures", _first_round_only)
    with pytest.raises(RuntimeError, match="can be removed"):
        _run()


def test_a_pin_test_that_fails_alone_reads_as_failed_on_the_original(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The pin pass's one reader of "passes on the original" folds the
    alone reading in (`adapters.test_worlds.failing_alone`), so the
    existing withdrawal removes a test that passes only beside another
    test's side effect (run `801510-bb22799f`, 2026-10-20, the t0c instance
    of the class). NEGATIVE CONTROL: with no alone failure the reading is
    the file run's."""
    from pipeline_core.adapters import pin_runs

    out = ("running 2 tests\ntest a_sets ... ok\ntest b_reads ... ok\n"
           "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
           "0 filtered out; finished in 0.00s\n")

    def fake_run(project_dir, rel, text, *, env, timeout, then=None, **_k):  # type: ignore[no-untyped-def]
        if then is not None:
            then(tmp_path, env, out)
        return True, out

    seen: list[tuple[str, frozenset[str]]] = []

    def fake_alone(tree, module, stem, names, *, env, timeout):  # type: ignore[no-untyped-def]
        seen.append((stem, frozenset(names)))
        return ("b_reads",) if lonely else ()

    monkeypatch.setattr(pin_runs, "run_test_file_on_original", fake_run)
    monkeypatch.setattr(pin_runs, "failing_alone", fake_alone)
    lonely = True
    run = pin_runs.run_on_original(tmp_path, "m", "pin_x_gen_1", "", None,
                                   cluster_env=lambda _w: {}, timeout=5)
    assert seen == [("pin_x_gen_1", frozenset({"a_sets", "b_reads"}))]
    assert run.passed == {"a_sets"} and run.failed == {"b_reads"}
    assert "b_reads: passes only beside" in run.excerpt
    text = ("#[test]\nfn a_sets() { std::env::set_var(\"K\", \"1\"); }\n\n"
            "#[test]\nfn b_reads() { assert_eq!(std::env::var(\"K\").unwrap(), \"1\"); }\n")
    cut = withdraw_original_failures(text, run)
    assert cut is not None and cut.withdrawn == {"b_reads"}
    lonely = False
    ctrl = pin_runs.run_on_original(tmp_path, "m", "pin_x_gen_1", "", None,
                                    cluster_env=lambda _w: {}, timeout=5)
    assert ctrl.passed == {"a_sets", "b_reads"} and ctrl.failed == frozenset()


def test_a_pin_writer_requires_a_settle() -> None:
    """No default returns the text as written. A construction that omits
    the settle cannot be built."""
    import dataclasses

    field = next(f for f in dataclasses.fields(PinningDeps) if f.name == "settle")
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


def test_an_admitted_pin_is_settled_into_the_worlds_it_passes_in(
        tmp_path: Path) -> None:
    """The pass hands the judged file to the world settle (read on the
    original) and admits the text it returns. A file the settle finds no
    world for is refused. An explicit identity settle admits the file under
    the gate of the world that wrote it. There is no default that does that."""
    def run(settle: Any) -> tuple[int, list[str]]:
        admitted: list[str] = []
        deps = _deps(tmp_path, [f"```rust\n{_FILE}```"] * 3,
                     [(_OK_ORIG, _RED_REBUILT, frozenset({"reaches_probe"}))] * 3,
                     [], admitted)
        if settle is not None:
            deps = _with(deps, settle=settle)
        n = run_pinning_pass(tmp_path, "krate", EndVerdict(blocking=(_SITE,)), deps)
        return n, admitted

    n, admitted = run(lambda _pd, _m, _s, text, _w: text.split("\n", 1)[1])
    assert n == 1
    assert not admitted[0].split("\n", 1)[1].startswith("#![cfg(")
    n_none, admitted_none = run(lambda *_a: None)
    assert n_none == 0 and admitted_none == []
    n_ctrl, admitted_ctrl = run(None)
    assert n_ctrl == 1
    assert admitted_ctrl[0].split("\n", 1)[1].startswith("#![cfg(alt)]\n")
