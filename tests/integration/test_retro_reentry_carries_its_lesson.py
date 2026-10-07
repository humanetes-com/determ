"""Integration: a fix loop that re-enters after a retrospective carries the
lesson it re-entered on, and a round whose lesson the screen would withhold
does not re-enter.

Real pieces: cargo (a body that does not compile, and the pipeline's own
compile reading of it), the pipeline's fill roles for splice, revert, cargo
and the retrospective store (`pipeline._APP.fill_roles`, which persists the
record to disk and reads it back), and `FillOneFn`. Scripted pieces: the
model's body and the retrospective's answer, which count what the loop spends.

The class, run `271137-e728ae95` (2026-09-23): `src/error.rs::construct_from_
boxed` logged `retro round 1 kept the error floor improving — re-entering the
fix loop with the new guidance`, then `recorded no tree premise — NOT injected
(no_tree_premise)` on every re-entry prompt, and stopped on the same E0277. A
compile-taught lesson whose failed body named no stub carries no tree premise,
so the screen retired it inside the loop that had just written it. The crate
is invented: one stub, a body that never compiles, no sibling stub.

Gated on `INTEGRATION=1` (real cargo).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_CLUSTER = "main"
_CARGO_TOML = """[package]
name = "meters"
version = "0.1.0"
edition = "2021"
"""
_LIB = """pub struct Meter(pub u8);

impl Meter {
    pub fn reading(&self) -> u8 {
        unimplemented!()
    }
}
"""
_TESTS = """use meters::Meter;

#[test]
fn test_reading() {
    assert_eq!(Meter(3).reading(), 3);
}
"""
# Never compiles: `u8 + &str`.
_BODY = 'self.0 + "one"'
# Never compiles either, on a different error set (`E0599`). Alternating the
# two keeps each attempt's error set different from the one before, so the
# loop stops on a stalled floor; the same body every attempt stops it on a
# repeated error set, which ends the unit for the fill.
_OTHER = "self.0.no_such_method()"
_LESSON = "Add an integer to the field; a string literal is not a number here."


class _Tel:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.events: list[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        self.events.append(event)

    def check_stop(self, context: str) -> None: ...


class _Delegate:
    def __init__(self, inner: Any, **overrides: Any) -> None:
        self._inner = inner
        for k, v in overrides.items():
            setattr(self, k, v)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(_CARGO_TOML)
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    (project / "tests" / "meters.rs").write_text(_TESTS)
    (cluster / "tests").symlink_to(project / "tests")
    (project / ".skeleton" / "_failures").mkdir(parents=True)
    return project


def _run_unit(project: Path, monkeypatch: pytest.MonkeyPatch,
              bodies: tuple[str, ...] = (_BODY, _OTHER), max_stalls: int = 2,
              ) -> tuple[_Tel, list[str], int]:
    import pipeline
    from pipeline_core.application.reconstruct.context import PerFnPassCtx
    from pipeline_core.application.reconstruct.fill_one_fn import (
        FillCaps,
        FillOneFn,
    )
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.models import FnLocator, Stub

    cluster = project / ".skeleton-filled" / _CLUSTER
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(cluster / "target")
    roles = pipeline._APP.fill_roles
    prompts: list[str] = []
    diagnoses = [0]

    # The model is scripted at the transport: the real per-fn ask and the
    # real conversation run, so `prompts` is every user turn the model
    # would read. Since every fill unit became one conversation
    # (2026-09-24), later attempts are retry turns on the transcript; a
    # stub of `ask_per_fn_body` never opened the conversation, so every
    # attempt was re-built as a first attempt, which carries no lesson, and
    # the test failed while the live re-entry turn carried it (run
    # `118933-877c43bd`, prompt `8e5fa66a…`).
    from pipeline_core.application.reconstruct.unit_conversation import (
        ConversationPorts,
        UnitConversation,
    )

    def ask_turns(messages: list[dict[str, Any]], **_k: Any) -> str:
        prompts.append(str(messages[-1]["content"]))
        body = bodies[min(len(prompts), len(bodies)) - 1]
        if len(prompts) > len(bodies):
            body = bodies[(len(prompts) - 1) % len(bodies)]
        return f"<BODY>\n{body}\n</BODY>"

    def open_conversation(label: str, *, route: str = "per-fn",
                          sources: object = None,
                          max_pulls: int = 2) -> UnitConversation:
        return UnitConversation(
            ConversationPorts(ask_turns=ask_turns, log=lambda _m: None),
            label=label, route=route)

    def diagnose(**_k: Any) -> dict[str, Any]:
        diagnoses[0] += 1
        return {"signal": "", "category": "WRONG_TYPE_CHOICE",
                "guidance": _LESSON}

    tel = _Tel()
    fill = FillOneFn(
        tel=tel, mut=roles.mut, cargo=roles.cargo,
        prompt=_Delegate(roles.prompt, open_conversation=open_conversation),
        retros=_Delegate(roles.retros, diagnose=diagnose,
                         extract_self_note=lambda *a, **k: None),
        tests=roles.tests, stall=roles.stall,
        caps=FillCaps(per_fn_fix_hard_cap=6, per_fn_fix_max_stalls=max_stalls,
                      fix_err_cap=4000))
    unit = FnLocator("src/lib.rs", "reading")
    ctx = PerFnPassCtx(
        project_dir=project, target_src_dir=cluster, module_name="meters",
        cluster_name=_CLUSTER, check_cmd="cargo check --offline --tests",
        cluster_env=env, fr_path=None, tests_dir_files=[],
        macros_section="", invariants_section="", crate_type_records=[],
        src_for_macros=cluster / "src",
        failures_dir=project / ".skeleton" / "_failures",
        tdd_frontier_tests=FailureFrontier.of_direct({unit: {"test_reading"}}),
        tdd_test_cmd="cargo test --offline --no-fail-fast",
        splice_check_lock=threading.Lock(), retros_cache={}, idiom_blocks=[],
        pass_n=1, total=1, cluster_cfg_set=None, still_stubbed=frozenset())
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    result = fill.run(1, Stub(file="src/lib.rs", fn_name="reading",
                              container="impl Meter"), ctx=ctx)
    assert not result.filled
    return tel, prompts, diagnoses[0]


def _stored(project: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in project.rglob("*retro*.json"):
        data = json.loads(path.read_text())
        out.extend(v for v in data.values() if isinstance(v, dict))
    return out


def test_the_re_entered_loop_carries_the_lesson_it_wrote(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain.retro_gate import FIX_LOOP_SCOPE
    project = _workspace(tmp_path)
    tel, prompts, diagnoses = _run_unit(project, monkeypatch)
    assert any("E0277" in m for m in tel.logs), (
        "premise: the body must fail to compile\n" + "\n".join(tel.logs))
    assert "phase_c.fn.retro_retry" in tel.events, "\n".join(tel.logs)
    assert any(_LESSON in p for p in prompts), (
        "the re-entry prompt must carry the lesson\n" + "\n".join(tel.logs))
    assert not any("no_tree_premise" in m for m in tel.logs)
    # The stamp survives the real store's write and read-back.
    records = _stored(project)
    assert records and all(r.get(FIX_LOOP_SCOPE) for r in records)
    assert diagnoses == 2


def test_negative_control_without_the_loop_premise_the_round_does_not_re_enter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fix premise: the lesson is retired `no_tree_premise`, so the
    re-entry now reads that verdict and stays out. (Before the re-entry read
    the screen it went back in with the lesson withheld, which is the live
    instance.)"""
    from pipeline_core.domain import retro_gate
    monkeypatch.setattr(retro_gate, "earned_in_loop", lambda *a: False)
    project = _workspace(tmp_path)
    tel, prompts, diagnoses = _run_unit(project, monkeypatch)
    assert "phase_c.fn.retro_retry" not in tel.events
    assert "phase_c.fn.retro_retry_withheld" in tel.events
    assert not any(_LESSON in p for p in prompts)
    assert diagnoses == 1


def test_a_repeated_error_set_ends_the_unit_for_the_fill(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run `486447-5207101c`: `construct` stopped on the
    same `E0433` twice, the retrospective was asked, and the same fill
    still kept a body the sweep classed `wrong`. The same error set on two
    attempts is the unit's outcome for the fill: the retrospective is
    stored, the loop is not re-entered, and the pre-fill body stays."""
    project = _workspace(tmp_path)
    tel, prompts, diagnoses = _run_unit(project, monkeypatch, bodies=(_BODY,))
    assert any("same error set after attempt 2" in m for m in tel.logs), (
        "\n".join(tel.logs))
    assert diagnoses == 1 and _stored(project)
    assert "phase_c.fn.retro_retry" not in tel.events
    assert len(prompts) == 2
    lib = project / ".skeleton-filled" / _CLUSTER / "src" / "lib.rs"
    assert lib.read_text() == _LIB


def test_a_repeated_error_set_after_a_compiling_attempt_salvages_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The salvage half of the rule: attempt 1 compiles and fails its test,
    attempts 2 and 3 repeat one error set. Before, the exit salvaged the
    compiling `4` (a body the sweep then grades `wrong`); now the pre-fill
    body stays. Control: the same sequence stopped on a stalled floor (no
    repeated set) still salvages the compiling body."""
    project = _workspace(tmp_path)
    lib = project / ".skeleton-filled" / _CLUSTER / "src" / "lib.rs"
    tel, _prompts, _d = _run_unit(project, monkeypatch,
                                  bodies=("4", _BODY, _BODY), max_stalls=3)
    assert any("same error set after attempt 3" in m for m in tel.logs), (
        "\n".join(tel.logs))
    assert lib.read_text() == _LIB

    control = _workspace(tmp_path / "control")
    lib_c = control / ".skeleton-filled" / _CLUSTER / "src" / "lib.rs"
    tel_c, _p, _d = _run_unit(control, monkeypatch,
                              bodies=("4", _BODY, _OTHER), max_stalls=2)
    assert not any("same error set" in m for m in tel_c.logs)
    assert "KEPT" in "\n".join(tel_c.logs) and "4" in lib_c.read_text(), (
        "\n".join(tel_c.logs))
