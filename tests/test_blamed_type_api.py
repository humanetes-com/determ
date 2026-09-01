"""A privacy/resolution compile error injects the blamed type's
crate-visible API into the retry (`domain/blamed_type_api.py`).

Recorded instance, run `358833-aa15d93c` (2026-09-01): four `E0616`s and one
`E0599` on the `src/fmt.rs::ErrorImpl` block, all reaching for
`ErrorImpl.backtrace` by field or by method-on-`dyn`; the one attempt that
saw all of `src/error.rs` immediately used the `pub(crate) unsafe fn
backtrace` accessor and compiled. The fixture is the recorded diagnostics
verbatim and the extractor's recorded `ErrorImpl` type record from the live
tree — not a hand-made shape."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.blamed_type_api import (
    blamed_type_api_section,
    blamed_type_names,
    with_blamed_type_api,
)
from pipeline_core.domain.models import TypeRecord

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_FX = json.loads((_FIXTURES / "blamed_type_errorimpl_358833.json")
                 .read_text(encoding="utf-8"))
_RECORDS = [TypeRecord.from_dict(d) for d in _FX["type_records"]]

# The run's diagnostics, verbatim from logs/pipeline_anyhow.log.
_E0616 = "error[E0616]: field `backtrace` of struct `ErrorImpl` is private"
_E0599_STD = ("error[E0599]: no method named `backtrace` found for reference "
              "`&dyn core::error::Error + Send + Sync` in the current scope")
_E0599_NOTYPE = "error[E0599]: `&Backtrace` is not an iterator"


def test_the_recorded_e0616_names_errorimpl_and_the_appendix_carries_backtrace() -> None:
    assert blamed_type_names(_E0616) == ("ErrorImpl",)
    section = blamed_type_api_section(_E0616, _RECORDS)
    assert "`ErrorImpl`" in section
    # The accessor the one compiling attempt used, with its visibility.
    assert "backtrace" in section
    assert "pub(crate)" in section
    out = with_blamed_type_api(_E0616, _RECORDS)
    assert out.startswith(_E0616) and "backtrace" in out[len(_E0616):]


def test_a_std_trait_object_blame_matches_no_crate_record() -> None:
    """The live E0599 blames `&dyn core::error::Error + Send + Sync` — a std
    path whose LEAF collides with the crate's own `Error`. It must inject
    nothing."""
    assert blamed_type_names(_E0599_STD) == ()
    assert with_blamed_type_api(_E0599_STD, _RECORDS) == _E0599_STD


def test_negative_controls_append_nothing() -> None:
    # No blamed-type shape at all (the run's other E0599).
    assert with_blamed_type_api(_E0599_NOTYPE, _RECORDS) == _E0599_NOTYPE
    # A blamed type the extractor has no record for.
    unknown = "error[E0616]: field `x` of struct `Elsewhere` is private"
    assert with_blamed_type_api(unknown, _RECORDS) == unknown
    # Other codes never trigger, even when they name a struct.
    e0277 = "error[E0277]: the trait bound `ErrorImpl: Sized` — struct `ErrorImpl`"
    assert blamed_type_names(e0277) == ()
    assert with_blamed_type_api("", _RECORDS) == ""


def test_generic_and_crate_qualified_blames_resolve_to_the_record() -> None:
    generic = ("error[E0599]: no method named `x` found for struct "
               "`crate::error::ErrorImpl<E>` in the current scope")
    assert blamed_type_names(generic) == ("ErrorImpl",)
    assert "backtrace" in blamed_type_api_section(generic, _RECORDS)


def test_the_retry_prompt_carries_the_blamed_types_inventory(
        monkeypatch, tmp_path) -> None:
    """Wiring: after an attempt fails on E0616, the NEXT attempt's prompt
    carries the blamed type's fn inventory in its error-feedback slot — the
    recorded attempt-2 prompts had only the diagnostic line, and the model
    guessed `E0599` next. Negative control in the same run: attempt 1's
    prompt (no error yet) carries no inventory."""
    import pipeline
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
    from tests.harness import (
        diag,
        install_cluster_scan,
        install_fake_extractor,
        install_scripted_cargo,
    )

    lib = ("pub struct Gauge { v: u32 }\n\n"
           "impl Gauge {\n"
           "    pub fn level(&self) -> u32 {\n"
           "        self.v\n"
           "    }\n"
           "}\n")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(lib)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    monkeypatch.setenv("PHASE_C_FILE_FIRST", "0")
    member = FnRecord(file="lib.rs", fn_name="level", container="impl Gauge",
                      sig_text="pub fn level (& self) -> u32")
    block = ImplBlock(file="lib.rs", container="impl Gauge",
                      members=(member,), refill_members=(member,))
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: [block])
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    gauge_rec = TypeRecord.from_dict({
        "file": "lib.rs", "kind": "struct", "name": "Gauge",
        "methods": [{"name": "reading", "sig": "fn reading (& self) -> u32",
                     "is_pub": True, "is_exactly_pub": False}]})
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[gauge_rec], dispatch_families=[])
    prompts: list[str] = []
    monkeypatch.setattr(
        pipeline, "_ask_raw",
        lambda p, *a, **k: (prompts.append(p) or
                            '<FN name="level" block="impl Gauge">self.v</FN>'))
    install_scripted_cargo(
        monkeypatch,
        check_diag=(101, "error[E0616]: field `v` of struct `Gauge` is private",
                    [diag("src/lib.rs", code="E0616",
                          message="field `v` of struct `Gauge` is private")]))
    pipeline._phase_c_fill_impl_blocks(
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={FnLocator("lib.rs", "level")},
        tdd_frontier_tests=FailureFrontier.build(
            {FnLocator("lib.rs", "level"): {"t_red"}},
            {FnLocator("lib.rs", "level"): {"t_red"}}),
    )
    assert len(prompts) >= 2, "the fix loop must retry at least once"
    assert "blames `Gauge`" not in prompts[0], (
        "attempt 1 has no error and no inventory")
    retry = prompts[1]
    assert "blames `Gauge`" in retry
    assert "pub(crate) fn reading (& self) -> u32" in retry
