"""FnFixUnit / ImplFixUnit drive their inner cargo-check loop over ISP role
ports (P3.C). Fakes script the probe + model; we assert verdict, prompt wiring,
splice accumulation, and the loop exit."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fix_unit import (
    FixUnitDeps,
    FnFixUnit,
    ImplFixUnit,
)
from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.fix_target import FnTarget, ImplTarget
from pipeline_core.domain.models import FnRecord, ImplBlock


class _Checker:
    """Dirty (the target's fn is in the frontier) until `clean_after` probes."""

    def __init__(self, file_rel: str, fn: str, clean_after: int = 1) -> None:
        self.file_rel, self.fn, self.clean_after = file_rel, fn, clean_after
        self.calls = 0

    def probe(self, cmd: str, cwd: Path, env: dict[str, str],
              timeout: int) -> CompileProbe:
        self.calls += 1
        if self.calls > self.clean_after:
            return CompileProbe(rc=0, rendered="ok")
        return CompileProbe(rc=101, rendered="E0277 boom",
                            frontier={(self.file_rel, self.fn): {"t"}},
                            errors_by_file={self.file_rel: 1})


class _Model:
    def __init__(self, resp: str) -> None:
        self.resp, self.prompts = resp, []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.resp


class _Splicer:
    def __init__(self) -> None:
        self.spliced: list[tuple[str, str | None]] = []

    def locate_fn(self, text: str, name: str):
        return (f"fn {name}()", 0, 0, len(text))

    def is_pub_fn(self, text: str, name: str) -> bool:
        return False

    def render_impl_block(self, text: str, members) -> str:
        return "IMPL_BLOCK_SRC"

    def splice_fn(self, file_path, container, name, body, *,
                  where_clause=None, allow_nonstub=False):
        self.spliced.append((name, where_clause))
        return True, "ok", len(body)


class _Prompts:
    def repo_spec_block(self, *, file_rel="", fn_name="", container="") -> str:
        return "<REPO/>"

    def gotchas_block(self, output: str) -> str:
        return "<GOTCHAS/>"

    def tests_block(self, names) -> str:
        return f"<TESTS {sorted(names)}/>"


def _deps(checker, model, splicer):
    return FixUnitDeps(
        checker=checker, model=model, splicer=splicer, prompts=_Prompts(),
        log=lambda _m: None, do_retro=lambda _o, _h: "",
        thinking_directive="THINK ", err_cap=2000, per_fn_max_stalls=2)


def _session(tmp: Path) -> FixSession:
    return FixSession.for_check_cmd(
        project_dir=tmp, check_cmd="cargo test", derive_check_only=lambda c: "cargo check",
        env={}, timeout=30, model="m")


def test_fn_unit_splices_then_loop_accepts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("fn foo() { unimplemented!() }\n")
    model = _Model('<FN name="foo" block="any">\n42\n</FN>')
    splicer = _Splicer()
    checker = _Checker("src/lib.rs", "foo", clean_after=1)
    unit = FnFixUnit(_session(tmp_path), _deps(checker, model, splicer),
                     FnTarget("src/lib.rs", "foo"))
    changed = unit.run()
    assert changed == ["src/lib.rs::foo"]
    assert splicer.spliced == [("foo", None)]
    assert "THINK " in model.prompts[0] and "<REPO/>" in model.prompts[0]


def test_fn_unit_missing_file_is_noop(tmp_path: Path) -> None:
    unit = FnFixUnit(_session(tmp_path),
                     _deps(_Checker("src/x.rs", "f"), _Model(""), _Splicer()),
                     FnTarget("src/x.rs", "f"))
    assert unit.run() == []


def test_impl_unit_splices_each_member(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/m.rs").write_text("impl Foo { fn a(){} fn b(){} }\n")
    block = ImplBlock(file="src/m.rs", container="impl Foo",
                      members=(FnRecord(file="src/m.rs", fn_name="a"),
                               FnRecord(file="src/m.rs", fn_name="b")))
    model = _Model('<FN name="a" block="impl">\n1\n</FN>\n<FN name="b" block="impl">\n2\n</FN>')
    splicer = _Splicer()
    checker = _Checker("src/m.rs", "a", clean_after=1)
    unit = ImplFixUnit(_session(tmp_path), _deps(checker, model, splicer),
                       ImplTarget("src/m.rs", "impl Foo", block))
    changed = unit.run()
    assert changed == ["src/m.rs::a", "src/m.rs::b"]
    assert "IMPL_BLOCK_SRC" in model.prompts[0]


def test_fn_unit_verify_reports_metric(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("fn foo(){}\n")
    checker = _Checker("src/lib.rs", "foo", clean_after=99)
    unit = FnFixUnit(_session(tmp_path), _deps(checker, _Model(""), _Splicer()),
                     FnTarget("src/lib.rs", "foo"))
    ok, rendered, metric = unit.verify()
    assert not ok and metric == 1 and "boom" in rendered
