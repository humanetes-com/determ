"""A behaviour-file verify round that reports no verdict withdraws only the
captures it can be put on.

Run `15370-6a4cfd45` (2026-10-30): one mis-rendered assertion per world made
`tests/behaviour_<world>_gen_1.rs` fail to compile (`:96:15`, `:104:15`), and
the no-verdict branch withdrew every capture in the file, 24 in stable and 26
in nightly; run `2129106-ead996ab` lost 52 the same way. rustc names the line
of each error, and a binary that compiled can run each unreported test alone,
so both readings attribute. The perturbed-environment branch re-runs a binary
that already compiled; its no-verdict is a death and is attributed alone
under the perturbed env.

Each test carries its negative control: with the attribution patched to name
nobody (the pre-fix branch), the whole file goes.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from pipeline_core.application import emit_behaviour_tests as ebt
from pipeline_core.application.emit_behaviour_tests import (
    behaviour_test_stem,
    capture_line_spans,
    emit_behaviour_tests,
    render_behaviour_test_file,
    replay_fn_name,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_probe import CapturedProbe, captured_spec

_STEM = behaviour_test_stem("stable")


def _spec(name: str) -> BehaviourSpec:
    return captured_spec(
        cfg_cluster="stable",
        probe=CapturedProbe(name=name, given='let e = make("boom");', when="e.to_string()",
                            observe="display"),
        n=0, stdout="boom", default_cluster="stable")


def _libtest(passed: Sequence[str], failed: Sequence[str] = (), *, trailer: bool = True) -> str:
    lines = [f"     Running tests/{_STEM}_gen_1.rs (target/debug/deps/{_STEM}_gen_1-ab)", "",
             f"running {len(passed) + len(failed)} tests"]
    lines += [f"test {n} ... ok" for n in passed]
    lines += [f"test {n} ... FAILED" for n in failed]
    if trailer:
        lines.append(f"test result: ok. {len(passed)} passed; {len(failed)} failed; 0 ignored; "
                     "0 measured; 0 filtered out; finished in 0.00s")
    return "\n".join(lines) + "\n"


def _present(tmp: Path, stem: str) -> list[str]:
    body = (tmp / "tests" / f"{stem}.rs").read_text(encoding="utf-8")
    return re.findall(r"fn (behaviour_\w+)\(\)", body)


def _emit(tmp: Path, specs: list[BehaviourSpec], verify, **kw):  # type: ignore[no-untyped-def]
    (tmp / "tests").mkdir(parents=True, exist_ok=True)
    kw.setdefault("failing_alone", lambda _s, _n, _e: ())
    kw.setdefault("settle", lambda _d, body: body)
    paths, dropped, misses, _emitted = emit_behaviour_tests(
        tmp, "stable", specs, crate="anyhow", verify=verify, **kw)
    return (paths[0] if paths else None), dropped, misses


def _compile_error_in(tmp: Path, stem: str, culprit: str) -> str:
    """rustc's text for a mismatched type inside `culprit`'s test body."""
    lines = (tmp / "tests" / f"{stem}.rs").read_text(encoding="utf-8").split("\n")
    at = next(i for i, ln in enumerate(lines) if f"fn {culprit}(" in ln) + 2
    return ("error[E0308]: mismatched types\n"
            f"  --> tests/{stem}.rs:{at}:15\n   |\n"
            "error: could not compile `anyhow` (test \"x\") due to 1 previous error\n")


def test_a_compile_error_inside_one_capture_withdraws_that_capture_only(tmp_path: Path) -> None:
    specs = [_spec("a"), _spec("bad"), _spec("c")]
    culprit = replay_fn_name(specs[1])

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        present = _present(tmp_path, stem)
        if culprit in present:
            return False, _compile_error_in(tmp_path, stem, culprit)
        return True, _libtest(present)

    dest, dropped, misses = _emit(tmp_path, specs, verify)
    assert dest is not None
    assert dropped == (specs[1].behaviour_id,)
    assert misses[0]["reason"].startswith("replay did not compile: ")
    assert culprit not in dest.read_text(encoding="utf-8")


def test_negative_control_the_pre_fix_branch_withdraws_the_whole_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ebt, "no_verdict_culprits", lambda *_a, **_k: [])
    specs = [_spec("a"), _spec("bad"), _spec("c")]
    culprit = replay_fn_name(specs[1])

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        present = _present(tmp_path, stem)
        if culprit in present:
            return False, _compile_error_in(tmp_path, stem, culprit)
        return True, _libtest(present)

    dest, dropped, _misses = _emit(tmp_path, specs, verify)
    assert dest is None
    assert set(dropped) == {s.behaviour_id for s in specs}


def test_an_error_in_the_shared_header_names_no_capture(tmp_path: Path) -> None:
    specs = [_spec("a"), _spec("b")]
    header_error = ("error[E0432]: unresolved import `anyhow`\n"
                    f"  --> tests/{_STEM}_gen_1.rs:2:5\n"
                    "error: could not compile `anyhow` (test \"x\")\n")
    dest, dropped, _m = _emit(tmp_path, specs, lambda _s, _e: (False, header_error))
    assert dest is None and set(dropped) == {s.behaviour_id for s in specs}


def test_capture_spans_cover_each_test_and_not_the_header() -> None:
    specs = [_spec("a"), _spec("b")]
    body = render_behaviour_test_file(specs, crate="anyhow", cfg_attr="not(x)")
    spans = capture_line_spans(body, specs)
    lines = body.split("\n")
    a, b = (spans[replay_fn_name(s)] for s in specs)
    assert a[1] < b[0]
    assert a[0] > 2  # the gate and the glob sit before the first span
    assert lines[a[0] - 1].strip().startswith("#[") or f"fn {replay_fn_name(specs[0])}(" in lines[a[0] - 1]


def test_a_binary_that_dies_withdraws_the_capture_that_fails_alone(tmp_path: Path) -> None:
    specs = [_spec("a"), _spec("dies"), _spec("c")]
    dying = replay_fn_name(specs[1])
    asked: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def verify(stem: str, _extra: Mapping[str, str]) -> tuple[bool, str]:
        present = _present(tmp_path, stem)
        if dying in present:
            ran = present[:present.index(dying)]
            return False, _libtest(ran, trailer=False) + "error: test failed, to rerun pass `--test x`\n"
        return True, _libtest(present)

    def alone(_stem: str, names: Sequence[str], env: Mapping[str, str]) -> Sequence[str]:
        asked.append((tuple(names), dict(env)))
        return [n for n in names if n == dying]

    dest, dropped, misses = _emit(tmp_path, specs, verify, failing_alone=alone)
    assert dest is not None and dropped == (specs[1].behaviour_id,)
    assert misses[0]["reason"].startswith("replay binary died")
    assert asked[0][1] == {}  # the capture env


def test_a_perturbed_death_is_attributed_alone_under_the_perturbed_env(tmp_path: Path) -> None:
    specs = [_spec("a"), _spec("moves")]
    dying = replay_fn_name(specs[1])
    perturb = {"RUST_BACKTRACE": "1"}
    envs: list[dict[str, str]] = []

    def verify(stem: str, extra: Mapping[str, str]) -> tuple[bool, str]:
        present = _present(tmp_path, stem)
        if extra and dying in present:
            return False, _libtest([n for n in present if n != dying], trailer=False)
        return True, _libtest(present)

    def alone(_stem: str, names: Sequence[str], env: Mapping[str, str]) -> Sequence[str]:
        envs.append(dict(env))
        return [n for n in names if n == dying and env == perturb]

    dest, dropped, _m = _emit(tmp_path, specs, verify, failing_alone=alone, perturb_env=perturb)
    assert dest is not None and dropped == (specs[1].behaviour_id,)
    assert perturb in envs

    # Negative control: the pre-fix branch withdraws both.
    for f in (tmp_path / "tests").iterdir():
        f.unlink()
    import pipeline_core.application.emit_behaviour_tests as mod
    real = mod.no_verdict_culprits
    try:
        mod.no_verdict_culprits = lambda *_a, **_k: []  # type: ignore[assignment]
        dest2, dropped2, _m2 = _emit(tmp_path, specs, verify, failing_alone=alone, perturb_env=perturb)
    finally:
        mod.no_verdict_culprits = real  # type: ignore[assignment]
    assert dest2 is None and set(dropped2) == {s.behaviour_id for s in specs}
