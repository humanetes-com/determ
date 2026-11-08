"""Integration: the end gate reads rustc's own dead-code analysis, and for a
trait-impl method the removal build checked against it, to tell a stub the
finished tree cannot reach from one it can.

Real cargo and rustc, the pipeline's cargo env, the real syn extractor, the
real stub scan and the end gate's classifier. No model.

The class, run `292191-d60a5308` (2026-10-02): every test passed and the run
exited 1 on two private helpers still `unimplemented!()`. The filled callers
routed around both — the only text naming one was a macro no body expands —
so no test could reach them and no fill of them could be judged. rustc reports
exactly that set as never used; the gate could not read it and judged the
stubs by the original's execution record instead.

The fix rests on three behaviours of the toolchain, each pinned by a
`test_premise_*` below:
- `--force-warn dead_code` reports over the crate-level `#![allow(dead_code)]`
  every tree this pipeline writes carries, and over `-A warnings` in RUSTFLAGS;
- a `pub fn` in a private module nothing uses is reported, one a public fn
  calls is not;
- the JSON diagnostic's span starts inside the fn's signature, where the
  extractor's `sig_byte_start..body_byte_start` range finds it.

The crate is invented: a private module holding one stub a public fn calls and
one stub named only by a macro nothing expands, plus a module compiled only
under a cfg, holding one more unused stub, and two cluster worlds.

Gated on `INTEGRATION=1` (plain cargo, no dependencies);
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.dead_code import measure_world_dead_code
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.fn_reachability import measure_world_reachability
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.adapters.stub_removal import measure_world_removal
from pipeline_core.application.orchestrator import (
    classify_remaining_stubs,
    report_stub_verdict,
)
from pipeline_core.application.reconstruct.stub_scan import scan_stub_inventory
from pipeline_core.application.unreached_stub_reading import (
    UnreachedStubDeps,
    read_unreached_stub_proofs,
)
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.models import BuildCluster, FnLocator, FnSite

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + rustc; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_CFG = "gadget_alt"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = { level = "allow" }
"""

_LIB = f"""#![allow(unused, dead_code)]
mod helpers;
#[cfg({_CFG})]
mod extras;

macro_rules! lookup_or {{
    ($x:expr) => {{
        $crate::helpers::lookup($x)
    }};
}}

pub fn size() -> u8 {{
    helpers::fetch()
}}
"""

_HELPERS = """#![allow(unused, dead_code)]
pub fn fetch() -> u8 {
    ::core::unimplemented!()
}
pub fn lookup(x: u8) -> u8 {
    ::core::unimplemented!()
}
"""

_EXTRAS = """#![allow(unused, dead_code)]
pub fn spare() -> u8 {
    ::core::unimplemented!()
}
"""

_FETCH = FnSite("src/helpers.rs", "fetch", "")
_LOOKUP = FnSite("src/helpers.rs", "lookup", "")
_SPARE = FnSite("src/extras.rs", "spare", "")


def _crate(tmp_path: Path) -> Path:
    root = tmp_path / "gadgets"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    (root / "src" / "helpers.rs").write_text(_HELPERS)
    (root / "src" / "extras.rs").write_text(_EXTRAS)
    return root


def _clusters() -> list[BuildCluster]:
    return [
        BuildCluster(name="base", env_extra={}, tarpaulin_exclude_files=[],
                     excluded_cfg=[_CFG], active_cfg=[], prompt_nightly_rule="",
                     is_default=True),
        BuildCluster(name="alt", env_extra={"RUSTFLAGS": f"--cfg {_CFG}"},
                     tarpaulin_exclude_files=[], excluded_cfg=[],
                     active_cfg=[_CFG], prompt_nightly_rule="", is_default=False),
    ]


def _structure() -> CliStructureReader:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    return CliStructureReader(CliExtractor(_EXTRACTOR))


def _deps(log: list[str] | None = None) -> UnreachedStubDeps:
    st = _structure()
    sink = log if log is not None else []
    return UnreachedStubDeps(
        enumerate_fns=st.enumerate_fns, enumerate_mods=st.enumerate_mods,
        measure_dead=functools.partial(
            measure_world_dead_code, cluster_env=cluster_cargo_env, timeout=600),
        read_compiler_view=functools.partial(
            measure_world_reachability, cluster_env=cluster_cargo_env, timeout=600,
            test_profile_mir=True),
        measure_removal=functools.partial(
            measure_world_removal, cluster_env=cluster_cargo_env, timeout=600),
        log=sink.append)


def read_dead_stub_sites(root: Path, clusters, deps) -> dict[FnSite, str]:
    return read_unreached_stub_proofs(root, clusters, deps)


def test_premise_force_warn_reports_over_the_crate_level_allow(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    base = _clusters()[0]
    assert "-A warnings" in cluster_cargo_env(base)["RUSTFLAGS"]
    world = measure_world_dead_code(root, base, cluster_env=cluster_cargo_env,
                                    timeout=600)
    assert world.measured, world.reason
    assert world.spans, (
        "rustc reported no dead_code diagnostic for a crate whose private "
        "module holds an unused pub fn, under `#![allow(dead_code)]` and "
        "`-A warnings`. If `--force-warn` no longer overrides those, the end "
        "gate's dead-stub reading sees nothing and must be re-derived.")


def test_premise_the_span_starts_inside_the_fn_signature(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    world = measure_world_dead_code(root, _clusters()[0],
                                    cluster_env=cluster_cargo_env, timeout=600)
    text = (root / "src" / "helpers.rs").read_text()
    sig_start = text.index("pub fn lookup")
    body_open = text.index("{", sig_start)
    inside = [b for f, b in world.spans
              if f == "src/helpers.rs" and sig_start <= b < body_open]
    assert inside, (
        f"no dead_code span in bytes {sig_start}..{body_open} of "
        f"src/helpers.rs; spans: {sorted(world.spans)}. The join to the "
        "extractor's signature range rests on rustc pointing at the fn name.")


def test_the_unused_stub_is_dead_and_the_called_one_is_not(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    dead = read_dead_stub_sites(root, _clusters(), _deps())
    assert _LOOKUP in dead      # named only by a macro nothing expands
    assert _SPARE in dead       # compiled only under the cfg cluster, unused there
    assert _FETCH not in dead   # `size()` is public and calls it


def test_the_gate_excuses_only_the_stubs_the_tree_cannot_reach(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    clusters = _clusters()
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                for c in clusters]
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert set(verdict.dead_in_tree) == {
        FnLocator("src/helpers.rs", "lookup"), FnLocator("src/extras.rs", "spare")}
    assert verdict.orphaned == (FnLocator("src/helpers.rs", "fetch"),)
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is False

    # NEGATIVE CONTROL: the gate without the reading counts all three.
    control = classify_remaining_stubs(stubs, cfg_sets, (), ())
    assert len(control.orphaned) == 3 and control.dead_in_tree == ()


def test_a_world_that_does_not_build_excuses_nothing(tmp_path: Path) -> None:
    root = _crate(tmp_path)
    # The cfg cluster's own module stops compiling: that world has no reading.
    (root / "src" / "extras.rs").write_text(_EXTRAS + "pub fn broken() -> u8 { }\n")
    log: list[str] = []
    dead = read_dead_stub_sites(root, _clusters(), _deps(log))
    assert _SPARE not in dead
    assert _LOOKUP not in dead   # shared: the broken world compiles it too
    assert any("no dead-code reading" in line for line in log), log


# ---- the oracle's own tests are callers; an exported stub is never dead ----
# Decided 2026-10-17: a crate-private stub no rebuilt caller and no oracle test
# reaches is not counted against green; a public fn, or a stub any oracle test
# or rebuilt caller reaches, still blocks. The plain `cargo check --lib`
# compiles without `cfg(test)`, so a private body only an in-crate test module
# calls read as dead there and was excused.

_KEEPER_LIB = """#![allow(unused, dead_code)]
mod helpers;

pub fn size() -> u8 {
    helpers::fetch()
}

pub fn exported() -> u8 {
    ::core::unimplemented!()
}

#[cfg(test)]
mod checks {
    #[test]
    fn probe_is_one() {
        assert_eq!(crate::helpers::probe(), 1);
    }
}
"""

_KEEPER_HELPERS = """#![allow(unused, dead_code)]
pub fn fetch() -> u8 {
    1
}
pub fn probe() -> u8 {
    ::core::unimplemented!()
}
pub fn routed() -> u8 {
    ::core::unimplemented!()
}
"""


def _keeper(tmp_path: Path) -> Path:
    root = tmp_path / "keeper"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("gadgets", "keeper"))
    (root / "src" / "lib.rs").write_text(_KEEPER_LIB)
    (root / "src" / "helpers.rs").write_text(_KEEPER_HELPERS)
    return root


def _plain_and_test_profile(root: Path) -> tuple[set[str], set[str]]:
    from pipeline_core.adapters.process import run_bounded
    from pipeline_core.domain.dead_code import dead_code_spans
    env = cluster_cargo_env(_clusters()[0])
    env["RUSTFLAGS"] = f"{env.get('RUSTFLAGS', '')} --force-warn dead_code"
    env["CARGO_TARGET_DIR"] = str(root / "target" / "premise")
    text = (root / "src" / "helpers.rs").read_text()
    out = []
    for extra in ([], ["--profile", "test"]):
        run = run_bounded(["cargo", "check", "--lib", *extra,
                           "--message-format=json"],
                          cwd=root, env=env, timeout=600.0)
        assert run.returncode == 0, run.stderr
        names = set()
        for f, b in dead_code_spans(run.stdout):
            if f == "src/helpers.rs":
                names.add(text[b:].split("(", 1)[0])
        out.append(names)
    return out[0], out[1]


def test_premise_the_test_profile_counts_an_in_crate_test_as_a_caller(
        tmp_path: Path) -> None:
    """The negative control is the plain reading the gate took before: it
    reports `probe`, which an oracle test calls."""
    plain, test_profile = _plain_and_test_profile(_keeper(tmp_path))
    assert plain == {"probe", "routed"}, plain
    assert test_profile == {"routed"}, (
        f"`cargo check --lib --profile test` reported {test_profile}: if it "
        "no longer compiles the library under cfg(test), the gate cannot see "
        "an oracle test as a caller and must be re-derived.")


def test_only_the_body_nothing_reaches_is_excused(tmp_path: Path) -> None:
    root = _keeper(tmp_path)
    clusters = _clusters()[:1]
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                for c in clusters]
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    assert set(dead) == {FnSite("src/helpers.rs", "routed", "")}
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert verdict.dead_in_tree == (FnLocator("src/helpers.rs", "routed"),)
    assert set(verdict.orphaned) == {
        FnLocator("src/helpers.rs", "probe"),   # an oracle test reaches it
        FnLocator("src/lib.rs", "exported"),    # public: rustc never calls it dead
    }
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is False

    # The same tree with the reached and the public stub filled: the routed
    # private body alone does not fail the run.
    (root / "src" / "helpers.rs").write_text(
        _KEEPER_HELPERS.replace(
            "pub fn probe() -> u8 {\n    ::core::unimplemented!()",
            "pub fn probe() -> u8 {\n    1"))
    (root / "src" / "lib.rs").write_text(
        _KEEPER_LIB.replace(
            "pub fn exported() -> u8 {\n    ::core::unimplemented!()",
            "pub fn exported() -> u8 {\n    2"))
    stubs = scan_stub_inventory(
        root, enumerate_fns=_structure().enumerate_fns).site_preds()
    dead = read_dead_stub_sites(root, clusters, _deps())
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), dead)
    assert verdict.orphaned == ()
    assert report_stub_verdict(verdict, [].append, len(cfg_sets)) is True


# ---- trait-impl methods: the lint never reports them; removal decides ----
# Run `3323407-984b07f2` (2026-11-08) passed every oracle test and exited 1 on
# a `Write` impl on a private wrapper the rebuilt crate never builds with that
# type argument. The shapes below are invented: an all-stub impl nothing
# needs, one a `dyn` coercion needs, one a call reaches that would fall back
# to a `Deref` target's same-named method once deleted, and one only an
# in-crate test module calls.

_TRAIT_LIB = """#![allow(unused, dead_code)]
use core::fmt::{self, Debug, Write};
use core::ops::Deref;

struct Wrap<C>(C);
impl<C: fmt::Display> Debug for Wrap<C> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        ::core::unimplemented!()
    }
}
impl Write for Wrap<&mut String> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        ::core::unimplemented!()
    }
}
pub fn show(x: u8) -> String {
    format!("{:?}", &Wrap(x) as &dyn Debug)
}

struct Inner;
impl Inner {
    fn go(&self) -> u8 {
        1
    }
}
struct Outer(Inner);
impl Deref for Outer {
    type Target = Inner;
    fn deref(&self) -> &Inner {
        &self.0
    }
}
trait Go {
    fn go(&self) -> u8;
}
impl Go for Outer {
    fn go(&self) -> u8 {
        ::core::unimplemented!()
    }
}
pub fn call() -> u8 {
    Outer(Inner).go()
}

struct Probe;
impl Write for Probe {
    fn write_str(&mut self, _s: &str) -> fmt::Result {
        ::core::unimplemented!()
    }
}
#[cfg(test)]
mod checks {
    use super::*;
    #[test]
    fn probe() {
        let _ = write!(Probe, "x");
    }
}
"""


def _traits(tmp_path: Path) -> Path:
    root = tmp_path / "traits"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("gadgets", "traits"))
    (root / "src" / "lib.rs").write_text(_TRAIT_LIB)
    return root


def _rec(root: Path, name: str, container_has: str):
    rows = list(_structure().enumerate_fns(root) or ())
    from pipeline_core.domain.models import FnRecord
    recs = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r)) for r in rows]
    hit = [r for r in recs if r.fn_name == name and container_has in r.container]
    assert len(hit) == 1, [(r.fn_name, r.container) for r in recs]
    return hit[0], recs


def test_premise_the_lint_never_reports_a_trait_impl_method(tmp_path: Path) -> None:
    """The defect's premise: the unused all-stub `Write` impl is not in the
    lint's dead set, so a lint-only gate can never excuse it."""
    from pipeline_core.domain.dead_code import names_record
    root = _traits(tmp_path)
    rec, _ = _rec(root, "write_str", "Wrap")
    world = measure_world_dead_code(root, _clusters()[0],
                                    cluster_env=cluster_cargo_env, timeout=600)
    assert world.measured, world.reason
    assert not names_record(rec, world.spans), (
        "rustc's dead_code lint now reports a trait-impl method; the removal "
        "half of the end gate may be redundant and must be re-derived.")


def _bundle_of(rec, recs):
    from pipeline_core.domain.stub_removal import rec_key, units_of
    unit = units_of(recs)[rec_key(rec)]
    return {unit.key: frozenset({unit.key})}


def _builds_without(root: Path, rec, recs) -> bool:
    """`cargo check --lib --tests` on a symlink-following copy with `rec`'s
    unit deleted: the build alone, the certificate before the MIR comparison."""
    import shutil

    from pipeline_core.adapters.process import run_bounded
    from pipeline_core.domain.stub_removal import delete_ranges, rec_key, units_of
    unit = units_of(recs)[rec_key(rec)]
    copy = root.parent / f"{root.name}_cut"
    shutil.copytree(root, copy, symlinks=False, ignore=shutil.ignore_patterns("target"))
    path = copy / unit.key[0]
    path.write_bytes(delete_ranges(path.read_bytes(), [unit.key[1:]]))
    env = cluster_cargo_env(_clusters()[0])
    env["CARGO_TARGET_DIR"] = str(copy / "target")
    run = run_bounded(["cargo", "check", "--lib", "--tests"], cwd=copy, env=env, timeout=600.0)
    shutil.rmtree(copy, ignore_errors=True)
    return run.returncode == 0


def test_premise_deleting_an_impl_can_build_while_a_call_reached_it(tmp_path: Path) -> None:
    """Why the build alone is not a proof: with `impl Go for Outer` deleted,
    `Outer(Inner).go()` resolves to `Inner::go` through `Deref` and the crate
    still builds. The removal reading refuses it because `call`'s MIR
    changed."""
    root = _traits(tmp_path)
    rec, recs = _rec(root, "go", "Go for Outer")
    assert _builds_without(root, rec, recs), (
        "deleting the Go impl no longer builds: method resolution stopped "
        "falling back through Deref; this premise must be re-derived.")
    got, reason = measure_world_removal(root, _clusters()[0], _bundle_of(rec, recs),
                                        cluster_env=cluster_cargo_env, timeout=600)
    assert reason == "" and got == frozenset(), (got, reason)


def test_premise_a_cut_moves_positions_the_compiler_stores_as_data(
        tmp_path: Path, monkeypatch) -> None:
    """Why deletion blanks instead of cutting: the test harness stores each
    test's line as a constant (`start_line: const 55_usize` in the test
    profile's MIR, measured 2026-11-08), so cutting the unused `Write` impl
    above `mod checks` changes a surviving item and the removal reading
    refuses a stub nothing calls. Blanked, it is certified."""
    from pipeline_core.adapters import stub_removal as adapter
    root = _traits(tmp_path)
    rec, recs = _rec(root, "write_str", "Wrap")
    bundles = _bundle_of(rec, recs)
    got, reason = measure_world_removal(root, _clusters()[0], bundles,
                                        cluster_env=cluster_cargo_env, timeout=600)
    assert reason == "" and got == frozenset(bundles), (got, reason)

    def cut(text: bytes, ranges) -> bytes:
        out, at = bytearray(), 0
        for a, b in sorted(ranges):
            out += text[at:a]
            at = b
        return bytes(out + text[at:])
    monkeypatch.setattr(adapter, "delete_ranges", cut)
    got, reason = measure_world_removal(root, _clusters()[0], bundles,
                                        cluster_env=cluster_cargo_env, timeout=600)
    assert reason == "" and got == frozenset(), (
        "a cut no longer changes the test descriptor's line constant; positions "
        "may have left the test-profile MIR", got, reason)


def test_the_gate_proves_only_the_trait_impl_nothing_can_call(tmp_path: Path) -> None:
    root = _traits(tmp_path)
    clusters = _clusters()[:1]
    log: list[str] = []
    proofs = read_unreached_stub_proofs(root, clusters, _deps(log))
    wrap_write, _ = _rec(root, "write_str", "Wrap")
    from pipeline_core.domain.attribution import record_site
    assert set(proofs) == {record_site(wrap_write)}, (proofs, log)
    text = "\n".join(log)
    assert "Debug for Wrap" in text and "stops building" in text    # the dyn coercion
    assert "Go for Outer" in text and "used in" in text            # the fallback, by MIR
    assert "Write for Probe" in text                               # the in-crate test
    cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false) for c in clusters]
    stubs = scan_stub_inventory(root, enumerate_fns=_structure().enumerate_fns).site_preds()
    verdict = classify_remaining_stubs(stubs, cfg_sets, (), (), proofs)
    assert FnLocator("src/lib.rs", "write_str") in verdict.dead_in_tree
    # NEGATIVE CONTROL: the lint-only reading (no compiler view, no removal)
    # proves none of the four trait-impl stubs.
    lint_only = UnreachedStubDeps(
        enumerate_fns=_structure().enumerate_fns,
        enumerate_mods=_structure().enumerate_mods,
        measure_dead=functools.partial(
            measure_world_dead_code, cluster_env=cluster_cargo_env, timeout=600),
        read_compiler_view=lambda _p, c: __import__(
            "pipeline_core.domain.fn_reachability", fromlist=["WorldReading"]
        ).WorldReading(c.name, reason="not read"),
        measure_removal=lambda *_a: (frozenset(), "not read"),
        log=[].append)
    assert read_unreached_stub_proofs(root, clusters, lint_only) == {}


# ---- the fixed point: uses behind dead code, and what MIR never shows ----
# Review of the first fix (2026-11-08): a use inside a closure had no owner,
# and a use inside a trait-impl method only dead code calls was live because
# the lint never names one, so both kept a stub nothing live calls. Walking
# that fix found the opposite: a stub `Drop` impl has no MIR use, deletes
# cleanly, and runs at every scope exit of a live value. Its review found the
# rule then kept every private `Drop`, so one on a type nothing builds still
# failed a green run. The certificate is now the compiler's: a removal that
# builds must also leave every surviving fn's MIR unchanged.

_CHAIN_LIB = """#![allow(unused, dead_code)]
use core::fmt::{self, Write};

struct Sink<W>(W);
impl Write for Sink<&mut String> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        ::core::unimplemented!()
    }
}
fn unused_helper(buf: &mut String) -> fmt::Result {
    let f = |b: &mut String| write!(Sink(b), "x");
    f(buf)
}

struct Sink2<W>(W);
impl Write for Sink2<&mut String> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        ::core::unimplemented!()
    }
}
struct Pen;
trait Draw {
    fn draw(&self) -> u8;
}
impl Draw for Pen {
    fn draw(&self) -> u8 {
        let mut s = String::new();
        let _ = Sink2(&mut s).write_str("y");
        1
    }
}
fn unused_drawer() -> u8 {
    Pen.draw()
}

struct Guard;
impl Drop for Guard {
    fn drop(&mut self) {
        ::core::unimplemented!()
    }
}
pub fn guarded() -> u8 {
    let _g = Guard;
    3
}

struct Held(u8);
impl Drop for Held {
    fn drop(&mut self) {
        ::core::unimplemented!()
    }
}
struct Parent {
    h: Held,
    n: u8,
}
fn consume(p: Parent) -> u8 {
    p.n
}
pub fn nested(x: u8) -> u8 {
    consume(Parent { h: Held(x), n: x })
}

struct Never(u8);
impl Drop for Never {
    fn drop(&mut self) {
        ::core::unimplemented!()
    }
}
enum Slot {
    Full(Never),
    Empty,
}
"""


def _chain(tmp_path: Path) -> Path:
    root = tmp_path / "chain"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("gadgets", "chain"))
    (root / "src" / "lib.rs").write_text(_CHAIN_LIB)
    return root


def test_premise_deleting_an_impl_the_compiler_calls_builds_and_changes_its_callers_mir(
        tmp_path: Path) -> None:
    """Why the build alone is not a proof, second shape: nothing in MIR calls
    `Guard`'s drop and the crate builds without it, while every live `Guard`
    runs it at scope exit. The drop the compiler inserted in `guarded`
    disappears from its MIR, and that is what the removal reading sees. The
    unbuilt `Never`'s drop changes no surviving fn."""
    from pipeline_core.domain.stub_removal import rec_key, units_of
    root = _chain(tmp_path)
    guard, recs = _rec(root, "drop", "Drop for Guard")
    never, _ = _rec(root, "drop", "Drop for Never")
    assert _builds_without(root, guard, recs), "deleting a Drop impl no longer builds"
    units = units_of(recs)
    g, n = units[rec_key(guard)].key, units[rec_key(never)].key
    bundles = {g: frozenset({g}), n: frozenset({n})}
    got, reason = measure_world_removal(root, _clusters()[0], bundles,
                                        cluster_env=cluster_cargo_env, timeout=600)
    assert reason == "" and got == frozenset({n}), (got, reason)


def test_the_gate_proves_stubs_behind_dead_code_and_keeps_what_the_program_needs(
        tmp_path: Path, monkeypatch) -> None:
    from pipeline_core.domain.attribution import record_site
    root = _chain(tmp_path)
    log: list[str] = []
    proofs = read_unreached_stub_proofs(root, _clusters()[:1], _deps(log))
    sink, _ = _rec(root, "write_str", "Sink <")
    sink2, _ = _rec(root, "write_str", "Sink2")
    guard, _ = _rec(root, "drop", "Drop for Guard")
    held, _ = _rec(root, "drop", "Drop for Held")
    never, _ = _rec(root, "drop", "Drop for Never")
    assert record_site(sink) in proofs, log          # a closure of a dead fn
    assert record_site(sink2) in proofs, log         # a trait method only dead code calls
    assert record_site(guard) not in proofs, proofs  # `guarded` holds a Guard
    assert record_site(held) not in proofs, proofs   # `nested` builds a Parent holding one
    assert record_site(never) in proofs, (proofs, log)   # nothing builds a Never
    text = "\n".join(log)
    assert "Drop for Guard" in text and "MIR changes" in text, log
    assert "Drop for Held" in text, log
    # NEGATIVE CONTROL: the build alone as the certificate proves the two
    # drops the program runs.
    from pipeline_core.adapters import stub_removal as adapter
    monkeypatch.setattr(adapter, "mir_unchanged", lambda _b, _c: True)
    loose = read_unreached_stub_proofs(root, _clusters()[:1], _deps([]))
    assert {record_site(guard), record_site(held)} <= set(loose), loose


def test_premise_a_test_crate_cannot_call_a_method_on_a_private_type(tmp_path: Path) -> None:
    """Why the integration tests' MIR is not read: a call a test crate makes
    can only reach a trait impl whose self type it can see. Even a value of a
    private type a public fn returns cannot have a method called on it from
    another crate, so the `Deref` fallback cannot be written in `tests/`."""
    from pipeline_core.adapters.process import run_bounded
    root = tmp_path / "priv"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("gadgets", "privy"))
    (root / "src" / "lib.rs").write_text(
        "#![allow(unused, dead_code, private_interfaces)]\n"
        "use core::ops::Deref;\n"
        "pub struct Inner;\nimpl Inner { pub fn go(&self) -> u8 { 1 } }\n"
        "struct Outer(Inner);\n"
        "impl Deref for Outer { type Target = Inner; fn deref(&self) -> &Inner { &self.0 } }\n"
        "pub trait Go { fn go(&self) -> u8; }\n"
        "impl Go for Outer { fn go(&self) -> u8 { 2 } }\n"
        "pub fn make() -> Outer { Outer(Inner) }\n")
    (root / "tests" / "t.rs").write_text(
        "use privy::*;\n#[test] fn t() { assert_eq!(make().go(), 2); }\n")
    env = cluster_cargo_env(_clusters()[0])
    env["CARGO_TARGET_DIR"] = str(root / "target")
    run = run_bounded(["cargo", "check", "--tests"], cwd=root, env=env, timeout=600.0)
    assert run.returncode != 0 and "is private" in (run.stderr or ""), (
        "a test crate can now call a method on a value of a private type; the "
        "gate must read the integration tests' MIR as well.")
