"""The pair-fill route: a cross-file dispatcher/implementer pair dispatches
as ONE transaction — one prompt carrying both blocks, both replies spliced,
one compile+test reading, kept or reverted together.

The class (run `358833-aa15d93c`, 2026-09-01): the paying write is
co-dependent across two files, and each half alone is correctly rejected by
the revert gate, so no sequence of independently-judged keeps can land it —
the recorded shape was three `src/fmt.rs` opens and two `src/error.rs`
reverts with no joint judgment possible.

The crate here is deliberately NOT that run's shape: a Display dispatcher in
`lib.rs` delegating to a `render.rs` implementer — the never-steer check's
own example. The mechanism reads only the hop's pair record and the
collected blocks; it must fire identically on this shape.
"""
from __future__ import annotations

from pathlib import Path

import pipeline
from pipeline_core.domain.attribution import FailureFrontier, FormatterPair
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from tests.harness import (
    diag,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
)

DISPATCHER_LIB = """use std::fmt;
pub struct Report { msg: String }

impl fmt::Display for Report {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.msg)
    }
}
"""

IMPLEMENTER_LIB = """pub struct Renderer { pub depth: u32 }

impl Renderer {
    pub fn render(&self) -> u32 {
        self.depth
    }
}
"""

_DISP = FnLocator("lib.rs", "fmt")
_IMPL = FnLocator("render.rs", "render")
_PAIR = FormatterPair(
    FnSite("lib.rs", "fmt", "impl fmt::Display for Report"),
    FnSite("render.rs", "render", "impl Renderer"))

PAIR_REPLY = (
    '<FN name="fmt" block="impl fmt::Display for Report">'
    'write!(f, "PAIRED {}", self.msg)</FN>\n'
    '<FN name="render" block="impl Renderer">self.depth + 7</FN>'
)


def _blocks() -> list[ImplBlock]:
    disp = FnRecord(file="lib.rs", fn_name="fmt",
                    container="impl fmt::Display for Report",
                    sig_text="fn fmt (& self , f : & mut fmt :: Formatter) -> fmt :: Result")
    impl_ = FnRecord(file="render.rs", fn_name="render",
                     container="impl Renderer",
                     sig_text="pub fn render (& self) -> u32")
    return [
        ImplBlock(file="lib.rs", container="impl fmt::Display for Report",
                  members=(disp,), refill_members=(disp,)),
        ImplBlock(file="render.rs", container="impl Renderer",
                  members=(impl_,), refill_members=(impl_,)),
    ]


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(DISPATCHER_LIB)
    (ws / "src" / "render.rs").write_text(IMPLEMENTER_LIB)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: _blocks())
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[], dispatch_families=[])
    return ws


def _install_model(monkeypatch, responses, prompts):
    seq = list(responses)

    def _ask(prompt, *a, **k):
        prompts.append(prompt)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)


def _frontier_tests() -> FailureFrontier:
    return FailureFrontier.build(
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        {_DISP: {"t_pair"}, _IMPL: {"t_pair"}},
        pairs=(_PAIR,))


def _run(ws: Path) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws,
        target_src_dir=ws / "src",
        tests_dir_files=[],
        module_name="m",
        cluster_name="t",
        cluster=None,
        check_cmd="cargo check",
        cluster_env={},
        fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=_frontier_tests(),
    )


def test_a_pair_dispatches_once_and_both_halves_land_together(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = _run(ws)
    lib = (ws / "src" / "lib.rs").read_text()
    ren = (ws / "src" / "render.rs").read_text()
    assert "PAIRED" in lib and "self.depth + 7" in ren
    # ONE prompt for the pair — not two independent opens — and it carries
    # BOTH target blocks and the paired-write instruction.
    pair_prompts = [p for p in prompts if "PAIRED WRITE" in p]
    assert len(pair_prompts) == 1
    assert 'TARGET_IMPL_BLOCK file="lib.rs"' in pair_prompts[0]
    assert 'TARGET_IMPL_BLOCK file="render.rs"' in pair_prompts[0]
    assert res.fns_filled >= 2
    assert (_DISP[0], _DISP[1]) in (res.dispatched_fns or ())
    assert (_IMPL[0], _IMPL[1]) in (res.dispatched_fns or ())
    # The pair consumed its members for this call: no later route re-opened
    # either half (their refill entries dropped after the pair served them).
    assert len(prompts) == len(pair_prompts)


def test_a_failing_pair_reverts_both_halves_as_one_transaction(
        monkeypatch, tmp_path):
    """Negative control for the transaction: the recorded pre-fix shape was
    half-at-a-time judgments; here a red reading after the joint splice
    restores BOTH files — never a kept half."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error: x", [diag("src/lib.rs", code="E0308")]))
    res = _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB
    assert any("PAIRED WRITE" in p for p in prompts)
    assert res.impls_failed >= 1
    # Both halves reported reverted, so the landed-change audit grades
    # neither as a measured try.
    assert ("lib.rs", "fmt") in (res.reverted_fns or ())
    assert ("render.rs", "render") in (res.reverted_fns or ())


def test_a_reply_covering_one_half_keeps_nothing(monkeypatch, tmp_path):
    """HALF a pair on the tree is the state the route exists to never
    create: a reply that answers only the implementer leaves both files
    untouched."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(
        monkeypatch,
        ['<FN name="render" block="impl Renderer">self.depth + 7</FN>'],
        prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == DISPATCHER_LIB
    assert (ws / "src" / "render.rs").read_text() == IMPLEMENTER_LIB


def test_without_a_pair_the_route_is_inert(monkeypatch, tmp_path):
    """No pair on the frontier → no pair prompt; the ordinary routes keep
    the call (pre-fix behaviour preserved)."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, [PAIR_REPLY], prompts)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_DISP},
        tdd_frontier_tests=FailureFrontier.build(
            {_DISP: {"t_pair"}}, {_DISP: {"t_pair"}}),
    )
    assert not any("PAIRED WRITE" in p for p in prompts)
