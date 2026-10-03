"""generate_fix strategies + driver: select_strategy mapping, the
whole-file delegate, and the per-impl one-shot fill on a clean-compile/failing
-test frontier."""
from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Lock

from pipeline_core.adapters.fill_engine_roles import MutatorAdapter
from pipeline_core.application.reconstruct.fix_roles import FixSplicerAdapter
from pipeline_core.application.reconstruct.fix_unit import FixUnitDeps, one_shot_fill
from pipeline_core.application.reconstruct.generate_fix import (
    GenerateFixDeps,
    PerFnFix,
    PerImplFix,
    WholeFileFix,
    select_strategy,
)
from pipeline_core.application.reconstruct.splice import (
    locate_fn_by_record,
    splice_fn_body,
    splice_fn_body_at,
)
from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.enums import FillUnit, FixStrategy
from pipeline_core.domain.fix_report import FixKind, FixReport
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.language_driver import using_driver
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.rust_fn_headers import locate_all_fns
from pipeline_core.prompts.language_catalog import catalog_driver


class _Model:
    def __init__(self, resp: str = "") -> None:
        self.resp = resp
        self.prompts: list[str] = []

    def ask_turns(self, messages, **_k):  # the transcript port
        return self.ask(str(messages[-1]["content"]))

    def fit(self, prompt: str) -> str:
        return prompt

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.resp


class _Splicer:
    def __init__(self) -> None:
        self.body_spliced: list[str] = []
        self.scoped_sigs: list[str] = []

    def locate_fn(self, text: str, name: str):
        return (f"fn {name}()", 0, 0, len(text))

    def is_pub_fn(self, text: str, name: str) -> bool:
        return False

    def render_impl_block(self, text: str, members) -> str:
        return "BLK"

    def splice_fn(self, *a, **k):
        return True, "ok", 1

    def splice_body(self, file_path, name, body, *, allow_nonstub=False):
        raise AssertionError(
            "the one-shot fill must write through the record locator: the "
            "bare-name path refuses ambiguous_name on any file holding two fns "
            "of a name")

    def splice_body_scoped(self, file_path, name, container, sig_text, new_body,
                           *, allow_nonstub=False):
        self.body_spliced.append(name)
        self.scoped_sigs.append(sig_text)
        return True, "ok", len(new_body)


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


def _deps(frontiers, splicer=None, model=None, whole=None, checker=None,
          enumerate_fns=None, impl_blocks=None) -> GenerateFixDeps:
    unit = FixUnitDeps(
        checker=checker or object(), model=model or _Model(),
        splicer=splicer or _Splicer(),
        prompts=_Prompts(), log=lambda _m: None, do_retro=lambda _o, _h: "",
        thinking_directive="T", err_cap=500, per_fn_max_stalls=2)
    return GenerateFixDeps(
        unit=unit, frontiers=frontiers,
        enumerate_fns=enumerate_fns or (lambda: []),
        collect_impl_blocks=(lambda: impl_blocks or []),
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


def test_one_shot_fill_lands_a_body_when_the_file_holds_two_fns_of_that_name(
        tmp_path: Path) -> None:
    """End-to-end against the REAL scoped splice.

    Against the pre-fix code this fails the way the 2026-08-05 run did:
    `[fix loop/behavior] splice refused src/error.rs::from: ambiguous_name`,
    a full model call spent and its answer discarded. The body must reach disk
    in the fn the prompt was built from, and the same-named sibling must be
    untouched.
    """
    src = tmp_path / "src"
    src.mkdir()
    f = src / "error.rs"
    f.write_text(
        "impl From<E> for Error {\n"
        "    fn from(error: E) -> Self { unimplemented!() }\n"
        "}\n"
        "impl From<Error> for Box<dyn StdError> {\n"
        "    fn from(error: Error) -> Self { second() }\n"
        "}\n"
    )
    text = f.read_text()
    first = text.index("unimplemented!()")
    recs = [
        {"file": "error.rs", "fn_name": "from",
         "container": "impl From < E > for Error",
         "sig_text": "fn from(error: E) -> Self",
         "sig_byte_start": text.index("    fn from"),
         "body_byte_start": first, "body_byte_end": first + len("unimplemented!()"),
         "body_text": "unimplemented!()", "has_unimplemented_body": True},
        {"file": "error.rs", "fn_name": "from",
         "container": "impl From < Error > for Box < dyn StdError >",
         "sig_text": "fn from(error: Error) -> Self",
         "sig_byte_start": text.rindex("    fn from"),
         "body_byte_start": text.index("second()"),
         "body_byte_end": text.index("second()") + len("second()"),
         "body_text": "second()", "has_unimplemented_body": False},
    ]
    mut = MutatorAdapter(
        locate_fn_by_record_fn=partial(locate_fn_by_record, lambda _sd: recs),
        locate_fn_fn=lambda t, n: (locate_all_fns(t, n) or [None])[0],
        splice_fn_body_at_fn=splice_fn_body_at,
        splice_fn_body_fn=splice_fn_body,
        replace_fn_signature_fn=lambda *a: False,
        extract_signature_block_fn=lambda r: None,
        lock_for_fn=lambda p: Lock(),
    )
    splicer = FixSplicerAdapter(
        locate_fn_fn=mut.locate_fn, is_pub_fn_fn=lambda t, n: False,
        render_impl_block_fn=lambda t, m: "BLK",
        splice_fn_fn=lambda *a, **k: SpliceOutcome(False, "unused", 0),
        splice_body_fn=mut.splice_fn_body,
        splice_body_scoped_fn=mut.splice_body_scoped,
        restore_signature_fn=lambda *a: False,
    )
    changed = one_shot_fill(
        {("src/error.rs", "from"): {"test_boxed_str"}}, kind="behavior",
        behavior_srcs={}, session=_session(tmp_path),
        model=_Model("<FN name=\"from\" block=\"any\">\n{ first() }\n</FN>"),
        splicer=splicer, log=lambda _m: None, thinking_directive="T",
        err_cap=500, src_snip_cap=4000, output="test_boxed_str FAILED")
    out = f.read_text()
    assert changed == ["src/error.rs::from"]
    assert "first()" in out, "the model's body never reached disk"
    assert "unimplemented!()" not in out, "the stub was not the body replaced"
    assert "second()" in out, "the same-named sibling was overwritten"


def test_one_shot_behavior_prompt_carries_aimed_identity_must(tmp_path: Path) -> None:
    """The compiles-but-wrong builder had no slot. A constructor of Error
    with an identity Then puts MUST on the prompt; Display::fmt with the
    same suite (coverage would have matched the leaf `fmt`) does not.
    Empty behaviour_must is the byte-identical control in
    test_behavior_fix_prompt_exact."""
    from pipeline_core.domain.behaviour import (
        BehaviourSpec,
        BehaviourSubKind,
        FenceDirective,
        ObservableOutcome,
    )

    pin = BehaviourSpec(
        behaviour_id="pin",
        setup="",
        when='err.is::<&str>()',
        then=ObservableOutcome.type_identity("&str", True),
        subkind=BehaviourSubKind.OUTCOME,
        fence=FenceDirective.RUN,
        exercises=frozenset({
            "src/error.rs::construct_from_adhoc",
            "src/wrapper.rs::fmt",
        }),
        source_ref="pin",
    )

    class _LocSplicer:
        def locate_fn(self, text: str, name: str):
            hits = locate_all_fns(text, name)
            return hits[0] if hits else None

        def splice_body_scoped(self, *a, **k):
            return SpliceOutcome(True, "ok", 1)

    def _run(rel: str, src: str, fn: str, container: str, sig: str) -> str:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
        model = _Model(f'<FN name="{fn}" block="any">\nok()\n</FN>')
        recs = [{"file": rel, "fn_name": fn, "container": container,
                 "sig_text": sig}]
        one_shot_fill(
            {(rel, fn): {"t"}}, kind="behavior", behavior_srcs={},
            session=_session(tmp_path), model=model, splicer=_LocSplicer(),
            log=lambda _m: None, thinking_directive="T",
            err_cap=500, src_snip_cap=4000, output="t FAILED",
            behaviour_specs=(pin,), fn_records=recs)
        assert model.prompts, fn
        return model.prompts[0]

    with using_driver(catalog_driver()):
        ctor = _run(
            "src/error.rs",
            "impl Error {\n"
            "    fn construct_from_adhoc<M>(error: M) -> Self { wrap() }\n"
            "}\n",
            "construct_from_adhoc", "impl Error",
            "fn construct_from_adhoc<M>(error: M) -> Self")
    assert "MUST:" in ctor
    assert "`is::<&str>()`" in ctor
    assert "never a wrapper's" in ctor

    fmt = _run(
        "src/wrapper.rs",
        "impl Display for MessageError<M> {\n"
        "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { wrap() }\n"
        "}\n",
        "fmt", "impl Display for MessageError < M >",
        "fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result")
    assert "MUST:" not in fmt
    assert "is::<" not in fmt


# --- splice-unit escalation on identical-error stall ---------------


class _StuckChecker:
    """Always 1 error on `file_rel` — every unit floor-stalls without progress."""

    def __init__(self, file_rel: str, fn: str) -> None:
        self.file_rel, self.fn = file_rel, fn

    def probe(self, cmd, cwd, env, timeout) -> CompileProbe:
        return CompileProbe(rc=101, rendered="E0308 stuck",
                            frontier={(self.file_rel, self.fn): {"t"}},
                            errors_by_file={self.file_rel: 1})


def _foo_block() -> ImplBlock:
    return ImplBlock(file="src/lib.rs", container="impl Foo",
                     members=(FnRecord(file="src/lib.rs", fn_name="foo",
                                       container="impl Foo"),))


def test_per_fn_stall_escalates_to_impl_then_whole_file(tmp_path: Path) -> None:
    """fn unit stalls on identical errors → retried at the enclosing impl
    scope; the impl stalls too → ONE whole-file rewrite."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("impl Foo { fn foo(){ unimplemented!() } }\n")
    model = _Model()
    whole_calls: list[str] = []
    d = _deps(_Frontiers(compile_fr={("src/lib.rs", "foo"): {"t"}}),
              model=model, checker=_StuckChecker("src/lib.rs", "foo"),
              enumerate_fns=lambda: [{"file": "src/lib.rs", "fn_name": "foo",
                                      "container": "impl Foo"}],
              impl_blocks=[_foo_block()],
              whole=lambda o, h, r: (whole_calls.append(o) or FixReport.changed(
                  summary="w", files_changed_str="x", error_preview="")))
    PerFnFix(_session(tmp_path), d).attempt("E0308 stuck", [])
    assert whole_calls == ["E0308 stuck"]
    # The impl rung actually ran: an impl prompt renders the block source.
    assert any("BLK" in p for p in model.prompts)


def test_per_impl_stall_escalates_to_whole_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("impl Foo { fn foo(){ unimplemented!() } }\n")
    whole_calls: list[str] = []
    d = _deps(_Frontiers(compile_fr={("src/lib.rs", "foo"): {"t"}}),
              checker=_StuckChecker("src/lib.rs", "foo"),
              enumerate_fns=lambda: [{"file": "src/lib.rs", "fn_name": "foo",
                                      "container": "impl Foo"}],
              impl_blocks=[_foo_block()],
              whole=lambda o, h, r: (whole_calls.append(o) or FixReport.changed(
                  summary="w", files_changed_str="x", error_preview="")))
    PerImplFix(_session(tmp_path), d).attempt("E0308 stuck", [])
    assert whole_calls == ["E0308 stuck"]


# --- StructuralFirst: the rung whose action is not "rewrite a body" ---------

def test_structural_repair_short_circuits_the_model(tmp_path: Path) -> None:
    """A duplicate definition is unreachable from every unit the strategies
    can pick, so the deletion runs before the model and the attempt ends
    there — the pre-fix loop spent three splices and four whole-file rewrites
    on one, across two full passes, and re-checked to the identical error."""
    asked: list[str] = []
    deps = _deps(_Frontiers(compile_fr={FnLocator("src/a.rs", "f"): {"<compile>"}}),
                 whole=lambda o, h, r: (asked.append("model"),
                                        FixReport.changed(summary="whole",
                                                          files_changed_str="x",
                                                          error_preview=""))[1])
    deps = replace(deps, structural_repair=lambda: FixReport.changed(
        summary="deleted duplicate ErrorImpl::debug",
        files_changed_str="src/error.rs", error_preview=""))

    strategy = select_strategy(FixStrategy.WHOLE_FILE, FillUnit.FN,
                               _session(tmp_path), deps)
    report = strategy.attempt("error[E0592]", [])

    assert "deleted duplicate" in (report.summary or "")
    assert asked == []


def test_no_structural_repair_defers_to_the_strategy(tmp_path: Path) -> None:
    """The control: with nothing structural to do, the attempt is exactly the
    strategy's own — every error the deletion rung does not own is unaffected."""
    deps = replace(
        _deps(_Frontiers()), structural_repair=lambda: None)
    strategy = select_strategy(FixStrategy.WHOLE_FILE, FillUnit.FN,
                               _session(tmp_path), deps)
    assert strategy.attempt("error[E0308]", []).summary == "whole"
