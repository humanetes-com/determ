"""R10.3/R10.4 — the OFFLINE REPLAY path proven on the REAL fill engine.

The seam units (test_cassette / test_cargo_cache / test_replay_harness) cover the
plumbing in isolation. This drives the actual `_task2_fill_fns_one_by_one` engine
(real splice, real per-fn ask ladder, real cargo seams) through a record→replay
round-trip: a recorded run's model cassette (`{prompt_sha→response}`) + cargo memo
(`(tree_hash,cmd)→verdict`, BOTH `_run_cargo` and `_run_cargo_check_diag`) reproduce
the run byte-identically with NO live model and NO real cargo — and a prompt-changing
edit surfaces as a precise cassette MISS (the blast-radius signal R3 refactors lean on).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pipeline
from pipeline_core.domain.enums import FillUnit
from tests.cargo_cache import CargoCache
from tests.cassette import _sha, install_cassette
from tests.harness import install_fake_extractor

STUB = "pub fn answer() -> u32 {\n    unimplemented!()\n}\n"
OK_RUN = (0, "test result: ok. 1 passed; 0 failed")


def _ws(root: Path, body: str = STUB) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "lib.rs").write_text(body)
    return root


def _force_fn_unit(monkeypatch) -> None:
    # Per-fn unit + extractor-absent fallbacks (name-only stub scan): identical
    # setup to test_e2e_fill_loop, so this exercises the same real engine path.
    monkeypatch.setattr(pipeline, "SETTINGS",
                        dataclasses.replace(pipeline.SETTINGS, phase_c_fill_unit=FillUnit.FN))
    monkeypatch.setattr(pipeline, "_scan_unimplemented_records_for_cluster", lambda *a, **k: None)
    install_fake_extractor(monkeypatch, fns=None, types=[])


def _run(ws: Path) -> dict:
    return pipeline._task2_fill_fns_one_by_one(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[], module_name="m",
        cluster_name="t", cluster=None, check_cmd="cargo check", cluster_env={},
        max_passes=1, fr_path=None, skip_end_pass_test=True)


def test_replay_reproduces_real_fill_engine(tmp_path, monkeypatch) -> None:
    _force_fn_unit(monkeypatch)

    # --- RECORD: real engine, scripted model+cargo captured into a cassette + memo. ---
    asks: dict[str, str] = {}

    def rec_ask(prompt, system=None, **kw):
        asks[_sha(prompt)] = "<BODY>{ 42 }</BODY>"
        return asks[_sha(prompt)]

    monkeypatch.setattr(pipeline, "_ask_raw", rec_ask)
    rec = CargoCache(record=True)
    monkeypatch.setattr(pipeline, "_run_cargo", rec.wrap(lambda *a, **k: OK_RUN))
    monkeypatch.setattr(pipeline, "_run_cargo_check_diag", rec.wrap_check(lambda *a, **k: (0, "", [])))

    res1 = _run(_ws(tmp_path / "rec"))
    fill1 = (tmp_path / "rec" / "src" / "lib.rs").read_text()
    assert res1.fns_filled == 1 and "42" in fill1
    assert len(asks) == 1 and len(rec.cache) == 1            # one prompt, one cargo verdict recorded

    # --- REPLAY: fresh tree, cassette + replay-mode memo; real runners must NOT fire. ---
    misses: list[str] = []
    install_cassette(monkeypatch, dict(asks), strict=True, misses=misses)
    rep = CargoCache(dict(rec.cache), record=False, check_cache=dict(rec.check_cache))

    def boom(*a, **k):
        raise AssertionError("real cargo ran during replay")

    monkeypatch.setattr(pipeline, "_run_cargo", rep.wrap(boom))
    monkeypatch.setattr(pipeline, "_run_cargo_check_diag", rep.wrap_check(boom))

    res2 = _run(_ws(tmp_path / "rep"))
    fill2 = (tmp_path / "rep" / "src" / "lib.rs").read_text()
    assert res2 == res1                                      # identical engine outcome, offline
    assert fill2 == fill1                                    # byte-identical reconstructed source
    assert misses == []                                     # nothing drifted
    assert rep.hits >= 1                                     # cargo served from the memo


def test_prompt_change_is_a_precise_cassette_miss(tmp_path, monkeypatch) -> None:
    _force_fn_unit(monkeypatch)

    asks: dict[str, str] = {}

    def rec_ask(prompt, system=None, **kw):
        asks[_sha(prompt)] = "<BODY>{ 42 }</BODY>"
        return asks[_sha(prompt)]

    monkeypatch.setattr(pipeline, "_ask_raw", rec_ask)
    monkeypatch.setattr(pipeline, "_run_cargo", lambda *a, **k: OK_RUN)
    monkeypatch.setattr(pipeline, "_run_cargo_check_diag", lambda *a, **k: (0, "", []))
    _run(_ws(tmp_path / "rec"))                              # record the `answer` prompt

    # Replay against a CHANGED stub: a different fn → a different per-fn prompt → its
    # sha is not in the cassette → a recorded miss (the precise blast radius). The
    # engine swallows the worker-thread CassetteMiss and simply makes no progress.
    misses: list[str] = []
    install_cassette(monkeypatch, dict(asks), strict=True, misses=misses)
    res = _run(_ws(tmp_path / "changed",
                   "pub fn changed() -> u32 {\n    unimplemented!()\n}\n"))
    assert misses and res.fns_filled == 0
