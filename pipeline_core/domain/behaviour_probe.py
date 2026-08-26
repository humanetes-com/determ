"""Behaviour probes — pure domain half of the synthesis tier (F10 tier 3).

A PROBE is a model-authored program that USES the crate's public API and
PRINTS an observable; the harness runs it against the ORIGINAL crate in the
pre-strip window and records stdout as the behaviour's Then. The model never
guesses the Then — it only chooses what to observe — so a probe that compiles
and runs is a golden reading, and one that panics or fails to build is
discarded (self-filtering, never a hallucinated oracle).

This module owns what needs no cargo: the result record, the reply parser for
the `<BEHAVIOUR unit="…">` blocks the synthesis prompt asks for, stdout
normalisation, the target selection (public fns no harvested spec names) and
the spec constructor. The adapter (`adapters/behaviour_probe.py`) writes the
example file and runs it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
    ObservableOutcome,
    classified,
)
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_idents import impl_self_type
from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.text import strip_think_blocks

_OPEN_TAG = "<BEHAVIOUR"
_CLOSE_TAG = "</BEHAVIOUR>"
_FENCE = "```"
# Consumers of the public API, never part of it — a probe target is a crate fn.
_AUX_DIRS = frozenset({"tests", "examples", "benches"})
PROBE_BATCH_SIZE = 6


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe program's run against the original crate."""

    ok: bool
    stdout: str
    stderr: str


def normalize_stdout(text: str) -> str:
    """Trailing whitespace stripped per line, trailing blank lines dropped —
    the form a Then is stored in and compared against."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _strip_code_fence(code: str) -> str:
    """The body of a ```rust … ``` fence when the block is wrapped in one,
    else the text unchanged. Models fence the program despite the tag."""
    s = code.strip()
    if not s.startswith(_FENCE):
        return s
    nl = s.find("\n")
    if nl < 0:
        return ""
    body = s[nl + 1:]
    end = body.rfind(_FENCE)
    if end >= 0:
        body = body[:end]
    return body.strip()


def _unit_attr(tag: str) -> str:
    """The `unit="…"` (or single-quoted) value of an opening tag, or ""."""
    at = tag.find("unit")
    if at < 0:
        return ""
    eq = tag.find("=", at)
    if eq < 0:
        return ""
    i = eq + 1
    while i < len(tag) and tag[i].isspace():
        i += 1
    if i >= len(tag) or tag[i] not in "\"'":
        return ""
    q = tag[i]
    end = tag.find(q, i + 1)
    return "" if end < 0 else tag[i + 1:end].strip()


def parse_behaviour_programs(reply: str) -> list[tuple[str, str]]:
    """`(unit_id, program)` for every `<BEHAVIOUR unit="…">…</BEHAVIOUR>` block
    in a synthesis reply, in order; think blocks and a wrapping code fence
    are stripped; empty programs are dropped. A block with no unit attribute
    keeps an empty unit id (the caller labels it by batch)."""
    text = strip_think_blocks(reply)
    out: list[tuple[str, str]] = []
    i = 0
    while (at := text.find(_OPEN_TAG, i)) >= 0:
        gt = text.find(">", at)
        if gt < 0:
            break
        close = text.find(_CLOSE_TAG, gt + 1)
        if close < 0:
            break
        unit = _unit_attr(text[at:gt])
        program = _strip_code_fence(text[gt + 1:close])
        if program:
            out.append((unit, program))
        i = close + len(_CLOSE_TAG)
    return out


def probe_unit_label(rec: FnRecord) -> str:
    """`Type::fn` for a method, `fn` for a free fn — the id the synthesis
    prompt names and the probe spec carries."""
    self_ty = impl_self_type(rec.container) if rec.container else None
    return f"{self_ty}::{rec.fn_name}" if self_ty else rec.fn_name


def _hidden_module_path(mod_path: str) -> bool:
    """A `__`-prefixed module segment (`__private`) — the crate-author
    convention for a `#[doc(hidden)] pub mod` that macros expand into and
    no user calls. HEURISTIC: the mod record does not carry `#[doc(hidden)]`,
    so the name is the only signal available here."""
    return any(seg.startswith("__") for seg in mod_path.split("::") if seg)


def _cfg_excluded(rec: FnRecord, cfg_set: CfgSet | None) -> bool:
    """True when one of the fn's own `#[cfg]` predicates evaluates FALSE under
    the cluster (`Error::thiserror_provide` is `error_generic_member_access`,
    which the stable cluster lists as false); undecidable stays included."""
    if cfg_set is None:
        return False
    return any(cfg_set.evaluate(pred) is False for pred in rec.cfg_predicates)


def _is_public_crate_fn(
    rec: FnRecord,
    reachable_files: frozenset[str] | None,
    type_files: Mapping[str, str] | None,
    cfg_set: CfgSet | None,
) -> bool:
    if not (rec.is_pub and rec.is_exactly_pub) or rec.is_test:
        return False
    if set(Path(rec.file).parts) & _AUX_DIRS:
        return False
    if _hidden_module_path(rec.mod_path) or _cfg_excluded(rec, cfg_set):
        return False
    if reachable_files is None:
        return True
    self_ty = impl_self_type(rec.container) if rec.container.startswith("impl") else None
    if self_ty is not None:
        # A method is callable iff its SELF TYPE is public: the impl may sit
        # in a private module (`mod error;` holds `impl Error`) while the type
        # is declared at the reachable root.
        declared_in = (type_files or {}).get(self_ty)
        return declared_in is not None and declared_in in reachable_files
    return rec.file in reachable_files


def probe_targets(
    fn_records: Iterable[FnRecord],
    specs: Iterable[BehaviourSpec],
    *,
    max_targets: int,
    reachable_files: frozenset[str] | None = None,
    type_files: Mapping[str, str] | None = None,
    cfg_set: CfgSet | None = None,
) -> tuple[str, ...]:
    """The externally-callable fns NO harvested spec names in its given/when
    (whole-word on the bare fn name), as prompt labels, sorted, capped at
    `max_targets`. A fn named anywhere in the suite is already observed by
    something that ran against the original; the probe budget goes to the
    rest.

    `reachable_files` is `module_reachability.externally_reachable_files`
    over the same records — the files behind a PUBLIC chain of `mod`
    declarations — and `type_files` maps each `pub` type name to the file
    that declares it. A fn's own `pub` is not the authority: on the anyhow
    skeleton (2026-08-26, 132 records, 41 `pub` fns) the visibility flags
    alone offered 23 targets, and the first 12-slot batch was
    `Error::backtrace, Error::from_boxed, …, Mut::by_ref, Mut::cast,
    Mut::deref_mut, Mut::extend, Own::boxed, Own::by_mut, Own::by_ref` —
    `Own`/`Ref`/`Mut` are pub structs behind the private `mod ptr;`,
    `nightly.rs` sits behind a cfg-gated private `mod nightly;`, and
    `__private::format_err` is the macro support module. None of those can
    be named from an example crate, so every such probe fails to compile and
    the whole budget buys nothing. Nor is the impl's FILE the authority:
    anyhow declares `pub struct Error` in lib.rs and implements
    `impl Error { pub fn msg … }` in the private `mod error;`, so a
    file-only rule offered zero targets. A method counts when its self type
    is declared in a reachable file; a free fn when its own file is. None
    for `reachable_files` = no reachability known (every `pub` fn counts,
    the pre-fix behaviour). `cfg_set` is the cluster's cfg world: a fn whose
    own `#[cfg]` evaluates false there (`Error::thiserror_provide` on
    stable) cannot compile in that cluster's probe and is skipped."""
    if max_targets <= 0:
        return ()
    texts = [f"{s.given}\n{s.when}" for s in specs]
    labels: set[str] = set()
    for rec in fn_records:
        if not _is_public_crate_fn(rec, reachable_files, type_files, cfg_set):
            continue
        if any(contains_word(t, rec.fn_name) for t in texts):
            continue
        labels.add(probe_unit_label(rec))
    return tuple(sorted(labels))[:max_targets]


def batches(items: Sequence[str], size: int = PROBE_BATCH_SIZE) -> list[tuple[str, ...]]:
    """`items` in consecutive batches of at most `size` (one prompt each)."""
    size = max(1, size)
    return [tuple(items[i:i + size]) for i in range(0, len(items), size)]


def probe_behaviour_id(cfg_cluster: str, unit: str, n: int) -> str:
    return f"probe::{cfg_cluster}::{unit}::{n}"


def is_probe_spec(spec: BehaviourSpec) -> bool:
    return spec.behaviour_id.startswith("probe::")


def probe_spec(
    *,
    cfg_cluster: str,
    unit: str,
    n: int,
    program: str,
    stdout: str,
    default_cluster: str | None = None,
) -> BehaviourSpec:
    """An OUTCOME / NORMALIZED_STDOUT spec for a probe that ran clean against
    the original: `given` = the program (what the exercise writer re-runs),
    `when` = the unit it targets, Then = its normalised stdout."""
    spec = BehaviourSpec(
        behaviour_id=probe_behaviour_id(cfg_cluster, unit, n),
        given=program, when=unit,
        then=ObservableOutcome.stdout(normalize_stdout(stdout)),
        provenance=BehaviourProvenance.SYNTHESIZED,
        subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN,
        cfg_cluster=cfg_cluster, source_ref=f"probe::{unit}")
    return classified(spec, default_cluster=default_cluster or cfg_cluster)
