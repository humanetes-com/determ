"""The function-execution reading task0c takes after every coverage measurement.

`domain.fn_execution` holds the rule; this module gathers its inputs from the
workspace augmentation runs in — the extractor's fn and mod records over the
ORIGINAL source (augmentation is pre-strip, the read Hard Rule 11 permits), the
cluster's cfg sets plus the crate's default features, and the function counts
the coverage backend returned — and folds the reading into the record the store
persists per cluster.

Every way the reading can be unavailable returns an UNMEASURED record carrying
the reason, never an empty measured one: an empty measured record would read as
"every function executed" and let the stop fire on the line target alone, which
is the defect this criterion exists to close. The caller logs the reason.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import is_stub_record
from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.fn_execution import (
    CandidateFn,
    FnCoverageRecord,
    FunctionExecution,
    SiteIndex,
    advance_record,
    apply_proofs,
    candidate_records,
    flipped_cfg_set,
    function_execution,
    unmeasured_record,
)
from pipeline_core.domain.fn_reachability import (
    Verdict,
    WorldReading,
    public_surface,
    read_world,
)
from pipeline_core.domain.fn_reachability import (
    proofs as reachability_proofs,
)
from pipeline_core.domain.models import FnRecord, FnSite, ModRecord


@dataclass(frozen=True, slots=True)
class FnCriterionInputs:
    """The collaborators the reading needs. Each may be absent (a fakes-built
    deps, an extractor that is not on disk): the reading then says why it is
    unmeasured."""

    enumerate_fns: Callable[[Path], Sequence[Any] | None] | None
    enumerate_mods: Callable[[Path], Sequence[ModRecord] | None] | None
    # (crate root, cluster) -> the world's MIR and public index; absent or
    # failing, nothing is proved and the budget behaves as it always did.
    measure_reachability: Callable[[Path, Any], Any] | None = None


def _default_features(project_dir: Path) -> list[str]:
    try:
        return cargo_default_features(
            (project_dir / "Cargo.toml").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []


def _records(rows: Iterable[Any]) -> list[FnRecord]:
    out: list[FnRecord] = []
    for r in rows:
        if isinstance(r, FnRecord):
            out.append(r)
        elif isinstance(r, Mapping):
            out.append(FnRecord.from_dict(dict(r)))
    return out


@dataclass(frozen=True, slots=True)
class _Gathered:
    records: list[FnRecord]
    candidates: list[CandidateFn]
    file_texts: dict[str, str]


def _gather(project_dir: Path, cluster: Any,
            inputs: FnCriterionInputs) -> _Gathered | str:
    """The extractor's records, the candidates this cluster compiles and the
    file texts, or the reason there are none."""
    if not callable(inputs.enumerate_fns):
        return "no fn enumerator"
    rows = inputs.enumerate_fns(project_dir)
    if rows is None:
        return "the extractor returned no fn records"
    records = [r for r in _records(rows) if r.fn_name]
    if not records:
        return "the extractor returned no fn records"
    mods: Sequence[ModRecord] | None = None
    if callable(inputs.enumerate_mods):
        mods = inputs.enumerate_mods(project_dir / "src")
    file_texts: dict[str, str] = {}
    for rel in {r.file for r in records}:
        try:
            file_texts[rel] = (project_dir / rel).read_text(encoding="utf-8")
        except OSError:
            continue
    cfg_true = list(getattr(cluster, "cfg_true", None) or [])
    cfg_false = list(getattr(cluster, "cfg_false", None) or [])
    defaults = _default_features(project_dir)
    cs = CfgSet.of(cfg_true, cfg_false, defaults)
    flipped = flipped_cfg_set(cfg_true, cfg_false, defaults)
    candidates = candidate_records(
        records, cs=cs, flipped=flipped, mod_records=mods,
        file_texts=file_texts,
        hollow=lambda rec: is_stub_record(rec.to_dict()))
    if not candidates:
        return "no src/ fn is compiled under this cluster"
    return _Gathered(records, candidates, file_texts)


def read_verdicts(project_dir: Path, cluster: Any,
                  inputs: FnCriterionInputs) -> tuple[dict[FnSite, Verdict], str]:
    """The oracle's verdict per candidate function of this cluster, or the
    reason there is none. `Verdict.proof` is set for the unreachable ones and
    `Verdict.kept` says which fact keeps each of the others testable."""
    if not callable(inputs.measure_reachability):
        return {}, "no reachability reader"
    got = _gather(project_dir, cluster, inputs)
    if isinstance(got, str):
        return {}, got
    world = inputs.measure_reachability(project_dir, cluster)
    if not isinstance(world, WorldReading):
        return {}, "the reachability reader returned no reading"
    if not world.measured:
        return {}, world.reason or "the compiler output was empty"
    surface = public_surface(world.rustdoc)
    if surface is None:
        return {}, "the rustdoc JSON was not understood"
    verdicts = read_world([c.record for c in got.candidates], mir_text=world.mir,
                          surface=surface, file_texts=got.file_texts)
    if not verdicts:
        return {}, "the MIR dump held no item"
    return verdicts, ""


def read_by_construction(project_dir: Path, cluster: Any,
                         inputs: FnCriterionInputs) -> tuple[dict[FnSite, str], str]:
    """`(site -> proof, reason)` for the functions of this cluster no test can
    execute (`domain.fn_reachability`), taken once at cluster entry. `reason`
    is non-empty when no reading was taken; nothing is proved then."""
    verdicts, reason = read_verdicts(project_dir, cluster, inputs)
    return reachability_proofs(verdicts), reason


def read_function_execution(
    project_dir: Path,
    cluster: Any,
    functions: Mapping[tuple[str, int], int] | None,
    inputs: FnCriterionInputs,
    proofs: Mapping[FnSite, str] | None = None,
) -> FunctionExecution | str:
    """The reading, or the reason there is none.

    `project_dir` is the crate root the coverage run measured, so the
    extractor's records and the backend's counts are both keyed `src/<file>`
    — a record rooted at `src/` would join to nothing (an empty join is a
    rooting defect, not a clean reading, and is reported as one).

    `proofs` (from :func:`read_by_construction`) moves the unexecuted
    functions a proof names out of the set the tests are asked about."""
    if functions is None:
        return "the coverage backend reported no function records"
    got = _gather(project_dir, cluster, inputs)
    if isinstance(got, str):
        return got
    reading = function_execution(
        got.candidates, SiteIndex(got.records, got.file_texts), functions,
        got.file_texts)
    if not reading.executed and not reading.unexecuted:
        return (f"the {len(functions)} function record(s) joined to none of "
                f"the {len(got.candidates)} candidate fn(s) — a rooting defect, "
                "not a reading")
    return apply_proofs(reading, proofs or {})


def fold_reading(prior: FnCoverageRecord | None,
                 reading: FunctionExecution | str,
                 *, attempt_budget: int,
                 inherited: Mapping[FnSite, int] | None = None) -> FnCoverageRecord:
    """The record after this measurement.

    An unavailable reading measures nothing (see the module docstring) but
    does not forget the attempts a prior reading recorded: a transient
    failure of one coverage run must not hand every function a fresh
    budget."""
    if isinstance(reading, str):
        if prior is None:
            return unmeasured_record(reading)
        return replace(prior, measured=False, reason=reading)
    return advance_record(prior, reading, attempt_budget=attempt_budget,
                          inherited=inherited)
