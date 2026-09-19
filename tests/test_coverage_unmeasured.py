"""A crashed binary is a destroyed coverage measurement, and every consumer
of the reading is told.

Run `1577437-c93d5c26` (2026-09-06, cluster `stable`, iter 4): three test
binaries died under the coverage probe (SIGSEGV, SIGABRT, SIGABRT). Pass A
merged the profiles that existed, so every body only those binaries execute
counted 0 and `slice_from_counts` classed it dead — `phase_c.dispatch.
service_order` listed `src/error.rs::reallocate_into_boxed_dyn_error_
without_backtrace` and `::object_reallocate_boxed` under `dead`, the ranking
sank them below the frontier, and the batch was spent elsewhere. Pass C
accepted any profraw on disk as a measurement, but the runtime CREATES the
file at process start and writes counters at exit (measured: present 5 ms
in, 0 bytes at the SIGSEGV), so six red tests came back with EMPTY owner
sets and the log said `owner set for 13/13`. The retrospectives store
already recorded the SIGSEGV test entering both units when it last ran to a
verdict.

Fixture: `tests/fixtures/coverage/1577437_stable_unmeasured.json` — the
shipped reading (negative control), the per-process record, the reading
after the completion rule, and the store's stamps, all computed on that
tree on 2026-09-07.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.adapters.coverage_owner import (
    _completed,
    measure_coverage_slice,
)
from pipeline_core.domain.coverage_slice import (
    CoverageSlice,
    bind,
    matching_test_key,
    slice_from_counts,
    withhold_unmeasured,
)
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from pipeline_core.domain.recorded_entry import (
    recorded_entry_sets,
    stamped_entry_sets,
)

_FIX = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "coverage"
     / "1577437_stable_unmeasured.json").read_text(encoding="utf-8"))
_CULPRIT = FnLocator("src/error.rs",
                     "reallocate_into_boxed_dyn_error_without_backtrace")
_SIBLING = FnLocator("src/error.rs", "object_reallocate_boxed")
_SEGV_TEST = ("test_reallocate_into_boxed_dyn_error_without_backtrace"
              "_preserves_std_error_trait")


def _loc(text: str) -> FnLocator:
    file, fn = text.rsplit("::", 1)
    return FnLocator(file, fn)


# ---- the recorded reading, as the loop read it (negative control data) ----

def test_the_shipped_reading_convicted_the_crashed_binaries_bodies() -> None:
    """The live event: both units dead, six red tests with an empty owner set
    and none absent, while every one of those six was a process that did
    not complete (`process.pass_c` rc < 0)."""
    ctl = _FIX["control"]
    assert ctl["units_in_dead"] == {
        f"{_CULPRIT.file}::{_CULPRIT.fn_name}": True,
        f"{_SIBLING.file}::{_SIBLING.fn_name}": True}
    empties = sorted(t for t, ks in ctl["owners"].items() if not ks)
    died = sorted(t for t, r in _FIX["process"]["pass_c"].items() if r["rc"] < 0)
    # The live event carried six empties; this reading of the same tree
    # carried seven (the use-after-free is layout-dependent and one more
    # process died) — every empty entry is exactly a process that died.
    assert empties == died and len(empties) >= 6 and ctl["absent"] == []
    assert any("owner set for 13/13" in ln for ln in ctl["log"])
    for t in empties:
        run = _FIX["process"]["pass_c"][t]
        assert run["rc"] < 0 and run["raw_exists"] and run["entered"] == 0, (t, run)


# ---- domain: unmeasured is not dead ----

def _control_slice() -> CoverageSlice:
    """The shipped reading re-expressed as a slice: the event's dead list,
    the six empties as UNMEASURED tests (what the adapter now reports)."""
    ctl = _FIX["control"]
    unmeasured = frozenset(t for t, ks in ctl["owners"].items() if not ks)
    return CoverageSlice(
        dead=frozenset(_loc(u) for u in ctl["dead"]),
        dead_regions={_CULPRIT: "Lines 1–2 of the body you are editing are never reached."},
        owners={t: frozenset(FnSite(_loc(u).file, _loc(u).fn_name, "")
                             for u in ks)
                for t, ks in ctl["owners"].items() if ks},
        failing=frozenset(_FIX["failing"]),
        unmeasured_binaries=frozenset(_FIX["after"]["unmeasured_binaries"]),
        unmeasured_tests=unmeasured)


def _recorded() -> dict[str, frozenset[FnLocator]]:
    return recorded_entry_sets(_FIX["store_excerpt"], _FIX["failing"])


def test_bodies_of_a_non_completed_binary_are_unmeasured_not_dead() -> None:
    """On the live dead list: the SIGSEGV binary's tests are unmeasured, so
    `dead` and its region sentences are withheld whole — the two reallocate
    units leave `dead`, and so does everything else, because a run that
    stopped short bounds nothing about the bodies after its stop."""
    reading, withheld = withhold_unmeasured(_control_slice())
    assert reading.dead == frozenset() and reading.dead_regions == {}
    assert {_CULPRIT, _SIBLING} <= withheld
    assert withheld == _control_slice().dead | frozenset(
        _control_slice().dead_regions)


def test_negative_control_the_pre_change_shape_keeps_the_units_dead() -> None:
    """The shipped slice carried no `unmeasured_tests` (the crashed tests
    were EMPTY entries in `owners`) and the capture named no truncated run,
    so nothing is withheld and both units stay dead — the live
    `service_order` event."""
    ctl = _FIX["control"]
    old = CoverageSlice(
        dead=frozenset(_loc(u) for u in ctl["dead"]),
        owners={t: frozenset() for t, ks in ctl["owners"].items() if not ks},
        failing=frozenset(_FIX["failing"]))
    reading, withheld = withhold_unmeasured(old)
    assert withheld == frozenset()
    assert _CULPRIT in reading.dead and _SIBLING in reading.dead


def test_a_failing_run_that_stopped_inside_the_crate_withholds_dead() -> None:
    """No process died, but a red test failed at a panic inside the crate
    (`test_outcome.truncated_failing`): its path after the panic is unknown,
    so the reading may not call anything dead, and the test leaves the
    owner map (absent = unmeasured, never a refusal)."""
    base = slice_from_counts([], owners={"m::t": frozenset({FnSite(
        "src/a.rs", "f", "")})}, failing=["m::t"])
    base = CoverageSlice(dead=frozenset({_CULPRIT}), owners=base.owners,
                         failing=base.failing)
    out, withheld = withhold_unmeasured(base, truncated={"t"})
    assert out.dead == frozenset() and withheld == frozenset({_CULPRIT})
    assert out.owners == {} and "t" in out.unmeasured_tests


def test_no_unmeasured_run_leaves_the_reading_untouched() -> None:
    base = slice_from_counts([], owners={}, failing=["t"])
    out, withheld = withhold_unmeasured(base, truncated=())
    assert out == base and withheld == frozenset()


def test_measured_tests_keep_their_owner_sets() -> None:
    """Only the unmeasured tests leave `owners`; a test that ran to its
    end keeps its measured set for every refusal that reads it."""
    base = _control_slice()
    out, _ = withhold_unmeasured(base)
    assert out.owners == {t: ks for t, ks in base.owners.items()
                          if t not in base.unmeasured_tests}
    assert _SEGV_TEST not in out.owners


# ---- domain: the recorded executed set ----

def test_the_recorded_set_stands_in_for_the_segv_test() -> None:
    """On the live store excerpt the SIGSEGV test's recorded set is the
    union of its five stamps (2, 3, 8, 8, 8 units) — eight units, and the
    culprit is one of them."""
    got = _recorded()[_SEGV_TEST]
    assert len(got) == 8, sorted(got)
    assert _CULPRIT in got and _SIBLING in got


def test_a_never_recorded_test_is_absent_not_empty() -> None:
    got = recorded_entry_sets(_FIX["store_excerpt"],
                              [_SEGV_TEST, "never_recorded_test"])
    assert "never_recorded_test" not in got
    assert _SEGV_TEST in got


def test_recorded_sets_reconcile_a_qualified_name_to_its_leaf() -> None:
    """The sweep names a generated test `gen_tests_2::t`; the stamp carries
    the convicting test's bare leaf. The set is returned under the name
    AS ASKED, and an ambiguous leaf (two stamped modules) reads as absent."""
    store = {
        "src/a.rs::f::": {"convictor_entry": {"t": ["src/a.rs::f"]}},
        "src/a.rs::g::": {"convictor_entry": {"t": ["src/a.rs::g"]}},
        "src/b.rs::h::": {"convictor_entry": {"m1::u": ["src/b.rs::h"],
                                              "m2::u": ["src/b.rs::h"]}},
        "__migrations__": ["not", "a", "record"],
        "src/c.rs::bad::": {"convictor_entry": {"v": ["no-separator", 7]}},
    }
    got = recorded_entry_sets(store, ["gen_tests_2::t", "u", "v"])
    assert got == {"gen_tests_2::t": frozenset(
        {FnLocator("src/a.rs", "f"), FnLocator("src/a.rs", "g")})}
    assert stamped_entry_sets(store) == {
        "t": frozenset({FnLocator("src/a.rs", "f"), FnLocator("src/a.rs", "g")}),
        "m1::u": frozenset({FnLocator("src/b.rs", "h")}),
        "m2::u": frozenset({FnLocator("src/b.rs", "h")})}
    assert matching_test_key("u", ["m1::u", "m2::u"]) is None
    assert matching_test_key("x::t", ["t"]) == "t"
    assert matching_test_key("t", ["t", "y::t"]) == "t"


# ---- adapter: completion is the returncode and a non-empty profile ----

def test_a_zero_byte_profile_left_by_a_signal_death_is_not_a_measurement(
        tmp_path: Path) -> None:
    """The mechanism behind the empty owner sets: the file the runtime
    creates at start EXISTS (the shipped completion signal) and is 0 bytes.
    A failing test that exits 101 is complete; a signal death is not."""
    raw = tmp_path / "one.profraw"
    raw.write_bytes(b"")
    assert raw.exists()
    assert not _completed(-11, [raw])
    assert not _completed(0, [raw])
    raw.write_bytes(b"\x81rforpl")
    assert _completed(101, [raw])
    assert not _completed(-6, [raw])
    assert not _completed(0, [])


def _crash_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    src = project / ".skeleton-filled" / "stable" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        "fn only_crashed_binary() {\n    let a = 1;\n}\n"
        "fn nothing_runs() {\n    let z = 0;\n}\n"
        "fn other_binary() {\n    let q = 0;\n}\n", encoding="utf-8")
    return project


def _crash_env(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """Two binaries. `error_gen_3-<hash>` dies with SIGSEGV on the whole
    suite (no profile at all) and lists two tests: `t_basic` completes and
    enters `only_crashed_binary`; `t_crash` dies leaving the 0-byte file.
    `ok_bin-<hash>` completes and enters `other_binary`."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    deps = tmp_path / "bins"
    deps.mkdir(exist_ok=True)
    crash = deps / "error_gen_3-1a679321963fec42"
    ok = deps / "ok_bin-0123456789abcdef"
    monkeypatch.setattr(cov, "_test_binaries", lambda *a, **k: [crash, ok])

    def _fake_run(cmd, cwd, env=None, **k):
        calls.append(list(cmd))
        prog = Path(cmd[0]).name
        if cmd[0] in ("cargo", "llvm-profdata", "llvm-cov"):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        pattern = (env or {}).get("LLVM_PROFILE_FILE", "")
        if "--list" in cmd:
            names = (["t_basic: test", "t_crash: test"]
                     if prog.startswith("error_gen_3") else ["t_ok: test"])
            return subprocess.CompletedProcess(
                cmd, 0, stdout="\n".join(names) + "\n", stderr="")
        if "--exact" in cmd:
            name = cmd[cmd.index("--exact") + 1]
            mine = (name in ("t_basic", "t_crash")
                    if prog.startswith("error_gen_3") else name == "t_ok")
            if not mine:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="running 0 tests\n", stderr="")
            raw = Path(pattern)
            if name == "t_crash":
                raw.write_bytes(b"")          # created at start, never filled
                return subprocess.CompletedProcess(
                    cmd, -11, stdout="running 1 test\n", stderr="")
            raw.write_bytes(b"counts:" + name.encode())
            return subprocess.CompletedProcess(
                cmd, 0, stdout="running 1 test\n", stderr="")
        # whole-suite pass A
        if prog.startswith("error_gen_3"):
            return subprocess.CompletedProcess(cmd, -11, stdout="", stderr="")
        Path(pattern.replace("%p", "1").replace("%m", "1")).write_bytes(
            b"counts:t_ok")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cov, "_run", _fake_run)

    def _fake_export(prof_files, objects, dest, profdata_bin, cov_bin):
        src = str(dest.parent.parent / "tree" / "src" / "lib.rs")
        blob = b"".join(Path(p).read_bytes() for p in prof_files)
        basic = 4 if b"t_basic" in blob else 0
        other = 4 if b"t_ok" in blob else 0
        return {"data": [{"functions": [
            {"filenames": [src], "count": basic, "regions": [[1, 1, 3, 1, basic]]},
            {"filenames": [src], "count": 0, "regions": [[4, 1, 6, 1, 0]]},
            {"filenames": [src], "count": other, "regions": [[7, 1, 9, 1, other]]},
        ]}]}

    monkeypatch.setattr(cov, "_merge_export", _fake_export)
    return calls


_BIN = (Path(__file__).resolve().parents[1] / "tools" / "skeleton_extractor"
        / "target" / "release" / "skeleton-extractor")


def _enumerate(tree: Path) -> list:
    from pipeline_core.adapters.extractor import CliExtractor
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    return CliExtractor(_BIN).enumerate_fns(tree) or []


def test_a_crashed_binary_is_re_run_per_test_and_its_dying_test_is_unmeasured(
        monkeypatch, tmp_path: Path) -> None:
    project = _crash_project(tmp_path)
    calls = _crash_env(monkeypatch, tmp_path)
    notes: list[str] = []
    slice_ = measure_coverage_slice(
        project, "stable", failing_tests=["t_crash", "t_ok"],
        log=notes.append, enumerate_fns=_enumerate)
    assert slice_ is not None
    assert slice_.unmeasured_binaries == frozenset({"error_gen_3"})
    # The dying test is ABSENT from owners and named unmeasured; the
    # completing test of the other binary is measured as before.
    assert "t_crash" not in slice_.owners
    assert "t_crash" in slice_.unmeasured_tests
    assert slice_.owners["t_ok"] == frozenset(
        {FnSite("src/lib.rs", "other_binary", "")})
    # The crashed binary's COMPLETING test was recovered one process at a
    # time, so the body only that binary executes is live, not dead...
    assert FnLocator("src/lib.rs", "only_crashed_binary") not in slice_.dead
    # ...and a body nothing executes is still dead.
    assert FnLocator("src/lib.rs", "nothing_runs") in slice_.dead
    assert any("--list" in c for c in calls if c[0].endswith("error_gen_3-1a679321963fec42"))
    # The recovery run is reused by pass C: `t_crash` is not re-spent.
    exact_crash = [c for c in calls if "--exact" in c and c[c.index("--exact") + 1] == "t_crash"]
    assert len(exact_crash) == 1
    assert any("owner set for 1/2 failing test(s), 1 unmeasured (binary did not complete)" in n
               for n in notes), notes
    assert any("1 binary(ies) did not complete" in n and "error_gen_3 (rc=-11)" in n
               for n in notes), notes


def test_negative_control_the_file_on_disk_rule_reads_the_death_as_empty(
        monkeypatch, tmp_path: Path) -> None:
    """The shipped signal was `raw.exists()`. Under it the 0-byte file of the
    dying test is a measurement that exports zero counts: the test gets an
    EMPTY owner set and the log counts it measured — the live 13/13."""
    project = _crash_project(tmp_path)
    _crash_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cov, "_completed",
                        lambda rc, raws: any(r.exists() for r in raws))
    slice_ = measure_coverage_slice(
        project, "stable", failing_tests=["t_crash"],
        enumerate_fns=_enumerate)
    assert slice_ is not None
    assert slice_.owners.get("t_crash") == frozenset()
    assert "t_crash" not in slice_.unmeasured_tests


# ---- driver: the recorded set reaches the ranking and the event ----

_REDS = ("t135", "t136", "t137")
_ACC = FnLocator("src/a.rs", "fmt")
_ACC2 = FnLocator("src/a.rs", "context")
_CULP = FnLocator("src/b.rs", "culprit")
_ACC_SITE = FnSite("src/a.rs", "fmt", "impl Debug for Error")
_ACC2_SITE = FnSite("src/a.rs", "context", "impl Error")


def _replay(monkeypatch, tmp_path: Path, *, coverage, recorded):
    """The culprit leads the frontier; a dead demotion is then an ORDER
    move (the culprit sinks last in every dispatched batch and
    `service_order` is emitted with it under `dead`)."""
    from tests.walks.harness import load_walk, replay
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(
            walk, monkeypatch, tmp_path, events=events,
            frontier={_CULP: {"t135"}, _ACC: set(_REDS), _ACC2: set(_REDS)},
            fill=_fill, coverage=coverage, recorded=recorded)
    finally:
        bind(None)
    return logs, events, batches


def _unmeasured_coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    """`t135`'s process did not complete; the culprit only its binary runs
    counts 0 in the merge. The other two reds were measured."""
    return CoverageSlice(
        dead=frozenset({_CULP}),
        owners={t: frozenset({_ACC_SITE, _ACC2_SITE}) for t in ("t136", "t137")},
        failing=frozenset(failing_tests),
        unmeasured_binaries=frozenset({"b_gen_1"}),
        unmeasured_tests=frozenset({"t135"}))


def _pre_change_coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    """The shipped shape of the same sweep: the crashed test is an EMPTY
    owner entry and the culprit is dead."""
    return CoverageSlice(
        dead=frozenset({_CULP}),
        owners={"t135": frozenset(),
                **{t: frozenset({_ACC_SITE, _ACC2_SITE}) for t in ("t136", "t137")}},
        failing=frozenset(failing_tests))


def _store(_pd: Path, tests):
    return {t: frozenset({_CULP, _ACC}) for t in tests if t == "t135"}


@pytest.mark.walk
def test_the_recorded_set_stands_in_for_the_unmeasured_red_test(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, batches = _replay(monkeypatch, tmp_path,
                                    coverage=_unmeasured_coverage, recorded=_store)
    maps = [d for e, d in events if e == "phase_c.coverage.owner_map"]
    assert maps, "the owner map must still be emitted"
    first = maps[0]
    assert first["unmeasured"] == ["t135"]
    assert first["unmeasured_binaries"] == ["b_gen_1"]
    assert "t135" not in first["measured"]
    recorded = {u["file"] + "::" + u["fn"] for u in first["recorded_owners"]["t135"]}
    assert recorded == {"src/b.rs::culprit", "src/a.rs::fmt"}
    assert {u["file"] + "::" + u["fn"] for u in first["owners"]["t135"]} == recorded
    assert first["dead_withheld"] == ["src/b.rs::culprit"]
    # Not demoted: no ranking lists the culprit dead and the service-order
    # log never prints a dead demotion (the negative control below shows
    # both on every sweep).
    orders = [d for e, d in events if e == "phase_c.dispatch.service_order"]
    assert all("src/b.rs::culprit" not in d["dead"] for d in orders)
    assert not any("dead body(ies) demoted" in ln for ln in logs)
    assert batches and all(_CULP in b for b in batches)
    assert any("1 failing test(s) did not reach the end of their path" in ln
               and "binaries did not complete: b_gen_1" in ln
               and "1 dead / unreached-line claim(s) withheld" in ln
               for ln in logs), logs


@pytest.mark.walk
def test_negative_control_the_pre_change_shape_demotes_the_culprit(
        monkeypatch, tmp_path: Path) -> None:
    """The live event: the crashed test present with an EMPTY owner set,
    counted measured, and the culprit under `dead` on every ranking."""
    _logs, events, batches = _replay(monkeypatch, tmp_path,
                                     coverage=_pre_change_coverage, recorded=_store)
    first = [d for e, d in events if e == "phase_c.coverage.owner_map"][0]
    assert first["owners"]["t135"] == [] and "t135" in first["measured"]
    assert first["unmeasured"] == [] and first["recorded_owners"] == {}
    orders = [d for e, d in events if e == "phase_c.dispatch.service_order"]
    assert len(orders) == len(batches) >= 3
    assert all(d["dead"] == ["src/b.rs::culprit"] for d in orders)
    assert any("1 dead body(ies) demoted" in ln for ln in _logs)
