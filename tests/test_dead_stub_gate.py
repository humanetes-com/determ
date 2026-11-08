"""The end gate's fifth bucket: a stub the finished tree cannot reach — the
free-fn and inherent-method half, read through rustc's dead-code lint (the
trait-impl half, decided by removal, is `tests/test_stub_removal.py`).

Run `292191-d60a5308` (2026-10-02) passed 401 tests and exited 1 on two
private helpers the filled callers route around. rustc's dead-code analysis of
that tree reports both never used in the only cluster that compiles them. The
gate now reads that analysis: a stub every compiling world reports dead is
reported and not counted; one any compiling world reaches, or whose world
gave no reading, stays orphaned.

Invented sites only.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.orchestrator import (
    LockDeps,
    classify_remaining_stubs,
    lock_or_complete,
    remaining_stub_verdict,
    report_stub_verdict,
)
from pipeline_core.application.unreached_stub_reading import (
    UnreachedStubDeps,
    read_unreached_stub_proofs,
)
from pipeline_core.domain.dead_code import (
    WorldDeadCode,
    dead_code_spans,
)
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.stub_inventory import StubInventory, StubSite
from pipeline_core.domain.stub_removal import WorldUnreached, unreached_stub_proofs

_HELPER = FnSite("src/probe.rs", "lookup", "")
_LIVE = FnSite("src/probe.rs", "fetch", "")

# `fn lookup` occupies bytes 40..70 of its file up to the `{`; `fn fetch` 100..130.
_REC_HELPER = FnRecord(file="src/probe.rs", fn_name="lookup", sig_byte_start=40,
                       body_byte_start=70, has_unimplemented_body=True)
_REC_LIVE = FnRecord(file="src/probe.rs", fn_name="fetch", sig_byte_start=100,
                     body_byte_start=130, has_unimplemented_body=True)


def _diag(code: str, *spans: tuple[str, int]) -> str:
    return json.dumps({"reason": "compiler-message", "message": {
        "code": {"code": code},
        "spans": [{"file_name": f, "byte_start": b, "is_primary": i == 0}
                  for i, (f, b) in enumerate(spans)]}})


def test_spans_are_read_from_dead_code_diagnostics_only_and_all_of_them() -> None:
    stream = "\n".join([
        _diag("dead_code", ("src/probe.rs", 47)),
        # one diagnostic about several associated items: impl + each item
        _diag("dead_code", ("src/ptr.rs", 10), ("src/ptr.rs", 30), ("src/ptr.rs", 55)),
        _diag("unused_variables", ("src/probe.rs", 110)),
        json.dumps({"reason": "compiler-artifact"}),
        "warning: not json",
        json.dumps({"reason": "compiler-message", "message": {"code": None, "spans": []}}),
    ])
    assert dead_code_spans(stream) == frozenset({
        ("src/probe.rs", 47), ("src/ptr.rs", 10), ("src/ptr.rs", 30), ("src/ptr.rs", 55)})


def _everywhere(_rec: FnRecord, _cluster: str) -> bool:
    return True


def dead_stub_sites(recs, worlds, compiled_in) -> frozenset[FnSite]:
    """The free-fn / inherent half of the one rule (`unreached_stub_proofs`):
    the lint's dead set, every compiling world agreeing."""
    return frozenset(unreached_stub_proofs(
        recs, (), [WorldUnreached(w.cluster, w) for w in worlds], compiled_in))


def _proofs(*sites: FnSite) -> dict[FnSite, str]:
    return dict.fromkeys(sites, "rustc reports it never used in a")


def test_a_stub_named_by_a_span_in_its_signature_is_dead() -> None:
    world = WorldDeadCode("a", frozenset({("src/probe.rs", 47)}))
    assert dead_stub_sites([_REC_HELPER, _REC_LIVE], [world], _everywhere) == frozenset(
        {_HELPER})


def test_a_span_in_another_file_or_outside_the_signature_names_nothing() -> None:
    world = WorldDeadCode("a", frozenset({("src/other.rs", 47), ("src/probe.rs", 75)}))
    assert dead_stub_sites([_REC_HELPER], [world], _everywhere) == frozenset()


def test_a_stub_one_compiling_world_reaches_is_not_dead() -> None:
    dead_in_a = WorldDeadCode("a", frozenset({("src/probe.rs", 47)}))
    live_in_b = WorldDeadCode("b", frozenset())
    assert dead_stub_sites([_REC_HELPER], [dead_in_a, live_in_b], _everywhere) == frozenset()
    # ...and a world that does not compile it is not asked.
    assert dead_stub_sites(
        [_REC_HELPER], [dead_in_a, live_in_b],
        lambda _r, cluster: cluster == "a") == frozenset({_HELPER})


def test_a_world_with_no_reading_excuses_nothing_it_compiles() -> None:
    broken = WorldDeadCode("a", reason="cargo check rc=101")
    assert dead_stub_sites([_REC_HELPER], [broken], _everywhere) == frozenset()
    # no world compiles it at all: nothing measured it, nothing excuses it
    assert dead_stub_sites([_REC_HELPER], [broken], lambda _r, _c: False) == frozenset()


def test_cfg_divergent_copies_of_one_site_are_dead_only_together() -> None:
    twin = FnRecord(file="src/probe.rs", fn_name="lookup", sig_byte_start=200,
                    body_byte_start=230, has_unimplemented_body=True)
    one = WorldDeadCode("a", frozenset({("src/probe.rs", 47)}))
    both = WorldDeadCode("a", frozenset({("src/probe.rs", 47), ("src/probe.rs", 205)}))
    assert dead_stub_sites([_REC_HELPER, twin], [one], _everywhere) == frozenset()
    assert dead_stub_sites([_REC_HELPER, twin], [both], _everywhere) == frozenset({_HELPER})


# --- the gate ---------------------------------------------------------------

def _inventory(*sites: FnSite) -> StubInventory:
    return StubInventory(tuple(StubSite(s, ()) for s in sites))


def test_a_dead_stub_is_its_own_bucket_and_not_counted() -> None:
    stubs = _inventory(_HELPER).site_preds()
    verdict = classify_remaining_stubs(stubs, [], (), (), _proofs(_HELPER))
    assert verdict.dead_in_tree == (FnLocator("src/probe.rs", "lookup"),)
    assert verdict.orphaned == ()
    log: list[str] = []
    assert report_stub_verdict(verdict, log.append, 2) is True
    assert "NOTHING CAN CALL THEM in any baked cluster" in "\n".join(log)
    assert "src/probe.rs::lookup — rustc reports it never used in a" in "\n".join(log)
    assert "‼" not in "\n".join(log)

    # NEGATIVE CONTROL: the same stub with no dead-code reading is orphaned
    # and fails the run, as it did on run 292191-d60a5308.
    control = classify_remaining_stubs(stubs, [], (), ())
    assert control.orphaned == (FnLocator("src/probe.rs", "lookup"),)
    assert report_stub_verdict(control, [].append, 2) is False


def test_a_live_stub_beside_a_dead_one_still_fails_the_run() -> None:
    verdict = classify_remaining_stubs(
        _inventory(_HELPER, _LIVE).site_preds(), [], (), (), _proofs(_HELPER))
    assert verdict.dead_in_tree == (FnLocator("src/probe.rs", "lookup"),)
    assert verdict.orphaned == (FnLocator("src/probe.rs", "fetch"),)
    assert report_stub_verdict(verdict, [].append, 2) is False


def test_a_dead_stub_the_original_executed_is_excused_and_said_loudly() -> None:
    record = FnCoverageRecord(measured=True, candidates=3, executed=(_HELPER,))
    verdict = classify_remaining_stubs(
        _inventory(_HELPER).site_preds(), [], (), [record], _proofs(_HELPER))
    assert verdict.orphaned == ()
    assert verdict.dead_executed_upstream == (
        "src/probe.rs::lookup — rustc reports it never used in a",)
    log: list[str] = []
    assert report_stub_verdict(verdict, log.append, 1) is True
    text = "\n".join(log)
    assert ("UNREACHED PRIVATE BODY, EXECUTED BY THE ORIGINAL: 1 fn(s)"
            in text)
    assert "    src/probe.rs::lookup" in text
    assert "NOTE: 1 fn(s)" not in text, "said once, on its own line"


def _verdict_deps(inventory: StubInventory, dead: frozenset[FnSite],
                  calls: list[int]) -> dict[str, object]:
    def _dead(_p: Path, _clusters: object) -> dict[FnSite, str]:
        calls.append(1)
        return _proofs(*dead)
    return {
        "scan_unimplemented_functions": lambda *a, **k: inventory,
        "load_baked_clusters": lambda _p: [],
        "reference_hollow": lambda _p: frozenset(),
        "augmentation_fn_records": lambda _p: (),
        "unreached_stub_proofs": _dead,
    }


def test_the_dead_code_reading_is_taken_only_when_an_orphan_remains() -> None:
    calls: list[int] = []
    verdict, _n = remaining_stub_verdict(
        Path("/p"), **_verdict_deps(StubInventory(), frozenset(), calls))  # type: ignore[arg-type]
    assert verdict.orphaned == () and calls == []
    verdict, _n = remaining_stub_verdict(
        Path("/p"), **_verdict_deps(_inventory(_HELPER), frozenset({_HELPER}), calls))  # type: ignore[arg-type]
    assert verdict.dead_in_tree and calls == [1]


def test_the_single_module_completion_exits_zero_on_dead_stubs_only() -> None:
    def _exit(code: int) -> None:
        raise SystemExit(code)

    def _complete(dead: frozenset[FnSite]) -> int:
        deps = LockDeps(
            commit_and_mark=lambda *a, **k: None, set_current=lambda *a: None,
            lock_module=lambda *a: None, git_commit=lambda *a: True,
            log=lambda _m: None, sys_exit=_exit,
            scan_unimplemented_functions=lambda *a, **k: _inventory(_HELPER),
            unreached_stub_proofs=lambda _p, _c: _proofs(*dead))
        try:
            verdict = lock_or_complete(
                Path("/p"), "m", Path("/p"), Path("/p/.gen"), is_multi_module=False,
                locked_modules=set(), src_hash=None, commit_message="cm", detail="d",
                set_current_label="s", locked_commit_msg="l", success_log="SUCCESS",
                deps=deps)
        except SystemExit as exc:
            return int(exc.code or 0)
        assert verdict is not None and verdict.blocking
        return -1                                  # blocked: back to the module loop

    assert _complete(frozenset({_HELPER})) == 0
    assert _complete(frozenset()) == -1           # control: no reading


# --- the use case: which worlds are asked -----------------------------------

_LIB = "mod probe;\n#[cfg(alt)]\nmod gated;\n"


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "g"\nversion = "0.1.0"\n')
    (tmp_path / "src" / "lib.rs").write_text(_LIB)
    (tmp_path / "src" / "probe.rs").write_text("x" * 300)
    (tmp_path / "src" / "gated.rs").write_text("y" * 300)
    return tmp_path


def _clusters() -> list[SimpleNamespace]:
    return [SimpleNamespace(name="base", cfg_true=[], cfg_false=["alt"]),
            SimpleNamespace(name="alt", cfg_true=["alt"], cfg_false=[])]


def _mods() -> list[object]:
    from pipeline_core.domain.models import ModRecord
    return [ModRecord.from_dict({"name": "probe", "file": "probe.rs",
                                 "declared_in": "lib.rs", "cfg_predicates": []}),
            ModRecord.from_dict({"name": "gated", "file": "gated.rs",
                                 "declared_in": "lib.rs", "cfg_predicates": ["alt"]})]


def _deps(fns: list[FnRecord], measure, log, mods=None) -> UnreachedStubDeps:
    """The use case with no trait-impl stub: the compiler view and the
    removal build must never be asked."""
    def _no_view(_p: Path, _c: object):
        raise AssertionError("no trait impl holds only stubs: no MIR is read")

    def _no_removal(*_a: object):
        raise AssertionError("no trait impl holds only stubs: nothing is deleted")

    return UnreachedStubDeps(
        enumerate_fns=lambda _p: fns,
        enumerate_mods=lambda _p: _mods() if mods is None else mods,  # type: ignore[arg-type,return-value]
        measure_dead=measure, read_compiler_view=_no_view,
        measure_removal=_no_removal, log=log)


def test_only_the_worlds_that_compile_a_stub_are_measured_and_must_agree(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    gated = FnRecord(file="src/gated.rs", fn_name="lookup", sig_byte_start=40,
                     body_byte_start=70, has_unimplemented_body=True)
    asked: list[str] = []

    def _measure(_p: Path, cluster: SimpleNamespace) -> WorldDeadCode:
        asked.append(cluster.name)
        return WorldDeadCode(cluster.name, frozenset({("src/gated.rs", 44)}))

    deps = _deps([gated], _measure, lambda _m: None)
    dead = frozenset(read_unreached_stub_proofs(root, _clusters(), deps))
    assert dead == frozenset({FnSite("src/gated.rs", "lookup", "")})
    assert asked == ["alt"]            # `base` does not compile `mod gated`


def test_a_shared_stub_needs_every_world_and_a_failed_check_is_logged(
        tmp_path: Path) -> None:
    root = _tree(tmp_path)
    log: list[str] = []

    def _measure(_p: Path, cluster: SimpleNamespace) -> WorldDeadCode:
        if cluster.name == "alt":
            return WorldDeadCode("alt", reason="cargo check rc=101: error")
        return WorldDeadCode("base", frozenset({("src/probe.rs", 47)}))

    deps = _deps([_REC_HELPER], _measure, log.append)
    assert read_unreached_stub_proofs(root, _clusters(), deps) == {}
    assert any("no dead-code reading" in line and "alt" in line for line in log)


def test_no_stub_or_no_cluster_takes_no_reading(tmp_path: Path) -> None:
    def _never(_p: Path, _c: object) -> WorldDeadCode:
        raise AssertionError("no world should be measured")

    filled = FnRecord(file="src/probe.rs", fn_name="fetch", body_text=" 1 ",
                      sig_byte_start=1, body_byte_start=9)
    deps = _deps([filled], _never, lambda _m: None, mods=[])
    assert read_unreached_stub_proofs(_tree(tmp_path), _clusters(), deps) == {}
    assert read_unreached_stub_proofs(tmp_path, [], deps) == {}
