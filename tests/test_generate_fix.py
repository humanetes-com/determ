"""generate_fix strategies + driver (P3.C): select_strategy mapping, the
whole-file delegate, and the per-impl one-shot fill on a clean-compile/failing
-test frontier."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fix_unit import FixUnitDeps
from pipeline_core.application.reconstruct.generate_fix import (
    GenerateFixDeps,
    PerFnFix,
    PerImplFix,
    WholeFileFix,
    select_strategy,
)
from pipeline_core.domain.enums import FillUnit, FixStrategy
from pipeline_core.domain.fix_report import FixKind, FixReport
from pipeline_core.domain.fix_session import FixSession


class _Model:
    def __init__(self, resp: str = "") -> None:
        self.resp = resp

    def ask(self, prompt: str) -> str:
        return self.resp


class _Splicer:
    def __init__(self) -> None:
        self.body_spliced: list[str] = []

    def locate_fn(self, text: str, name: str):
        return (f"fn {name}()", 0, 0, len(text))

    def is_pub_fn(self, text: str, name: str) -> bool:
        return False

    def render_impl_block(self, text: str, members) -> str:
        return "BLK"

    def splice_fn(self, *a, **k):
        return True, "ok", 1

    def splice_body(self, file_path, name, body, *, allow_nonstub=False):
        self.body_spliced.append(name)
        return True, len(body)


class _Prompts:
    def repo_spec_block(self, *, file_rel="", fn_name="", container="") -> str:
        return ""

    def gotchas_block(self, output: str) -> str:
        return ""

    def tests_block(self, names) -> str:
        return ""


class _Frontiers:
    def __init__(self, compile_fr=None, tdd_fr=None, beh=None) -> None:
        self._c, self._t, self._b = compile_fr or {}, tdd_fr or {}, beh or ({}, {})

    def compile_frontier(self, output): return dict(self._c)
    def tdd_frontier(self, output): return dict(self._t)
    def behavior_frontier(self, output): return dict(self._b[0]), dict(self._b[1])


def _deps(frontiers, splicer=None, model=None, whole=None) -> GenerateFixDeps:
    unit = FixUnitDeps(
        checker=object(), model=model or _Model(), splicer=splicer or _Splicer(),
        prompts=_Prompts(), log=lambda _m: None, do_retro=lambda _o, _h: "",
        thinking_directive="T", err_cap=500, per_fn_max_stalls=2)
    return GenerateFixDeps(
        unit=unit, frontiers=frontiers,
        enumerate_fns=lambda: [], collect_impl_blocks=lambda: [],
        log_overview=lambda _o, _n: None,
        whole_file_fix=whole or (lambda o, h, r: FixReport.changed(
            summary="whole", files_changed_str="x", error_preview="")),
        behavior_max_fns=8, src_snip_cap=4000)


def _session(tmp: Path) -> FixSession:
    return FixSession.for_check_cmd(
        project_dir=tmp, check_cmd="cargo test", derive_check_only=lambda c: "cargo check",
        env={}, timeout=30, model="m")


def test_select_strategy_maps_settings(tmp_path: Path) -> None:
    d = _deps(_Frontiers())
    s = _session(tmp_path)
    assert isinstance(select_strategy(FixStrategy.PER_FN, FillUnit.IMPL, s, d), PerImplFix)
    assert isinstance(select_strategy(FixStrategy.PER_FN, FillUnit.FN, s, d), PerFnFix)
    assert isinstance(select_strategy(FixStrategy.WHOLE_FILE, FillUnit.FN, s, d), WholeFileFix)


def test_timeout_aborts(tmp_path: Path) -> None:
    rep = PerFnFix(_session(tmp_path), _deps(_Frontiers())).attempt("TIMEOUT after 5s", [])
    assert rep.kind is FixKind.ABORT and rep.reason == "check_timeout"


def test_empty_compile_frontier_falls_back_to_whole_file(tmp_path: Path) -> None:
    called: list[str] = []
    d = _deps(_Frontiers(compile_fr={}), whole=lambda o, h, r: (
        called.append("whole") or FixReport.changed(summary="w", files_changed_str="x", error_preview="")))
    rep = PerFnFix(_session(tmp_path), d).attempt("err", [])
    assert called == ["whole"] and rep.summary == "w"


def test_per_impl_one_shot_fills_tdd_frontier(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("fn foo(){ unimplemented!() }\n")
    splicer = _Splicer()
    d = _deps(_Frontiers(compile_fr={}, tdd_fr={("src/lib.rs", "foo"): {"t"}}),
              splicer=splicer, model=_Model('<FN name="foo" block="any">\n7\n</FN>'))
    rep = PerImplFix(_session(tmp_path), d).attempt("test failed", [])
    assert splicer.body_spliced == ["foo"]
    assert rep.summary == "tdd_stub changed=1"
