"""Pins for the session-65 convergence fixes:

1. retro-key canonicalisation — one impl rendered two ways (extractor fn
   records vs impl-block manifest) must share one retrospective key.
2. exhaustion reset at phase boundaries — flags cleared, lessons kept.
3. default-cluster gen_tests gating — stable's siblings must be cfg-excluded
   under every other cluster's flags (nightly 0% root cause).
4. gen_tests sibling name parsing (regex-free) + is_test_file classification.
5. behavioral-failure attribution — failing tests map to the src fns they
   exercise so the fix loop repairs bodies instead of whole-file rewrites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402
from pipeline_core.adapters.retros import (  # noqa: E402
    is_fn_fully_exhausted,
    load_retrospectives,
    retro_key,
    save_retrospectives,
)
from pipeline_core.application.reconstruct.cluster_membership import (  # noqa: E402
    cluster_cfg_attr,
)
from pipeline_core.domain.models import BuildCluster  # noqa: E402
from pipeline_core.domain.test_results import (  # noqa: E402
    failing_test_names,
    identifiers_in,
)
from tests.harness import install_fake_extractor  # noqa: E402

# ── 1. retro-key canonicalisation ───────────────────────────────────────────

_PRETTY = 'impl<T, E> Context<T, E> for Result<T, E>\nwhere\n    E: ext::StdError + Send + Sync + \'static,'
_TOKENED = "impl Context < T , E > for Result < T , E >"


def test_canon_container_unifies_renderings() -> None:
    assert pipeline._canon_container(_PRETTY) == pipeline._canon_container(_TOKENED)
    assert retro_key("src/context.rs", "context", _PRETTY) == \
        retro_key("src/context.rs", "context", _TOKENED)
    # Distinct impls stay distinct; sentinel and empty unchanged.
    assert pipeline._canon_container("impl Bool for &bool") != \
        pipeline._canon_container(_TOKENED)
    assert pipeline._canon_container("*impl*") == "*impl*"
    assert pipeline._canon_container("") == ""


def test_canon_container_is_total_over_lifetime_elision() -> None:
    """`Chain<'_>` elides to an EMPTY generic list, and the model's quoted
    `block` attribute sometimes arrives cut at the lifetime. All three shapes
    name one impl, so all three must canonicalise alike — the one-character
    gap between `DefaultforChain<>` and `DefaultforChain<` dropped a correct,
    name-unique `default` body and escalated the unit to a budget-off retry."""
    full = pipeline._canon_container("impl Default for Chain<'_>")
    assert full == "DefaultforChain"
    assert pipeline._canon_container("impl Default for Chain<'_") == full
    assert pipeline._canon_container("impl Default for Chain") == full
    # A non-empty generic list is still identity-bearing and must NOT collapse.
    assert pipeline._canon_container("impl Default for Chain<T>") != full


def test_load_retrospectives_rekeys_old_records(tmp_path: Path) -> None:
    d = tmp_path / ".phase_c_logs"
    d.mkdir()
    (d / "retrospectives.json").write_text(json.dumps({
        f"src/context.rs::context::{_PRETTY}": {"fully_exhausted": True, "guidance": "g1"},
        f"src/context.rs::context::{_TOKENED}": {"fully_exhausted": False, "guidance": "g2"},
    }), encoding="utf-8")
    retros = load_retrospectives(tmp_path)
    # Both old spellings collapse onto the canonical key.
    assert len(retros) == 1
    key = retro_key("src/context.rs", "context", _TOKENED)
    assert key in retros


# ── 2. exhaustion reset ─────────────────────────────────────────────────────

def test_reset_exhaustion_flags_keeps_lessons(tmp_path: Path) -> None:
    key = retro_key("src/error.rs", "new", "impl Error")
    save_retrospectives(tmp_path, {
        key: {"fully_exhausted": True, "category": "GENUINELY_HARD",
              "guidance": "try a vtable"},
    })
    cleared = pipeline._reset_exhaustion_flags(tmp_path, "test boundary")
    # Returns the RELEASED KEYS now, not a count — the caller must open its
    # own in-process exhaustion gates for exactly these units.
    assert cleared == [key]
    retros = load_retrospectives(tmp_path)
    rec = retros[key]
    assert rec["fully_exhausted"] is False
    assert rec["guidance"] == "try a vtable"          # lesson survives
    assert not is_fn_fully_exhausted(retros, "src/error.rs", "new", "impl Error")
    # Idempotent: nothing left to clear.
    assert pipeline._reset_exhaustion_flags(tmp_path, "again") == []


def test_reset_exhaustion_flags_caps_repeat_offenders(
        tmp_path: Path, monkeypatch) -> None:
    # A fn that keeps re-exhausting immediately after each reset (no real
    # improvement) must stop being re-admitted once it hits the per-fn cap —
    # otherwise it pays a full attempt on every future boundary forever,
    # for zero payoff each time (live-observed on anyhow: a 16-fn re-admit
    # batch came back improved=False right after a 43-fn batch that DID
    # pay off — the unconditional version can't tell those apart).
    monkeypatch.setenv("RETRO_RESET_MAX_ATTEMPTS", "2")
    key = retro_key("src/nightly.rs", "provide", "impl Error")
    save_retrospectives(tmp_path, {
        key: {"fully_exhausted": True, "category": "GENUINELY_HARD",
              "guidance": "stuck"},
    })
    # Attempt 1: cleared, reset_attempts becomes 1.
    assert pipeline._reset_exhaustion_flags(tmp_path, "boundary 1") == [key]
    retros = load_retrospectives(tmp_path)
    assert retros[key]["reset_attempts"] == 1
    # Simulate the fn immediately re-exhausting with no improvement.
    retros[key]["fully_exhausted"] = True
    save_retrospectives(tmp_path, retros)
    # Attempt 2: still under the cap (1 < 2) — cleared again.
    assert pipeline._reset_exhaustion_flags(tmp_path, "boundary 2") == [key]
    retros = load_retrospectives(tmp_path)
    assert retros[key]["reset_attempts"] == 2
    retros[key]["fully_exhausted"] = True
    save_retrospectives(tmp_path, retros)
    # Attempt 3: now AT the cap (2 >= 2) — refused, stays exhausted.
    assert pipeline._reset_exhaustion_flags(tmp_path, "boundary 3") == []
    retros = load_retrospectives(tmp_path)
    assert retros[key]["fully_exhausted"] is True
    assert is_fn_fully_exhausted(
        retros, "src/nightly.rs", "provide", "impl Error")


# ── 3. default-cluster cfg gate ─────────────────────────────────────────────

def _cluster(name: str, active: list, default: bool) -> BuildCluster:
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=active,
                        prompt_nightly_rule="", is_default=default)


def test_default_cluster_gen_tests_gated_against_other_clusters(monkeypatch) -> None:
    stable = _cluster("stable", [], True)
    nightly = _cluster("nightly", ["error_generic_member_access"], False)
    no_core = _cluster("anyhow_no_core_error", ["anyhow_no_core_error"], False)
    monkeypatch.setattr(pipeline._SCOPE, "all", [stable, nightly, no_core])

    gate = cluster_cfg_attr(stable, pipeline._SCOPE.all)
    assert gate.startswith("not(any(")
    assert "error_generic_member_access" in gate
    assert 'feature = "anyhow_no_core_error"' in gate
    # Non-default clusters keep their positive gate.
    assert cluster_cfg_attr(nightly, pipeline._SCOPE.all) == \
        'any(error_generic_member_access, feature = "error_generic_member_access")'
    # Single-cluster project: default stays ungated.
    monkeypatch.setattr(pipeline._SCOPE, "all", [stable])
    assert cluster_cfg_attr(stable, pipeline._SCOPE.all) == ""
    assert cluster_cfg_attr(None, pipeline._SCOPE.all) == ""


# ── 4. sibling name parsing (regex-free) ────────────────────────────────────

def test_gen_tests_sibling_parsing_and_test_file_classification(tmp_path: Path) -> None:
    assert pipeline._match_gen_tests_sibling("context.gen_tests_1.rs") == ("context", 1)
    assert pipeline._match_gen_tests_sibling("a.gen_tests_1.gen_tests_2.rs") == \
        ("a.gen_tests_1", 2)
    for bad in ("context.rs", "gen_tests_1.rs", "x.gen_tests_.rs",
                "x.gen_tests_1.txt", "x.gen_tests_a.rs"):
        assert pipeline._match_gen_tests_sibling(bad) is None, bad
    assert pipeline._is_gen_tests_file("error.gen_tests_12.rs")
    assert not pipeline._is_gen_tests_file("error.rs")
    # is_test_file still classifies siblings as test files (write-protection
    # sets rely on it); task0c's harness loop uses its own protected-only filter.
    sib = tmp_path / "src" / "error.gen_tests_1.rs"
    sib.parent.mkdir(parents=True)
    sib.write_text("", encoding="utf-8")
    assert pipeline.is_test_file(sib, tmp_path)


# ── 5. behavioral-failure attribution ───────────────────────────────────────

_CARGO_OUT = (
    "running 2 tests\n"
    "test test_add ... FAILED\n"
    "test other::test_ok ... ok\n"
    "test result: FAILED. 1 passed; 1 failed; 0 ignored\n"
)


def test_failing_test_names_and_identifiers() -> None:
    assert failing_test_names(_CARGO_OUT) == ["test_add"]
    assert failing_test_names(_CARGO_OUT + _CARGO_OUT) == ["test_add"]  # dedup
    ids = identifiers_in("assert_eq!(add(1, two), Error::msg)")
    assert {"assert_eq", "add", "two", "Error", "msg"} <= ids
    assert "1" not in ids


def test_behavior_frontier_attributes_failing_test_to_fn(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "lib.rs").write_text(
        "pub fn add(a: u32, b: u32) -> u32 { 0 }\n"
        "pub fn sub(a: u32, b: u32) -> u32 { 0 }\n", encoding="utf-8")
    (ws / "tests" / "test_math.rs").write_text(
        "#[test]\nfn test_add() { assert_eq!(add(1, 2), 3); }\n", encoding="utf-8")
    install_fake_extractor(monkeypatch, fns=[
        {"file": "src/lib.rs", "fn_name": "add", "container": ""},
        {"file": "src/lib.rs", "fn_name": "sub", "container": ""},
    ])
    frontier, test_srcs = pipeline._APP.behavior_frontier(
        _CARGO_OUT, ws, {"tests/test_math.rs"})
    assert frontier == {("src/lib.rs", "add"): {"test_add"}}
    trel, tsrc = test_srcs["test_add"]
    assert trel == "tests/test_math.rs"
    assert "assert_eq!(add(1, 2), 3)" in tsrc


def test_behavior_frontier_disambiguates_by_container(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "tests" / "t.rs").write_text(
        "#[test]\nfn test_new() { let e = Error::new(1); assert!(new(e)); }\n",
        encoding="utf-8")
    install_fake_extractor(monkeypatch, fns=[
        {"file": "src/error.rs", "fn_name": "new", "container": "impl Error"},
        {"file": "src/other.rs", "fn_name": "new", "container": "impl Widget"},
    ])
    out = "test test_new ... FAILED\n"
    frontier, _ = pipeline._APP.behavior_frontier(out, ws, {"tests/t.rs"})
    # Ambiguous fn name: only the container mentioned in the test is kept.
    assert ("src/error.rs", "new") in frontier
    assert ("src/other.rs", "new") not in frontier
