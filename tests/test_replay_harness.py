"""The full-run offline replay harness wiring: cargo-memo persistence
(save/load) and `tests.replay` composing the cassette + cargo seams so a recorded
run replays with no live model and no real cargo. Exercised here on a synthetic
recorded run (hermetic; no model, no cargo binary)."""
from __future__ import annotations

import json

import pipeline
from pipeline_core.domain.models import Diagnostic
from tests.cargo_cache import CargoCache, load_cargo_cache, save_cargo_cache, tree_hash
from tests.cassette import CassetteMiss, _sha
from tests.replay import build_replay, install_replay


def _seed_recorded_run(tmp_path, *, product="microcrate", sha="abc123"):
    """Write a minimal events.jsonl + prompts/ store for one request/response pair."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    psha, rsha = _sha("PROMPT_A"), _sha("RESP_A")
    (prompts / f"{rsha}.txt").write_text("RESP_A", encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"product": product, "pipeline_sha": sha, "event": "llm.request",
                    "data": {"prompt": {"sha256": psha}}}) + "\n"
        + json.dumps({"product": product, "pipeline_sha": sha, "event": "llm.response",
                      "data": {"prompt_sha": psha, "response": {"sha256": rsha}}}) + "\n",
        encoding="utf-8")
    return events, prompts


def test_cargo_cache_save_load_round_trips(tmp_path) -> None:
    cache = CargoCache(record=True)
    cache.cache[("treehash1", "cargo test")] = (0, "ok\n")
    cache.cache[("treehash2", "cargo build")] = (101, "error[E0001]\n")
    # the structured check-diag seam round-trips too (Diagnostics serialised).
    cache.check_cache[("treehash3", "cargo check")] = (
        101, "error[E0277]\n", [Diagnostic(code="E0277", file="src/lib.rs", line=2)])
    path = tmp_path / "cargo_cache.json"
    save_cargo_cache(cache, path)
    restored = load_cargo_cache(path)
    assert restored.cache == cache.cache
    assert restored.check_cache == cache.check_cache
    assert restored.record is False
    # The persisted form is plain JSON rows (auditable / diffable).
    assert json.loads(path.read_text())["run"][0][:2] == ["treehash1", "cargo test"]


def test_build_replay_assembles_cassette_and_cargo(tmp_path) -> None:
    events, prompts = _seed_recorded_run(tmp_path)
    cargo_json = tmp_path / "cargo.json"
    save_cargo_cache(CargoCache({("h", "cargo test"): (0, "ok")}), cargo_json)

    replay = build_replay(events, prompts, product="microcrate",
                          cargo_cache_path=cargo_json)
    assert replay.cassette == {_sha("PROMPT_A"): "RESP_A"}
    assert replay.cargo is not None and replay.cargo.record is False  # replay mode
    # product filter isolates the run.
    assert build_replay(events, prompts, product="other").cassette == {}


def test_install_replay_patches_both_seams(tmp_path, monkeypatch) -> None:
    events, prompts = _seed_recorded_run(tmp_path)
    # A real source tree → its tree_hash keys the cargo verdict.
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    cargo_json = tmp_path / "cargo.json"
    save_cargo_cache(CargoCache({(tree_hash(crate), "cargo test"): (0, "ok\n")}), cargo_json)

    replay = build_replay(events, prompts, product="microcrate",
                          cargo_cache_path=cargo_json)
    misses = install_replay(monkeypatch, replay)

    # Model seam: recorded prompt → canned response (no LLM).
    assert pipeline._ask_raw("PROMPT_A") == "RESP_A"
    # Cargo seam: known tree+cmd served from the memo (no cargo binary).
    assert pipeline._run_cargo("cargo test", crate, {}) == (0, "ok\n")
    assert replay.cargo.hits == 1
    assert not misses


def test_install_replay_miss_is_precise(tmp_path, monkeypatch) -> None:
    events, prompts = _seed_recorded_run(tmp_path)
    replay = build_replay(events, prompts, product="microcrate")  # cassette only

    misses = install_replay(monkeypatch, replay, strict=True)
    # An unrecorded prompt is the blast-radius signal: raises + recorded.
    try:
        pipeline._ask_raw("PROMPT_CHANGED")
        raise AssertionError("expected CassetteMiss")
    except CassetteMiss:
        pass
    assert misses == [_sha("PROMPT_CHANGED")]


def test_replay_cargo_miss_raises_in_replay_mode(tmp_path, monkeypatch) -> None:
    events, prompts = _seed_recorded_run(tmp_path)
    cargo_json = tmp_path / "cargo.json"
    save_cargo_cache(CargoCache({("known", "cargo test"): (0, "ok")}), cargo_json)
    replay = build_replay(events, prompts, product="microcrate", cargo_cache_path=cargo_json)
    install_replay(monkeypatch, replay)

    crate = tmp_path / "unknown_crate"
    (crate / "src").mkdir(parents=True)
    (crate / "src" / "lib.rs").write_text("pub fn g() {}\n", encoding="utf-8")
    # A tree with no recorded verdict is a hard miss in replay mode (never a stale hit).
    try:
        pipeline._run_cargo("cargo test", crate, {})
        raise AssertionError("expected cargo cache miss")
    except KeyError:
        pass
