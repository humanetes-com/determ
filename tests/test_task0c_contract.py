"""The test contract at task0c's generation and admission sites.

Driven through `_run_phase_c` with a fakes-built `Task0cDeps` (the established
pattern of `test_task0c_fix_loops.py`): cargo always compiles and passes, the
model's replies are scripted, and every log line / event / revert is recorded.
What is asserted is the plan's claim, not the code's output:

- the generator prompt lists the file's unpinned harvested behaviours and the
  contract, and no longer says "simply exercising the code path is acceptable";
- a contains-only block (the live `test_error_impl_debug_fmt` shape) is refused
  by contract, re-prompted ONCE with the reason and the list, and on the second
  refusal reverted under WEAK_ASSERTION;
- an exact-equality block (the live `test_construct_from_display` shape) is
  admitted and its pinned ids are emitted;
- a block reading a private field is refused with the path named, and the second
  refusal stops under NEEDS_INTERNALS;
- negative control: with the behaviour target at 0 the contains-only block is
  admitted as before, while the private read is still refused;
- the behaviour pass targets the fns the listed behaviours exercise and reports
  whether a block was admitted; the coverage reader counts the pin on disk.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pipeline_core.application.tasks.task0c as task0c_mod
from pipeline_core.application.tasks.task0c import (
    Task0cDeps,
    Task0cFileHistory,
    _PhaseCCtx,
    _run_phase_c,
    task0c_behaviour_coverage,
    task0c_behaviour_pass,
)
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    classified,
)
from pipeline_core.domain.coverage import PerFileCoverage
from pipeline_core.domain.generation import is_pipeline_gen_test_name
from pipeline_core.domain.rust_scan import count_test_attrs

_MICROCRATE = Path(__file__).parent / "fixtures" / "microcrate" / "src"

CONTAINS_ONLY = """#[cfg(test)]
mod gen_tests_1 {
    use super::*;

    #[test]
    fn test_error_impl_debug_fmt() {
        let err = checked_div(1, 0).unwrap_err();
        assert!(format!("{:#?}", err).contains("DivByZero"));
    }
}
"""

EXACT = """#[cfg(test)]
mod gen_tests_1 {
    use super::*;

    #[test]
    fn test_construct_from_display() {
        let err = checked_div(1, 0).unwrap_err();
        assert_eq!(err.to_string(), "division by zero");
    }
}
"""

PRIVATE_FIELD = """#[cfg(test)]
mod gen_tests_1 {
    use super::*;

    #[test]
    fn test_value_field() {
        let c = Counter::new(4);
        assert_eq!(c.value, 4);
    }
}
"""


def _display_spec(exercises: frozenset[str] = frozenset({"checked_div"})) -> BehaviourSpec:
    return BehaviourSpec(
        "display::div_by_zero", "", "MathError::DivByZero.to_string()",
        then=ObservableOutcome.display("division by zero"), exercises=exercises,
        subkind=BehaviourSubKind.OUTCOME)


class _Harness:
    # `capture_split` is the BEHAVIOUR_CAPTURE_SPLIT knob. It defaults OFF
    # here so the contract tests below keep exercising the guess route on a
    # spec that IS listed; the split's own effect (a capture-owned class is
    # never listed and never required) is pinned by the two tests at the end.
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 src_name: str, replies: list[str], target: float, specs: tuple[Any, ...],
                 capture_split: bool = False) -> None:
        self.ws = tmp_path / "ws"
        (self.ws / "src").mkdir(parents=True)
        shutil.copy(_MICROCRATE / src_name, self.ws / "src" / src_name)
        self.rel = f"src/{src_name}"
        self.abs = self.ws / self.rel
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.reverts: list[tuple[Path, Path]] = []
        self.logs: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.suite = SimpleNamespace(specs=specs)
        self.target = target
        self.capture_split = capture_split
        monkeypatch.setattr(task0c_mod, "log", self.logs.append)
        monkeypatch.setattr(task0c_mod, "emit",
                            lambda event, **f: self.events.append((event, f)))
        self.deps = self._deps()

    def ask(self, prompt: str, **_kw: Any) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""

    def revert(self, abs_path: Path, base_text: str, sib: Path) -> None:
        self.reverts.append((abs_path, sib))
        abs_path.write_text(base_text, encoding="utf-8")
        if sib.exists():
            sib.unlink()

    def _deps(self) -> Task0cDeps:
        stub: dict[str, Any] = {f: (lambda *a, **k: None) for f in Task0cDeps.__dataclass_fields__}
        stub.update(
            BUILD_TIMEOUT=30, COVERAGE_TARGET=100.0, BEHAVIOUR_COVERAGE_TARGET=self.target,
            BEHAVIOUR_CAPTURE_SPLIT=self.capture_split,
            TASK0C_PROMPT_CHAR_CAP=400_000, FIX_ERR_CAP=3000, SIBLING_CAP=3000,
            SRC_SNIP_CAP=3000, WORKING_CAP=3000, TASK0C_THINKING_DIRECTIVE="",
            TASK0C_FIX_THINKING_DIRECTIVE="", TASK0C_MODEL="m",
            load_behaviour_suite=lambda pd, c: self.suite,
            guard=lambda *a, **k: None,
            get_current_cluster=lambda: None,
            cargo_env=lambda: {},
            ask=self.ask,
            extract_test_mod=lambda r: r if "#[test]" in (r or "") else "",
            block_is_prose=lambda b: False,
            has_nonempty_test_body=lambda b: True,
            flags_unreachable=lambda t: False,
            strip_trailing_file_tag=lambda b: b,
            inject_allow_before_gen_mod=lambda b, a: b,
            inject_allow_before_any_mod=lambda b, a: b,
            inject_gen_prelude_imports=lambda b: b,
            count_test_attrs=count_test_attrs,
            gen_tests_path=lambda p, n: p.with_name(f"{p.stem}.gen_tests_{n}.rs"),
            build_gen_tests_decl=lambda *a, **k: '\n#[cfg(test)]\n#[path = "gen"]\nmod gen_tests_1;\n',
            cluster_gate_for=lambda *a, **k: "",
            extract_gen_tests_body=lambda b: b,
            revert_gen_tests_pass=self.revert,
            subprocess=SimpleNamespace(
                run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")),
            error_signatures_from_output=lambda o: frozenset(),
            quick_file_coverage=lambda pd, m, rel: (9, 9),
            type_signals_write=lambda *a, **k: None,
            check_stop=lambda ctx: None,
            describe_test_environment=lambda: "",
            load_file_constraints=lambda pd: {},
            next_gen_tests_n=lambda p: 1,
            # as the real reader: leftover src siblings plus tests/{stem}_gen_N.rs
            read_all_gen_tests=lambda p: "\n".join(
                f.read_text(encoding="utf-8")
                for f in sorted(
                    list(p.parent.glob(f"{p.stem}.gen_tests_*.rs"))
                    + list((p.parent.parent / "tests").glob(f"{p.stem}_gen_*.rs"))
                    if (p.parent.parent / "tests").is_dir() else
                    list(p.parent.glob(f"{p.stem}.gen_tests_*.rs")))),
            strip_gen_tests_decls=lambda t: t,
            extract_uncovered_functions=lambda text, lines, max_fns=8: "pub fn target() {}",
            extract_public_symbols=lambda t: "pub fn checked_div",
            select_relevant_siblings=lambda *a, **k: [],
            condense_rust_to_signatures=lambda t, *a, **k: t,
            nightly_rules_for_cluster=lambda: "",
            is_test_file=lambda p, root: False,
            is_structural_line=lambda s: False,
            is_gen_tests_file=lambda name: is_pipeline_gen_test_name(name),
            derive_gentest_prelude=lambda pd: ("", ""),
            prelude_import_names=lambda b: "",
            test_augmentor_factory=lambda: SimpleNamespace(
                test_harness_compiles=lambda p, m: (True, "")),
        )
        return Task0cDeps(**stub)

    def phase_c(self, *, uncovered_line: int = 46, covered: int = 3) -> Any:
        files_data = [PerFileCoverage(
            abs_path=str(self.abs), path=list(self.abs.parts),
            traces=[{"line": uncovered_line, "stats": {"Line": 0}}],
            covered=covered, coverable=10)]
        return _run_phase_c(
            project_dir=self.ws, module_name="microcrate", module_dir=self.ws,
            project_context="", coverage_strategy="", attempt=1, coverage=30.0,
            covered_lines=covered, files_data=files_data, hist=Task0cFileHistory(),
            ctx=_PhaseCCtx(deps=self.deps, current_cluster=None, cluster_label="stable",
                           split_n=1, allow_line="", prelude_block="", prelude_names="",
                           file_stall_limit=5, retro_deep_at_stall=1, stall_context=False))

    def refusals(self) -> list[dict[str, Any]]:
        return [f["data"] for e, f in self.events if e == "task0c.gen_tests_contract_refused"]

    def pinned_events(self) -> list[list[str]]:
        return [f["data"]["ids"] for e, f in self.events if e == "task0c.behaviours_pinned"]


def test_generator_prompt_lists_the_behaviours_and_the_contract(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[], target=60.0,
                 specs=(_display_spec(),))
    h.phase_c()
    assert len(h.prompts) == 2                       # full prompt, then the medium retry
    for prompt in h.prompts:
        assert "Harvested behaviours of `src/lib.rs`" in prompt
        assert 'then: "division by zero"' in prompt
        assert "Test contract" in prompt and "PUBLIC API ONLY" in prompt
        assert "INTEGRATION TEST RULES" in prompt
        assert "these tests live in `tests/`" in prompt
        assert "INTERNAL MODULE RULES" not in prompt
        assert "Do NOT write `use super::*;`" in prompt
        assert "simply exercising the code path is acceptable" not in prompt
    # the list sits after fn_bodies, before the public API section
    full = h.prompts[0]
    assert full.index("pub fn target() {}") < full.index("Harvested behaviours") < full.index("Public API of")


def test_contains_only_block_is_refused_reprompted_once_then_stopped_weak(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[CONTAINS_ONLY, CONTAINS_ONLY],
                 target=60.0, specs=(_display_spec(),))
    outcome = h.phase_c()
    assert len(h.prompts) == 2                        # generation + ONE restatement pass
    fix_prompt = h.prompts[1]
    assert "REFUSED by the test contract" in fix_prompt
    assert "pins no listed behaviour" in fix_prompt and "contains(" in fix_prompt
    assert 'then: "division by zero"' in fix_prompt   # the list again, for the pin case
    refusals = h.refusals()
    assert [r["stop"] for r in refusals] == ["", "WEAK_ASSERTION"]
    assert all(r["pinned"] == [] and r["private"] == [] for r in refusals)
    assert any("REFUSED by contract: pins no listed behaviour" in m for m in h.logs)
    assert any(m.strip().startswith("[microcrate] WEAK_ASSERTION:") for m in h.logs)
    assert len(h.reverts) == 1 and not h.reverts[0][1].exists()
    assert h.pinned_events() == [] and outcome.blocks_admitted == 0


def test_exact_equality_block_is_admitted_and_its_pins_emitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=60.0,
                 specs=(_display_spec(),))
    outcome = h.phase_c()
    assert len(h.prompts) == 1 and h.refusals() == [] and h.reverts == []
    assert h.pinned_events() == [["display::div_by_zero"]]
    assert any(e == "task0c.gen_tests_appended" for e, _ in h.events)
    assert outcome.blocks_admitted == 1
    integ = (h.ws / "tests" / "lib_gen_1.rs").read_text(encoding="utf-8")
    assert "fn test_construct_from_display" in integ
    assert "use microcrate::*;" in integ
    assert "use super::*;" not in integ
    assert "mod gen_tests_1" not in integ
    assert not (h.ws / "src" / "lib.gen_tests_1.rs").exists()
    assert any("pins display::div_by_zero" in m for m in h.logs)


def test_private_field_block_is_refused_with_the_path_then_stopped_needs_internals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="counter.rs",
                 replies=[PRIVATE_FIELD, PRIVATE_FIELD], target=60.0, specs=())
    h.phase_c(uncovered_line=14)
    refusals = h.refusals()
    assert [r["reason"] for r in refusals] == ["names private items: Counter.value"] * 2
    assert [r["private"] for r in refusals] == [["Counter.value"]] * 2
    assert refusals[1]["stop"] == "NEEDS_INTERNALS"
    assert any(m.strip().startswith("[microcrate] NEEDS_INTERNALS:") for m in h.logs)
    assert len(h.reverts) == 1


def test_negative_control_target_zero_admits_weak_but_still_refuses_private(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weak = _Harness(tmp_path / "a", monkeypatch, src_name="lib.rs", replies=[CONTAINS_ONLY],
                    target=0.0, specs=(_display_spec(),))
    assert weak.phase_c().blocks_admitted == 1
    assert weak.refusals() == [] and weak.pinned_events() == [[]]
    private = _Harness(tmp_path / "b", monkeypatch, src_name="counter.rs",
                       replies=[PRIVATE_FIELD, PRIVATE_FIELD], target=0.0, specs=())
    assert private.phase_c(uncovered_line=14).blocks_admitted == 0
    assert private.refusals()[-1]["stop"] == "NEEDS_INTERNALS"


def test_behaviour_pass_targets_the_exercised_fn_and_the_reader_counts_the_pin(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs", replies=[EXACT], target=60.0,
                 specs=(_display_spec(),))
    assert task0c_behaviour_coverage(h.ws, "microcrate", h.ws, h.deps) == (0, 1)
    assert task0c_behaviour_pass(h.ws, "microcrate", h.ws, h.deps) is True
    assert len(h.prompts) == 1 and "Behaviour pass:" in h.prompts[0]
    assert any("behaviour pass: src/lib.rs — 1 unpinned behaviour(s), 1 fn(s) targeted" in m
               for m in h.logs)
    # the targeted "uncovered" line is checked_div's signature line
    lib_lines = (_MICROCRATE / "lib.rs").read_text(encoding="utf-8").splitlines()
    sig = next(i for i, ln in enumerate(lib_lines, 1) if ln.startswith("pub fn checked_div"))
    assert any(e == "task0c.file" for e, _ in h.events)
    assert any("1 actionable uncovered lines" in m for m in h.logs)
    assert h.prompts[0].count("pub fn target() {}") == 1 and sig > 0
    assert task0c_behaviour_coverage(h.ws, "microcrate", h.ws, h.deps) == (1, 1)
    # a second pass has nothing left to target: no model call, no admission
    assert task0c_behaviour_pass(h.ws, "microcrate", h.ws, h.deps) is False
    assert len(h.prompts) == 1


def test_behaviour_pass_is_off_at_target_zero_and_on_a_broken_harness(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    off = _Harness(tmp_path / "a", monkeypatch, src_name="lib.rs", replies=[EXACT], target=0.0,
                   specs=(_display_spec(),))
    assert task0c_behaviour_pass(off.ws, "microcrate", off.ws, off.deps) is False
    assert off.prompts == []
    broken = _Harness(tmp_path / "b", monkeypatch, src_name="lib.rs", replies=[EXACT],
                      target=60.0, specs=(_display_spec(),))
    broken.deps = Task0cDeps(**{**{f: getattr(broken.deps, f) for f in Task0cDeps.__dataclass_fields__},
                               "test_augmentor_factory": lambda: SimpleNamespace(
                                   test_harness_compiles=lambda p, m: (False, "error[E0308]"))})
    assert task0c_behaviour_pass(broken.ws, "microcrate", broken.ws, broken.deps) is False
    assert broken.prompts == [] and any("harness does not compile" in m for m in broken.logs)


def _option_spec() -> BehaviourSpec:
    """A pinnable spec the split KEEPS: `Some(...)` at the observable is a
    class the model can derive from the source, not a value it must predict.
    Classified by the real classifier so the fixture's class is computed, not
    asserted by hand."""
    return classified(BehaviourSpec(
        "option::downcast_ref", "",
        'format!("{:?}", checked_div(1, 0).unwrap_err().downcast_ref::<MathError>())',
        then=ObservableOutcome.display("Some(DivByZero)"),
        exercises=frozenset({"checked_div"}), subkind=BehaviourSubKind.OUTCOME))


def test_capture_owned_behaviours_are_not_listed_and_no_pin_is_required(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the split on, an EXACT_FORMAT spec — the class almost every
    pinnable harvested spec falls in — leaves `_file_specs` empty, so the
    prompt lists nothing, `require_pin` keys itself off, and the contains-only
    block that the pin contract refuses is admitted instead of costing a
    restatement pass and a revert. The negative control is the same spec with
    the knob off."""
    on = _Harness(tmp_path / "on", monkeypatch, src_name="lib.rs", replies=[CONTAINS_ONLY],
                  target=60.0, specs=(_display_spec(),), capture_split=True)
    assert on.phase_c().blocks_admitted == 1
    assert len(on.prompts) == 1
    assert "Harvested behaviours of `src/lib.rs`" not in on.prompts[0]
    assert 'then: "division by zero"' not in on.prompts[0]
    assert on.refusals() == [] and on.reverts == []
    assert not any("harvested behaviour(s) not " in m for m in on.logs)
    assert any("1 capture-owned behaviour(s) not listed" in m for m in on.logs)

    off = _Harness(tmp_path / "off", monkeypatch, src_name="lib.rs",
                   replies=[CONTAINS_ONLY, CONTAINS_ONLY], target=60.0,
                   specs=(_display_spec(),), capture_split=False)
    assert off.phase_c().blocks_admitted == 0
    assert "Harvested behaviours of `src/lib.rs`" in off.prompts[0]
    assert [r["stop"] for r in off.refusals()] == ["", "WEAK_ASSERTION"]
    assert any("1 harvested behaviour(s) not yet pinned (pin required)" in m for m in off.logs)
    assert not any("capture-owned behaviour(s) not listed" in m for m in off.logs)


def test_a_non_capture_owned_behaviour_survives_the_split_and_is_still_required(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The split is by class, not a blanket off switch: the guess route keeps
    the classes it can answer, so the spec is still listed and its block is
    still held to the pin."""
    h = _Harness(tmp_path, monkeypatch, src_name="lib.rs",
                 replies=[CONTAINS_ONLY, CONTAINS_ONLY], target=60.0,
                 specs=(_option_spec(),), capture_split=True)
    assert h.phase_c().blocks_admitted == 0
    assert "Harvested behaviours of `src/lib.rs`" in h.prompts[0]
    assert 'then: "Some(DivByZero)"' in h.prompts[0]
    assert [r["stop"] for r in h.refusals()] == ["", "WEAK_ASSERTION"]
    assert any("1 harvested behaviour(s) not yet pinned (pin required)" in m for m in h.logs)


def test_the_coverage_denominator_excludes_what_the_split_forbids_pinning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A measurement whose meaning changes has to be told. With the split on,
    this task is neither shown a capture-owned spec nor held to it, so counting
    it in the denominator makes the ratio unreachable BY CONSTRUCTION — and
    `_behaviour_rounds` then spends a round (and a `test_harness_compiles`
    cargo build) per module per cluster discovering that, before logging
    `stopping below target` as if something had gone wrong."""
    on = _Harness(tmp_path / "on", monkeypatch, src_name="lib.rs", replies=[],
                  target=60.0, specs=(_display_spec(),), capture_split=True)
    assert task0c_behaviour_coverage(on.ws, "microcrate", on.ws, on.deps) == (0, 0)
    assert task0c_behaviour_pass(on.ws, "microcrate", on.ws, on.deps) is False
    assert on.prompts == []
    assert any("capture-owned and this task is not asked to pin them" in m
               for m in on.logs)

    # NEGATIVE CONTROL: the same spec with the split off is counted, and the
    # pass runs — the pre-split reading, unchanged.
    off = _Harness(tmp_path / "off", monkeypatch, src_name="lib.rs",
                   replies=[EXACT], target=60.0, specs=(_display_spec(),),
                   capture_split=False)
    assert task0c_behaviour_coverage(off.ws, "microcrate", off.ws, off.deps) == (0, 1)
    assert task0c_behaviour_pass(off.ws, "microcrate", off.ws, off.deps) is True
