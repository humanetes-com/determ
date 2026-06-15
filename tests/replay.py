"""Full-run offline replay harness — CODE_ARCHITECTURE.md R10.3 (the wiring that
ties the cassette + cargo memo into one replayable run).

A recorded run leaves two artefact sets behind:
  * the model seam  — `logs/events.jsonl` + the content-addressed `logs/prompts/`
    store → a `{prompt_sha → response}` cassette (R10.3, :mod:`tests.cassette`);
  * the cargo seam  — `(tree_hash, cmd) → (rc, out)` verdicts captured in RECORD
    mode and persisted with `save_cargo_cache` (R10.4, :mod:`tests.cargo_cache`).

Replay installs BOTH seams at once: identical prompts hash to the same canned
responses → identical source trees → cargo verdicts served from the memo. The run
is then deterministic, instant, and needs neither a live model nor a real cargo.
That is the validation path R3 (engine decomposition) was blocked on: refactors of
the fill engine can be exercised offline and any divergence surfaces as a precise
**cassette miss** at the prompt that changed.

Usage::

    cassette, cargo = build_replay(events, prompts, product="microcrate",
                                   cargo_cache_path=cargo_json)
    misses = install_replay(monkeypatch, cassette, cargo)   # both seams patched
    ... drive the pipeline / a use-case ...
    assert not misses                                       # nothing drifted
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests.cargo_cache import CargoCache, install_cargo_cache, load_cargo_cache
from tests.cassette import build_cassette, install_cassette


@dataclass(frozen=True)
class Replay:
    """The artefacts a recorded run replays from: the model cassette and (optionally)
    the cargo memo. `misses` accumulates the prompt shas with no recorded response
    once installed — empty means the code under test reproduced the recording."""
    cassette: dict[str, str]
    cargo: CargoCache | None = None
    misses: list[str] = field(default_factory=list)


def build_replay(
    events_path: str | Path,
    prompts_dir: str | Path,
    *,
    product: str | None = None,
    pipeline_sha: str | None = None,
    cargo_cache_path: str | Path | None = None,
) -> Replay:
    """Assemble a :class:`Replay` from a recorded run. The cassette is always built
    from `events.jsonl` + `prompts/`; the cargo memo is loaded (replay mode) only
    when `cargo_cache_path` is given (else cargo runs live / is patched separately)."""
    cassette = build_cassette(
        events_path, prompts_dir, product=product, pipeline_sha=pipeline_sha)
    cargo = (CargoCache(load_cargo_cache(cargo_cache_path), record=False)
             if cargo_cache_path is not None else None)
    return Replay(cassette=cassette, cargo=cargo)


def install_replay(monkeypatch, replay: Replay, *, strict: bool = True) -> list[str]:
    """Patch both seams (`pipeline._ask_raw`, and `pipeline._run_cargo` when a cargo
    memo is present). Returns the live `misses` list — assert it stays empty to prove
    the run reproduced the recording. `strict` makes an unrecorded prompt raise
    `CassetteMiss` immediately (the precise blast-radius signal)."""
    install_cassette(monkeypatch, replay.cassette, strict=strict, misses=replay.misses)
    if replay.cargo is not None:
        install_cargo_cache(monkeypatch, replay.cargo)
    return replay.misses
