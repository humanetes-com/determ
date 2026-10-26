"""The emitter's runtime gate: a capture becomes part of the oracle only after
it has been COMPILED AND RUN against the original, twice.

WHY THESE EXIST. The first version of this writer checked delimiter balance and
wrote the file. Measured 2026-08-27 against a pristine copy of the original,
that admits two kinds of permanently-red test with nothing anywhere to catch
them — the reference floor is taken on a pristine worktree of `master`, so an
emitted file has no floor entry to absolve it, and the deterministic
cluster-leak guard only inspects diagnostics whose path contains
`.gen_tests_`, which an integration test's does not:

  * a replay that disagrees with its own capture (a value the store mangled, a
    setup that compiles as an example and not as a test);
  * a value that is perfectly stable at any fixed environment and moves with
    `RUST_BACKTRACE` — invisible to the run-twice guard by construction.

Either one enters the reconstruction oracle "as an entry no fill can ever
satisfy, and the fns it covers are re-attempted for the life of the project"
(task0c's `_engine_runtime_gate`, on the class it exists to prevent). The
negative control in each test below is the same specs under a `verify` that
reports green: everything is emitted, and nothing is withdrawn.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from pipeline_core.application.emit_behaviour_tests import (
    behaviour_test_stem,
    emit_behaviour_tests,
    replay_fn_name,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_probe import (
    CapturedProbe,
    captured_spec,
    probe_spec,
)

_STEM = behaviour_test_stem("stable")
Verify = Callable[[str, Mapping[str, str]], tuple[bool, str]]


def _fn_sets_env(body: str, fn: str) -> bool:
    """True when `fn`'s body writes `std::env::set_var` — the test pins its
    own world and cargo extra must not convict it."""
    marker = f"fn {fn}()"
    at = body.find(marker)
    if at < 0:
        return False
    nxt = body.find("\nfn ", at + len(marker))
    chunk = body[at:] if nxt < 0 else body[at:nxt]
    return "std::env::set_var" in chunk


def _spec(name: str, when: str = "e.to_string()", stdout: str = "boom") -> BehaviourSpec:
    return captured_spec(
        cfg_cluster="stable",
        probe=CapturedProbe(name=name, given='let e = anyhow!("boom");', when=when, observe="display"),
        n=0,
        stdout=stdout,
        default_cluster="stable",
    )


def _libtest(passed: list[str], failed: list[str]) -> str:
    lines = [
        f"     Running tests/{_STEM}_gen_1.rs (target/debug/deps/{_STEM}_gen_1-ab)",
        "",
        f"running {len(passed) + len(failed)} tests",
    ]
    lines += [f"test {n} ... ok" for n in passed]
    lines += [f"test {n} ... FAILED" for n in failed]
    verdict = "FAILED" if failed else "ok"
    lines.append(
        f"test result: {verdict}. {len(passed)} passed; {len(failed)} failed; "
        "0 ignored; 0 measured; 0 filtered out; finished in 0.00s"
    )
    return "\n".join(lines) + "\n"


def _verifier(tmp: Path, red: frozenset[str] = frozenset(), red_under_env: frozenset[str] = frozenset()) -> Verify:
    """A `verify` that READS the file the emitter just wrote and reports a
    verdict for exactly the tests it contains.

    Realism is the point: a fake that keeps reporting a test the emitter has
    already removed is not a run of the emitted binary, and a loop pinned
    against one would be pinned against output no cargo call can produce."""

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (tmp / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = re.findall(r"fn (behaviour_\w+)\(\)", body)
        # A test that writes set_var pins its own world; cargo extra must not
        # convict it. That is the load-bearing half of required_env.
        pinned = {n for n in present if _fn_sets_env(body, n)}
        bad = set(red) | (set(red_under_env) - pinned if extra else set())
        failed = [n for n in present if n in bad]
        passed = [n for n in present if n not in bad]
        return not failed, _libtest(passed, failed)

    return verify


def _emit(
    tmp: Path, specs: list[BehaviourSpec], verify: Verify, **kw: object
) -> tuple[Path | None, tuple[str, ...], list[dict[str, str]]]:
    (tmp / "tests").mkdir(parents=True, exist_ok=True)
    # Explicit empty readings. Omitting either callable is a TypeError; these
    # are a check that ran and found nothing to withdraw.
    kw.setdefault("failing_alone", lambda _stem, _names: ())
    kw.setdefault("settle", lambda _dest, body: body)
    dest, dropped, misses, _emitted = emit_behaviour_tests(
        tmp,
        "stable",
        specs,
        crate="anyhow",
        verify=verify,
        **kw,  # type: ignore[arg-type]
    )
    return dest, dropped, misses


def test_a_capture_whose_replay_disagrees_with_it_is_withdrawn(tmp_path: Path) -> None:
    keep, liar = _spec("keeper"), _spec("liar", stdout="a \nb")
    red = frozenset({replay_fn_name(liar)})

    dest, dropped, misses = _emit(tmp_path, [keep, liar], _verifier(tmp_path, red=red))
    assert dropped == (liar.behaviour_id,)
    assert [m["reason"] for m in misses] == ["replay disagreed with the capture"]
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert replay_fn_name(keep) in body and replay_fn_name(liar) not in body

    # NEGATIVE CONTROL: nothing red — both are emitted and nothing is withdrawn.
    green = tmp_path / "green"
    dest2, dropped2, misses2 = _emit(green, [keep, liar], _verifier(green))
    assert dropped2 == () and misses2 == []
    assert dest2 is not None
    body2 = dest2.read_text(encoding="utf-8")
    assert replay_fn_name(keep) in body2 and replay_fn_name(liar) in body2


def test_an_environment_dependent_capture_is_withdrawn(tmp_path: Path) -> None:
    """Green under the capture env, red under the flip, and no recapture.
    NEGATIVE CONTROL of the two-world contract: without a recapture that
    prints a different Then, the mover is still withdrawn. That is what
    shipped."""
    fixed, moving = _spec("stable_value"), _spec("backtrace", when="e.backtrace()")
    env_red = frozenset({replay_fn_name(moving)})

    dest, dropped, misses = _emit(
        tmp_path, [fixed, moving], _verifier(tmp_path, red_under_env=env_red), perturb_env={"RUST_BACKTRACE": "1"}
    )
    assert dropped == (moving.behaviour_id,)
    assert misses[0]["reason"].startswith("environment-dependent")
    assert dest is not None
    assert replay_fn_name(moving) not in dest.read_text(encoding="utf-8")

    # NEGATIVE CONTROL: no perturbation asked for — the second run never
    # happens and the env-dependent capture survives, which is what shipped.
    plain = tmp_path / "unperturbed"
    dest2, dropped2, _m = _emit(plain, [fixed, moving], _verifier(plain, red_under_env=env_red))
    assert dropped2 == ()
    assert dest2 is not None
    assert replay_fn_name(moving) in dest2.read_text(encoding="utf-8")


def test_an_environment_dependent_capture_is_emitted_as_two_worlds(tmp_path: Path) -> None:
    """A recapture that prints a different Then is TWO tests, each pinning
    its world with set_var, not a withdrawal. The original id stays; the
    twin is `::env`."""
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.behaviour_probe import env_twin_id

    fixed, moving = _spec("stable_value"), _spec("backtrace", when="e.backtrace()")
    env_red = frozenset({replay_fn_name(moving)})
    perturb = {"RUST_BACKTRACE": "1"}

    def recapture(spec: BehaviourSpec, extra: Mapping[str, str]) -> str | None:
        assert extra == perturb
        if spec.behaviour_id == moving.behaviour_id:
            return "captured frames\n"
        return None

    dest, dropped, misses = _emit(
        tmp_path,
        [fixed, moving],
        _verifier(tmp_path, red_under_env=env_red),
        perturb_env=perturb,
        recapture=recapture,
    )
    assert dropped == () and misses == []
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    twin_fn = replay_fn_name(dc_replace(moving, behaviour_id=env_twin_id(moving.behaviour_id)))
    assert replay_fn_name(moving) in body
    assert twin_fn in body
    orig_at = body.index(f"fn {replay_fn_name(moving)}()")
    twin_at = body.index(f"fn {twin_fn}()")
    orig_chunk = body[orig_at:twin_at] if orig_at < twin_at else body[orig_at:]
    twin_chunk = body[twin_at:] if twin_at > orig_at else body[twin_at:orig_at]
    assert 'set_var("RUST_BACKTRACE", "0")' in orig_chunk
    assert 'set_var("RUST_BACKTRACE", "1")' in twin_chunk
    assert orig_chunk.index("set_var") < orig_chunk.index("let e = anyhow")
    # A world-pinned capture asserts equality AND reports what it rendered,
    # so a disagreement can be narrowed instead of withdrawn.
    assert 'assert!(actual == "captured frames"' in twin_chunk
    assert "determ-observed[" in twin_chunk


def test_a_file_that_does_not_build_withdraws_everything_and_leaves_no_file(tmp_path: Path) -> None:
    """No verdict at all cannot be attributed to one capture, and a `tests/`
    file that does not compile does not fail one test — it stops the crate
    compiling and takes every verdict with it."""
    specs = [_spec("a"), _spec("b")]
    compile_error = (
        'error[E0425]: cannot find value `nope` in this scope\nerror: could not compile `anyhow` (test "x")\n'
    )
    dest, dropped, misses = _emit(tmp_path, specs, lambda _s, _e: (False, compile_error))
    assert dest is None
    assert set(dropped) == {s.behaviour_id for s in specs}
    assert {m["reason"] for m in misses} == {"replay did not build or reported no verdict"}
    assert list((tmp_path / "tests").iterdir()) == []


def test_the_file_is_cluster_gated_and_a_re_entry_rewrites_it(tmp_path: Path) -> None:
    """`tests/` is symlinked into every cluster's filled tree, so an ungated
    file is ACTIVE in clusters it was never captured under. And a second t0d
    entry must rewrite one file, not stack a second copy of the same tests."""
    specs = [_spec("a")]
    dest, _d, _m = _emit(tmp_path, specs, _verifier(tmp_path), cfg_attr="not(any(nightly))")
    assert dest is not None and dest.name == f"{_STEM}_gen_1.rs"
    assert dest.read_text(encoding="utf-8").startswith("#![cfg(not(any(nightly)))]")
    dest2, _d2, _m2 = _emit(tmp_path, specs, _verifier(tmp_path), cfg_attr="not(any(nightly))")
    assert dest2 == dest
    assert [p.name for p in sorted((tmp_path / "tests").iterdir())] == [f"{_STEM}_gen_1.rs"]


def test_an_authored_file_of_the_same_name_is_never_deleted_or_overwritten(tmp_path: Path) -> None:
    """A `tests/<stem>_gen_<N>.rs` without the writer's origin marker is the
    crate's own oracle. `_clear_previous` leaves it, so the destination has to
    step over it rather than write through it."""
    (tmp_path / "tests").mkdir(parents=True)
    authored = tmp_path / "tests" / f"{_STEM}_gen_1.rs"
    authored.write_text("#[test]\nfn authored() {}\n", encoding="utf-8")
    dest, _d, _m = _emit(tmp_path, [_spec("a")], _verifier(tmp_path))
    assert authored.read_text(encoding="utf-8") == "#[test]\nfn authored() {}\n"
    assert dest is not None and dest.name == f"{_STEM}_gen_2.rs"


def test_a_suite_of_legacy_blobs_emits_nothing_and_withdraws_nothing(tmp_path: Path) -> None:
    """The state on disk today: `render_replay_test` returns "" for a
    free-form capture, so there is nothing to verify and nothing to drop."""
    blob = probe_spec(
        cfg_cluster="stable",
        unit="Error::backtrace",
        n=0,
        program='fn main() { println!("a"); println!("b"); }',
        stdout="a\nb",
    )
    (tmp_path / "tests").mkdir(parents=True)
    assert _emit(tmp_path, [blob], _verifier(tmp_path)) == (None, (), [])


def test_an_unbalanced_render_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """A broken file in `tests/` does not fail one test — it stops the whole
    crate compiling and takes every verdict with it, so the check that already
    guards t0c's writer guards this one. A check that refuses every block
    drops each capture and writes nothing."""
    specs = [_spec("a")]
    dest, dropped, misses = _emit(tmp_path, specs, _verifier(tmp_path), balanced=lambda _b: False)
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert misses[0]["reason"] == "replay refused: delimiters unbalanced"
    assert list((tmp_path / "tests").iterdir()) == []


def test_one_unbalanced_capture_is_dropped_and_the_others_are_emitted(tmp_path: Path) -> None:
    """The check is per replay block. One capture whose own text fails it
    is the only drop; the assembled file of the survivors is checked again
    and, when it passes, is verified. A check that fails only the assembled
    file still writes nothing."""
    from pipeline_core.domain.rust_scan import rust_delimiters_balanced

    good = _spec("keeper")
    bad = _spec("broken", when="foo(")
    dest, dropped, misses = _emit(tmp_path, [good, bad], _verifier(tmp_path))
    assert dropped == (bad.behaviour_id,)
    assert misses[0]["reason"] == "replay refused: delimiters unbalanced"
    assert "replay_failed" not in misses[0]
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert replay_fn_name(good) in body and replay_fn_name(bad) not in body
    assert rust_delimiters_balanced(body)

    # The same two blocks are each balanced, and the file that contains both
    # is not. Nothing is written.
    both = tmp_path / "both"

    def file_only(text: str) -> bool:
        return "keeper" not in text or "broken" not in text

    dest2, dropped2, misses2 = _emit(both, [good, bad], _verifier(both), balanced=file_only)
    assert dest2 is None
    assert set(dropped2) == {good.behaviour_id, bad.behaviour_id}
    assert list((both / "tests").iterdir()) == []


def test_a_raw_string_capture_is_emitted(tmp_path: Path) -> None:
    """The renderer writes a captured quote as a raw literal. That literal
    is balanced, so a sibling capture is not refused with it."""
    plain = _spec("plain")
    quoted = _spec("quoted", stdout='he said "hi (x')
    dest, dropped, _misses = _emit(tmp_path, [plain, quoted], _verifier(tmp_path))
    assert dropped == ()
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert 'r#"he said "hi (x"#' in body
    assert replay_fn_name(plain) in body and replay_fn_name(quoted) in body


def _libtest_in_cargo_order(passed: list[str], failed: dict[str, tuple[str, str]]) -> str:
    """libtest's own layout: every status line first, then one
    `---- name stdout ----` block per failure, then the name list and the
    trailer (checked against a real `cargo test --test-threads=1` on
    2026-10-26)."""
    status = [f"test {n} ... ok" for n in passed] + [f"test {n} ... FAILED" for n in failed]
    lines = [f"running {len(status)} tests", *status, "", "failures:", ""]
    for n, (left, right) in failed.items():
        lines += [f"---- {n} stdout ----", "",
                  f"thread '{n}' (1) panicked at tests/x.rs:1:1:",
                  "assertion `left == right` failed", f"  left: {left}", f" right: {right}", ""]
    lines += ["", "failures:", *[f"    {n}" for n in failed], "",
              f"test result: FAILED. {len(passed)} passed; {len(failed)} failed; "
              "0 ignored; 0 measured; 0 filtered out; finished in 0.00s"]
    return "\n".join(lines)


def _first_line_reader(output: str, fn_name: str) -> str:
    """The reader this module shipped first: from the first line naming the
    test to the next `test ` line. Kept here as the negative control."""
    lines = output.splitlines()
    start = next((i for i, ln in enumerate(lines) if fn_name in ln), -1)
    if start < 0:
        return ""
    end = min(len(lines), start + 40)
    for j in range(start + 1, end):
        if lines[j].startswith("test result:") or (
                lines[j].startswith("test ") and fn_name not in lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def test_a_withdrawn_replay_records_whether_that_test_failed(tmp_path: Path) -> None:
    """The miss row carries the round's verdict for that replay name. A
    capture the last verify had already passed is not given an assertion it
    did not fail. A capture the verdict failed keeps its own block, left and
    right included, and not another failure's."""
    liar = _spec("liar", stdout="a \nb")
    fibber = _spec("fibber", stdout="c")
    keeper = _spec("keeper")
    asserted = {replay_fn_name(liar): ("observed_liar", "a\\nb"),
                replay_fn_name(fibber): ("observed_fibber", "c")}

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (tmp_path / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = re.findall(r"fn (behaviour_\w+)\(\)", body)
        failed = {n: asserted[n] for n in present if n in asserted}
        passed = [n for n in present if n not in asserted]
        return not failed, _libtest_in_cargo_order(passed, failed)

    _dest, dropped, misses = _emit(tmp_path, [keeper, liar, fibber], verify)
    assert set(dropped) == {liar.behaviour_id, fibber.behaviour_id}
    rows = {m["unit"]: m for m in misses}
    for spec, own, other in ((liar, "observed_liar", "observed_fibber"),
                             (fibber, "observed_fibber", "observed_liar")):
        row = rows[spec.behaviour_id]
        assert row["replay_failed"] == "yes"
        assert f"left: {own}" in row["replay_output"] and "right:" in row["replay_output"]
        assert other not in row["replay_output"]

    # Negative control: the first-line reader on the same output records the
    # status line alone for the first failure and the other failure's block
    # for the second.
    out = _libtest_in_cargo_order([replay_fn_name(keeper)], asserted)
    first, second = asserted
    assert _first_line_reader(out, first) == f"test {first} ... FAILED"
    assert "observed_liar" in _first_line_reader(out, second)

    # Three rounds each fail a new capture and pass the rest. The ones the
    # last round passed are withdrawn by the budget, and the row says they
    # were not in that failed set.
    budget = tmp_path / "budget"
    specs = [_spec(f"s{i}") for i in range(6)]
    order = [replay_fn_name(s) for s in specs]

    def budget_verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (budget / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = [n for n in order if f"fn {n}()" in body]
        return False, _libtest(present[1:], present[:1])

    _dest2, _dropped2, misses2 = _emit(budget, specs, budget_verify)
    by_id = {m["unit"]: m for m in misses2}
    budget_rows = [m for m in misses2 if m["reason"] == "replay still red after the verification budget"]
    assert len(budget_rows) == 3
    assert {m["replay_failed"] for m in budget_rows} == {"no"}
    assert all("replay_output" not in m for m in budget_rows)
    disagreed = [m for m in misses2 if m["reason"] == "replay disagreed with the capture"]
    assert disagreed and {m["replay_failed"] for m in disagreed} == {"yes"}
    assert by_id


def test_a_red_verdict_that_names_no_capture_withdraws_rather_than_retries(tmp_path: Path) -> None:
    """Dropping cannot make this file green — the failure belongs to nothing
    the emitter can remove. Retrying it would spend the whole round budget
    re-rendering the same thing and end in the same place."""

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        return False, _libtest(["behaviour_probe_stable_a_0"], ["someone_elses_test"])

    specs = [_spec("a")]
    dest, dropped, misses = _emit(tmp_path, specs, verify)
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert misses[-1]["reason"] == "replay red on a verdict naming no capture in the file"
    assert list((tmp_path / "tests").iterdir()) == []


def test_a_completed_run_that_never_ran_the_emitted_tests_is_not_a_pass(tmp_path: Path) -> None:
    """An integration test whose `#![cfg]` excludes it compiles as `running 0
    tests` and prints a green trailer. The trailer says the binary ran; it does
    not say it ran OUR tests, and a predicate that stops at the trailer admits
    every capture in a file that executed none of them, under a log line
    claiming it was verified against the original.

    Live trigger: `_verify_behaviour_tests` runs `cargo test -p <crate> --test
    <stem>` and passes no `--features`, while a `feature/<pred>` cluster
    contributes no RUSTFLAGS and `cluster_cfg_attr` gates the file on
    `any(<pred>, feature = "<pred>")`.

    The trailer is still what distinguishes this from a build failure, and the
    two must not be reported as the same thing: this file compiled, so the fix
    is a cfg/env one, not a broken render."""
    zero = (
        f"     Running tests/{_STEM}_gen_1.rs "
        f"(target/debug/deps/{_STEM}_gen_1-ab)\n"
        "\nrunning 0 tests\n"
        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.00s\n"
    )
    specs = [_spec("a")]
    dest, dropped, misses = _emit(tmp_path, specs, lambda _s, _e: (True, zero))
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert list((tmp_path / "tests").iterdir()) == []
    assert misses[-1]["reason"] == "replay completed but did not run the emitted test — not verified"

    # NEGATIVE CONTROL, the class this must not swallow: the same green trailer
    # with the emitted test actually reported. Nothing is withdrawn — a
    # coverage check that fired here would refuse every healthy capture.
    green = tmp_path / "green"
    dest2, dropped2, misses2 = _emit(green, specs, _verifier(green))
    assert dest2 is not None and dropped2 == () and misses2 == []


def test_a_perturbed_run_that_never_ran_the_emitted_tests_is_not_a_pass(tmp_path: Path) -> None:
    """Same rule on the second run: a green trailer over zero of our tests
    rules nothing out about environment-dependence, so it may not stand in for
    the perturbed evidence."""
    names = [replay_fn_name(_spec("a"))]

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        if extra:
            return True, _libtest([], [])  # completed, ran nothing of ours
        return True, _libtest(names, [])

    specs = [_spec("a")]
    dest, dropped, misses = _emit(tmp_path, specs, verify, perturb_env={"RUST_BACKTRACE": "1"})
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert list((tmp_path / "tests").iterdir()) == []
    assert misses[-1]["reason"].startswith("environment-perturbed replay completed but did not run")


def test_a_perturbed_run_with_no_verdict_withdraws(tmp_path: Path) -> None:
    """No trailer cannot rule env-dependence out. Keeping the file admitted
    a capture whose value may move with the environment as a specification
    of the crate. Fail closed: withdraw, leave no file.

    NEGATIVE CONTROL: the same specs under a perturbed run that reports
    green stay emitted — that is the path that shipped as keep-on-unknown."""
    specs = [_spec("a")]
    names = [replay_fn_name(specs[0])]

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        if extra:
            return False, "error: could not run\n"
        return True, _libtest(names, [])

    dest, dropped, misses = _emit(tmp_path, specs, verify, perturb_env={"RUST_BACKTRACE": "1"})
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert misses[-1]["reason"].startswith("perturbed replay reported no verdict")
    assert list((tmp_path / "tests").iterdir()) == []

    green = tmp_path / "green"
    dest2, dropped2, _m = _emit(green, specs, _verifier(green), perturb_env={"RUST_BACKTRACE": "1"})
    assert dest2 is not None and dropped2 == ()


def test_a_file_that_stays_red_past_the_budget_leaves_nothing_behind(tmp_path: Path) -> None:
    """Each round drops at least one capture, so the loop normally ends green
    or empty. The bound is a backstop for a file that keeps failing somewhere
    NEW every round, and on it the rest is withdrawn rather than left failing
    in the oracle for the life of the project."""
    specs = [_spec(f"s{i}") for i in range(6)]
    order = [replay_fn_name(s) for s in specs]
    rounds = iter(range(99))

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (tmp_path / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = [n for n in order if f"fn {n}()" in body]
        next(rounds)
        return False, _libtest(present[1:], present[:1])  # a new one each round

    dest, dropped, misses = _emit(tmp_path, specs, verify)
    assert dest is None and set(dropped) == {s.behaviour_id for s in specs}
    assert [m["reason"] for m in misses].count("replay still red after the verification budget") == 3
    assert list((tmp_path / "tests").iterdir()) == []


def test_a_perturbed_verdict_naming_no_capture_withdraws_the_file(tmp_path: Path) -> None:
    """Same reasoning as the capture-env branch: a red run the emitter cannot
    attribute to one of its own tests cannot be fixed by dropping one."""

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (tmp_path / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = re.findall(r"fn (behaviour_\w+)\(\)", body)
        if extra:
            return False, _libtest(present, ["someone_elses_test"])
        return True, _libtest(present, [])

    specs = [_spec("a")]
    dest, dropped, misses = _emit(tmp_path, specs, verify, perturb_env={"RUST_BACKTRACE": "1"})
    assert dest is None and set(dropped) == {specs[0].behaviour_id}
    assert misses[-1]["reason"].startswith("perturbed replay red on a verdict")
    assert list((tmp_path / "tests").iterdir()) == []


def test_a_harvested_spec_and_a_foreign_file_are_both_left_alone(tmp_path: Path) -> None:
    """`replayable` takes probe specs only — a harvested test assertion is
    already a `#[test]` in the crate. And `_clear_previous` walks `tests/`, so
    it must ignore names that are not this cluster's and files it cannot read."""
    from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSubKind, ObservableOutcome

    harvested = BehaviourSpec(
        "testassert::tests/x.rs::t::0",
        "",
        "e.to_string()",
        then=ObservableOutcome.display("boom"),
        subkind=BehaviourSubKind.OUTCOME,
    )
    (tmp_path / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "error_gen_1.rs").write_text("// someone else\n", encoding="utf-8")
    (tmp_path / "tests" / "not_a_gen_test.rs").write_text("// authored\n", encoding="utf-8")
    specs = [harvested, _spec("a")]
    dest, _d, _m = _emit(tmp_path, specs, _verifier(tmp_path))
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert body.count("#[test]") == 1  # the harvested one is not ours
    assert (tmp_path / "tests" / "error_gen_1.rs").exists()
    assert (tmp_path / "tests" / "not_a_gen_test.rs").exists()


def test_a_verify_that_raises_leaves_no_file_behind(tmp_path: Path) -> None:
    """The file is on disk from `write_text` onwards. Every ordinary exit
    removes it; an exception did not, and `run_task0d` catches whatever this
    raises, logs `non-fatal, suite kept` and saves the suite — so the crate was
    left holding a `tests/` file no verification run ever saw green, with every
    capture in it still listed to t1.5 as a MUST.

    NEGATIVE CONTROL: the same specs under a verify that returns instead of
    raising still emit the file, so the guard cannot be a blanket unlink."""

    def boom(_stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        raise RuntimeError("cargo runner blew up")

    specs = [_spec("a")]
    with pytest.raises(RuntimeError, match="blew up"):
        _emit(tmp_path, specs, boom)
    assert list((tmp_path / "tests").iterdir()) == []

    green = tmp_path / "green"
    dest, dropped, _m = _emit(green, specs, _verifier(green))
    assert dest is not None and dest.exists() and dropped == ()


def _render_verifier(tmp: Path, renders: Mapping[str, str],
                     env_renders: Mapping[str, str] | None = None,
                     env_key: str = "SOME_KNOB") -> Verify:
    """A `verify` that EVALUATES each emitted assertion against what the test
    binary would render in that test's world, and reports a failure the way
    the emitted file does — with the observed value in the marker.

    A test's world is its own `set_var` when it pins one (that is what
    `required_env` is for), else the env cargo was given. `renders` is the
    unperturbed world, `env_renders` the perturbed one; a fn with no entry
    passes."""

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        body = (tmp / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
        present = re.findall(r"fn (behaviour_\w+)\(\)", body)
        passed, failed, marks = [], [], []
        for fn in present:
            chunk = body[body.index(f"fn {fn}()"):]
            chunk = chunk[:chunk.index("\n}\n") + 1]
            if f'set_var("{env_key}", "1")' in chunk:
                world = env_renders or {}
            elif f'set_var("{env_key}", "0")' in chunk:
                world = renders
            else:
                world = (env_renders or {}) if extra else renders
            actual = world.get(fn)
            q = r'"((?:[^"\\]|\\.)*)"'
            lit = (re.search(r"starts_with\(" + q, chunk)
                   or re.search(r"actual == " + q, chunk)
                   or re.search(r"assert_eq!\(.*, " + q + r"\);", chunk))
            if actual is None or lit is None:
                passed.append(fn)
                continue
            want = lit.group(1).encode().decode("unicode_escape")
            ok = (actual.startswith(want) and len(actual) > len(want)
                  if "starts_with" in chunk else actual == want)
            (passed if ok else failed).append(fn)
            if not ok:
                marks.append(f"thread '{fn}' panicked:\n"
                             f"determ-observed[{fn}]<<<{actual}>>>")
        return not failed, _libtest(passed, failed) + "\n".join(marks)

    return verify


def test_a_world_pinned_capture_is_narrowed_to_what_the_replay_shares(
        tmp_path: Path) -> None:
    """The capture runs in the probe binary and the replay in the test
    binary, so the part of a render that names the rendering PROCESS differs
    between them — and two runs of the same probe agree on exactly that part,
    which is why the probe cannot find it alone. Before this, the twin
    carried the probe's whole render and was withdrawn: run
    `330242-0be2c3e1` (2026-09-03), `1 capture(s) did not reproduce as a test
    against the original and were withdrawn`, both clusters, the only Debug
    probe of each."""
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.behaviour_probe import env_twin_id

    fixed, moving = _spec("stable_value"), _spec("moving", when="e.backtrace()")
    perturb = {"SOME_KNOB": "1"}
    twin_fn = replay_fn_name(
        dc_replace(moving, behaviour_id=env_twin_id(moving.behaviour_id)))
    # The probe binary rendered one tail; the test binary renders another.
    probe_render = "shared head\nat 0x41 probe\n"
    replay_render = "shared head\nat 0x77 test\n"
    verify = _render_verifier(
        tmp_path,
        {replay_fn_name(moving): "boom"},    # the capture world is stable
        {replay_fn_name(moving): replay_render, twin_fn: replay_render})

    dest, dropped, misses = _emit(
        tmp_path, [fixed, moving], verify, perturb_env=perturb,
        recapture=lambda s, _e: (probe_render
                                 if s.behaviour_id == moving.behaviour_id
                                 else None))
    assert dropped == () and misses == []
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    twin_chunk = body[body.index(f"fn {twin_fn}()"):]
    assert '.starts_with("shared head\\nat 0x")' in twin_chunk
    assert "actual.len() > " in twin_chunk
    assert "assert!(actual ==" not in twin_chunk


def test_a_world_pinned_capture_sharing_nothing_with_the_replay_is_withdrawn(
        tmp_path: Path) -> None:
    """Narrowing is not a licence to keep everything: a replay that shares no
    leading text with the capture pins nothing, and the capture goes."""
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.behaviour_probe import env_twin_id

    fixed, moving = _spec("stable_value"), _spec("moving", when="e.backtrace()")
    twin_fn = replay_fn_name(
        dc_replace(moving, behaviour_id=env_twin_id(moving.behaviour_id)))
    verify = _render_verifier(
        tmp_path, {replay_fn_name(moving): "boom"},
        {replay_fn_name(moving): "nothing alike", twin_fn: "nothing alike"})

    dest, dropped, misses = _emit(
        tmp_path, [fixed, moving], verify, perturb_env={"SOME_KNOB": "1"},
        recapture=lambda s, _e: ("probe side only"
                                 if s.behaviour_id == moving.behaviour_id
                                 else None))
    # The TWIN goes; the capture-world original, which reproduces, stays.
    from pipeline_core.domain.behaviour_probe import env_twin_id as _tid
    assert dropped == (_tid(moving.behaviour_id),)
    assert "replay disagreed with the capture" in misses[0]["reason"]
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert twin_fn not in body and replay_fn_name(moving) in body


def test_a_capture_that_passes_only_beside_another_is_withdrawn(tmp_path: Path) -> None:
    """The file run is green, and one replay fails when run alone in its own
    process (`adapters.test_worlds.failing_alone`). That capture depends on
    another test's side effect on process state; it is withdrawn and the
    file is re-rendered without it. Run `801510-bb22799f` (2026-10-20) is the
    t0c instance of the class. An alone reading that names nobody keeps both.
    Omitting the reading is refused, not a way to keep both."""
    keep, leaner = _spec("keeper"), _spec("leaner")
    asked: list[tuple[str, tuple[str, ...]]] = []

    def failing_alone(stem: str, names: Sequence[str]) -> Sequence[str]:
        asked.append((stem, tuple(names)))
        return [n for n in names if n == replay_fn_name(leaner)]

    dest, dropped, misses = _emit(tmp_path, [keep, leaner], _verifier(tmp_path),
                                  failing_alone=failing_alone)
    assert dropped == (leaner.behaviour_id,)
    assert [m["reason"] for m in misses] == [
        "passes only beside the file's other tests: red when run alone in its "
        "own process"]
    assert dest is not None
    body = dest.read_text(encoding="utf-8")
    assert replay_fn_name(keep) in body and replay_fn_name(leaner) not in body
    assert asked[0][1] == tuple(sorted({replay_fn_name(keep), replay_fn_name(leaner)}))

    ctrl = tmp_path / "ctrl"
    dest2, dropped2, _m2 = _emit(ctrl, [keep, leaner], _verifier(ctrl))
    assert dropped2 == () and dest2 is not None
    assert replay_fn_name(leaner) in dest2.read_text(encoding="utf-8")


def test_the_verified_file_is_settled_into_its_measured_worlds(tmp_path: Path) -> None:
    """After verification the emitter hands the file to the world settle
    (`test_worlds.settle_test_gate`), which rewrites its gate to the worlds
    it passes in. A file the settle finds no world for is not kept.
    An explicit settle that returns the body keeps the gate it was written
    under (`cfg_attr`). Omitting the settle is refused."""
    keep = _spec("keeper")
    settled: list[str] = []

    def settle(dest: Path, body: str) -> str | None:
        settled.append(dest.name)
        dest.write_text(body.split("\n", 1)[1], encoding="utf-8")   # ungated
        return ""

    dest, dropped, _m = _emit(tmp_path, [keep], _verifier(tmp_path),
                              cfg_attr="own_world", settle=settle)
    assert dropped == () and dest is not None and settled == [dest.name]
    assert not dest.read_text(encoding="utf-8").startswith("#![cfg(")

    nowhere = tmp_path / "nowhere"
    dest2, dropped2, misses2 = _emit(nowhere, [keep], _verifier(nowhere),
                                     cfg_attr="own_world",
                                     settle=lambda _d, _b: None)
    assert dest2 is None and dropped2 == (keep.behaviour_id,)
    assert "world settle" in misses2[0]["reason"]

    ctrl = tmp_path / "ctrl"
    dest3, _d3, _m3 = _emit(
        ctrl, [keep], _verifier(ctrl), cfg_attr="own_world",
        settle=lambda _dest, body: body)
    assert dest3 is not None
    assert dest3.read_text(encoding="utf-8").startswith("#![cfg(own_world)]")

    bare = tmp_path / "bare"
    (bare / "tests").mkdir(parents=True)
    with pytest.raises(TypeError):
        emit_behaviour_tests(
            bare, "stable", [keep], crate="anyhow", verify=_verifier(bare),
            failing_alone=lambda _s, _n: ())
    with pytest.raises(TypeError):
        emit_behaviour_tests(
            bare, "stable", [keep], crate="anyhow", verify=_verifier(bare),
            settle=lambda _d, body: body)
    with pytest.raises(TypeError):
        emit_behaviour_tests(
            bare, "stable", [keep], crate="anyhow", verify=_verifier(bare),
            failing_alone=None, settle=lambda _d, body: body)  # type: ignore[arg-type]
    assert list((bare / "tests").glob("*.rs")) == []
