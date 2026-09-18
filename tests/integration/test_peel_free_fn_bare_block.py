"""Integration: a free fn answered with `block=""` must be peelable.

Real cargo check, the pipeline's own diagnostic runner, the real syn
extractor, the real reply parser, router, splice and fn-granular peel. No
model.

The class: the output contract spells a free fn as `block=""`, and the router
sent every such body to the prompt's TARGET container. The splice overruled
that silently — `locate_fn_by_record` narrows by container only when some
record matches, so the body still landed in the free fn — but the peel keyed
the landed body by the ROUTED container. rustc's span resolves to
`(helper, "")`, the landed body said `(helper, "impl Target")`, nothing
matched, and the peel refused `none_implicated`, reverting every compiling
body in the reply with the one wrong one. Run `3109629-f6bcbf01` (2026-09-18,
src/error.rs file-first): three attempts, 23 minutes, fifteen compiling bodies
reverted each time for one error inside a free fn.

The crate is invented: one inherent impl with one stub method and two free
stub fns. The reply answers all three; one free fn's body is a type error.
The negative control is the pre-fix routing (no member table, so a bare body
goes to the target) through the same splice and peel, producing the refusal.

Gated on `INTEGRATION=1` (it runs cargo); `scripts/run_integration.sh` is the
launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct import attribution as attr
from pipeline_core.application.reconstruct.cargo_runner import run_check_diag
from pipeline_core.application.reconstruct.impl_blocks import collect_impl_blocks
from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelRefusal,
    PeelResult,
    peel_file_first,
)
from pipeline_core.application.reconstruct.splice import splice_fn_with_where
from pipeline_core.application.reconstruct.tree_txn import capture
from pipeline_core.domain.impl_routing import (
    containers_in_file,
    members_in_file,
    norm_src_rel,
    route_fn_bodies,
)
from pipeline_core.domain.llm_output import extract_fn_bodies

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "peel-free-fn"
_CHECK = "cargo check --offline --all-targets"

_CARGO_TOML = """[package]
name = "gadgets"
version = "0.1.0"
edition = "2021"
"""

_LIB = """pub struct Gadget {
    pub n: u32,
}

impl Gadget {
    pub fn build(n: u32) -> Gadget {
        unimplemented!()
    }
}

pub fn doubled(x: u32) -> u32 {
    unimplemented!()
}

pub fn label(x: u32) -> u32 {
    unimplemented!()
}
"""

_REPLY = """<FN name="build" block="impl Gadget">
Gadget { n: doubled(n) }
</FN>
<FN name="doubled" block="">
x * 2
</FN>
<FN name="label" block="">
"not a number"
</FN>
"""


def _crate(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB)
    return root


def _enum() -> CliStructureReader:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    return CliStructureReader(CliExtractor(_EXTRACTOR))


def _deps(reader: CliStructureReader) -> attr.AttributionDeps:
    return attr.AttributionDeps(
        structure=reader,
        classify=lambda **_k: {},
        locate_fn=lambda *_a, **_k: None,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lambda _m: None,
    )


def _check(cmd: str, cwd: Path, env: dict[str, str] | None):
    e = dict(os.environ)
    e.update(env or {})
    e["CARGO_TARGET_DIR"] = str(_TARGET)
    return run_check_diag(CargoCli(), cmd, cwd, e, 300)


def _land_and_peel(root: Path, *, with_members: bool) -> PeelResult | PeelRefusal:
    reader = _enum()
    enum = reader.enumerate_fns
    file_rel = "src/lib.rs"
    file_path = root / file_rel
    blocks = collect_impl_blocks(root, enumerate_fns=enum) or []
    conts = containers_in_file(blocks, file_rel)
    routing = route_fn_bodies(
        extract_fn_bodies(_REPLY), target=conts[0], related=list(conts[1:]),
        members=members_in_file(blocks, file_rel) if with_members else ())
    pre = capture(file_path, None)
    landed: list[LandedBody] = []
    for rb in routing.routed:
        fd = rb.fn_data
        ok, reason, _delta = splice_fn_with_where(
            enum, file_path, rb.container, fd["fn_name"], fd["body"], None,
            sig_text=rb.sig_text)
        assert ok, (fd["fn_name"], reason)
        landed.append(LandedBody(fd["fn_name"], rb.container, rb.sig_text,
                                 fd["body"], None))
    rc, _out, diags = _check(_CHECK, root, None)
    assert rc != 0, "the reply must not compile — `label` returns a &str"
    peel, _rc, _o, _d = peel_file_first(
        file_path=file_path, pre_image=pre, file_rel=file_rel,
        landed=landed, diags=diags, target_src_dir=root,
        check_cmd=_CHECK, cluster_env={},
        implicated_keys_from_diags=lambda ds, sd: attr.implicated_fn_keys(
            _deps(reader), list(ds), sd),
        run_cargo_check_diag=_check,
        splice_fn_with_where=lambda *a, **k: splice_fn_with_where(enum, *a, **k),
        norm_src_rel=norm_src_rel)
    return peel


def test_premise_rustc_blames_the_free_fn(tmp_path: Path) -> None:
    root = _crate(tmp_path / "premise")
    (root / "src" / "lib.rs").write_text(
        _LIB.replace('pub fn label(x: u32) -> u32 {\n    unimplemented!()',
                     'pub fn label(x: u32) -> u32 {\n    "not a number"'))
    rc, _out, diags = _check(_CHECK, root, None)
    assert rc != 0
    keys = attr.implicated_fn_keys(_deps(_enum()), list(diags), root)
    assert ("src/lib.rs", "label", "") in keys, (
        "rustc's span no longer resolves to the free fn with an empty "
        f"container — the peel identity this test pins was derived from that: {keys}")


def test_a_bare_block_free_fn_error_peels_only_that_body(tmp_path: Path) -> None:
    peel = _land_and_peel(_crate(tmp_path / "fixed"), with_members=True)
    assert isinstance(peel, PeelResult), peel
    assert peel.reverted == ("label",)
    assert sorted(b.fn_name for b in peel.kept) == ["build", "doubled"]


def test_negative_control_target_routing_refuses_the_peel(tmp_path: Path) -> None:
    peel = _land_and_peel(_crate(tmp_path / "prefix"), with_members=False)
    assert isinstance(peel, PeelRefusal)
    assert peel.reason == "none_implicated"
