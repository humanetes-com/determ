"""A reply whose text the splicer refuses is a failed attempt, not the end of
the unit.

Run `473471-33301154` (2026-09-29, cluster `stable`): the frontier's top
blocker, `object_ref`, was answered with a body rustc's parser rejects (an
ambiguous `+` in a cast type). The splice was refused `parse_broken`, the unit
logged `reverted after 1 attempt(s)`, the model was never shown why, and the
retrospective scored the untouched tree as improved. The unit got one call
per fill. Invented crate and bodies.

Driven through the real per-fn route (`_task2_fill_fns_one_by_one`) with the
model and cargo scripted, as `test_e2e_test_retry.py` does.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pipeline
from pipeline_core.domain.enums import FillUnit
from pipeline_core.domain.reconstruction import SpliceOutcome, SpliceRefusal
from pipeline_core.domain.splice_feedback import (
    REPLY_REFUSALS,
    reply_refusal_feedback,
)
from tests.harness import (
    install_app_seams,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
    install_scripted_model,
)

STUB_LIB = """pub fn answer() -> u32 {
    unimplemented!()
}
"""
# Closes the fn early and declares another at file scope: the splicer refuses
# it on the file's own item list (`item_set_changed`), with no parser needed.
ESCAPES = "41\n}\npub fn smuggled() -> u32 {\n    7"


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(STUB_LIB)
    (ws / "tests").mkdir()
    (ws / "tests" / "t_answer.rs").write_text(
        "#[test]\nfn t_answer() { assert_eq!(answer(), 42); }\n")
    monkeypatch.setattr(
        pipeline, "SETTINGS",
        dataclasses.replace(pipeline.SETTINGS, phase_c_fill_unit=FillUnit.FN))
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])
    install_app_seams(monkeypatch,
                      classify_phase_c_test_failures=lambda *a, **k: {})
    return ws


def _run(ws: Path):  # noqa: ANN202
    return pipeline._task2_fill_fns_one_by_one(
        project_dir=ws, target_src_dir=ws / "src",
        tests_dir_files=[ws / "tests" / "t_answer.rs"], module_name="m",
        cluster_name="t", cluster=None, check_cmd="cargo check",
        cluster_env={}, max_passes=1, fr_path=None, skip_end_pass_test=True)


def _script_model(monkeypatch, bodies: list[str]) -> list[str]:
    """Scripted per-fn model that records every prompt it is sent."""
    install_scripted_model(monkeypatch, per_fn_body="unused")
    prompts: list[str] = []
    queue = iter(bodies)

    def _ask(prompt, fn, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        # Production sends a retry as a TURN of the unit's conversation
        # (`AttemptFeedback`, whose `excerpt` is the measured result). The
        # fake keeps the transcript the way the real transport would, so the
        # route takes that path, and records what each call was told.
        body = next(queue, bodies[-1])
        said = str(getattr(prompt, "excerpt", prompt))
        prompts.append(said)
        conv = k.get("conversation")
        if conv is not None:
            conv.messages.append({"role": "user", "content": said})
            conv.messages.append({"role": "assistant", "content": body})
        return body, None

    monkeypatch.setattr(pipeline, "_APP",
                        dataclasses.replace(pipeline._APP, ask_per_fn_body=_ask))
    return prompts


def test_a_refused_reply_gets_a_second_turn_that_carries_the_reason(
        monkeypatch, tmp_path: Path) -> None:
    """The plan's sentence: after a refusal that judges the reply's text the
    unit is asked again, the retry is told what was refused, and the body
    that follows lands. Pre-fix the unit stopped after the first call with
    the stub still in the file."""
    ws = _setup(monkeypatch, tmp_path)
    prompts = _script_model(monkeypatch, [ESCAPES, "42"])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, "ok"))
    _run(ws)
    text = (ws / "src" / "lib.rs").read_text()
    assert len(prompts) == 2, "the refused reply must be answered with a retry"
    assert "42" in text and "smuggled" not in text and "unimplemented!" not in text
    assert "was refused" in prompts[1]
    assert "closes the function early" in prompts[1]


def test_two_refusals_in_a_row_stall_and_leave_the_stub(monkeypatch, tmp_path: Path) -> None:
    """A model that keeps sending the refused shape is stopped by the fix
    loop's own stall rule, and the tree is left as it was found."""
    ws = _setup(monkeypatch, tmp_path)
    prompts = _script_model(monkeypatch, [ESCAPES])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=(0, "ok"))
    _run(ws)
    assert 2 <= len(prompts) <= 6
    text = (ws / "src" / "lib.rs").read_text()
    code = [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("//")]
    assert "unimplemented!()" in code and not any("smuggled" in ln for ln in code)


# --- the rule ---------------------------------------------------------------

def test_only_refusals_of_the_replys_text_are_feedback() -> None:
    for reason in REPLY_REFUSALS:
        assert reply_refusal_feedback(SpliceOutcome(False, reason, 0), unit="f")  # type: ignore[arg-type]
    for reason in ("target_not_stub", "fn_not_found", "not_in_cluster_cfg",
                   "ambiguous_name", "file_missing", "bad_offsets", "body_is_stub"):
        assert reply_refusal_feedback(
            SpliceOutcome(False, reason, 0), unit="f") is None  # type: ignore[arg-type]
    assert reply_refusal_feedback(SpliceOutcome(True, "ok", 3), unit="f") is None


def test_a_parse_refusal_quotes_the_parser_and_names_the_units_file() -> None:
    out = SpliceRefusal("parse_broken",
                        "error: ambiguous `+` in a type\n --> <stdin>:2:19\n")
    ok, reason, delta = out                       # still the tuple callers unpack
    assert (ok, reason, delta) == (False, "parse_broken", 0)
    text = reply_refusal_feedback(out, unit="probe", file_rel="src/a.rs")
    assert text is not None
    assert text.startswith("error: your body for `probe` was refused")
    assert "ambiguous `+` in a type" in text and "--> src/a.rs:2:19" in text
    assert "<stdin>" not in text
    bare = reply_refusal_feedback(SpliceRefusal("parse_broken"), unit="probe")
    assert bare is not None and "does not parse as Rust" in bare
