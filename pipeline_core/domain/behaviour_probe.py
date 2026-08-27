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

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
    ObservableOutcome,
    OutcomeKind,
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


def printed_value(stdout: str) -> str:
    """What a `println!`-of-one-expression capture OBSERVED, exactly.

    NOT :func:`normalize_stdout`. That one strips trailing whitespace PER LINE
    and drops trailing blank lines, which is right for a free-form program's
    multi-line blob and wrong for a single rendering: the replay test asserts
    `format!(…) == <stored value>`, so any character the store drops that
    `format!` still produces makes the emitted test fail against the ORIGINAL
    crate. Measured 2026-08-27 on a pristine copy: a value of `"a \nb"` printed
    as `'a \nb\n'`, stored by `normalize_stdout` as `'a\nb'`, and the emitted
    `#[test]` failed on the very tree the value was captured from. `println!`
    appends exactly one newline, so removing exactly one is the inverse."""
    if stdout.endswith("\r\n"):
        return stdout[:-2]
    if stdout.endswith("\n"):
        return stdout[:-1]
    return stdout


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


def _attr(tag: str, key: str) -> str:
    """The `key="…"` (or single-quoted) value of an opening tag, or ""."""
    at = tag.find(key)
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


def _unit_attr(tag: str) -> str:
    """Back-compat alias: the `unit=` attribute of an opening tag."""
    return _attr(tag, "unit")


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


# --- the captured-behaviour shape (setup + ONE expression + a render kind) ---
#
# Measured 2026-08-27 on this model and this crate: the free-form `fn main()`
# the old prompt asked for produced 0 of 6 mechanically-assertable observables
# (3 recorded on disk, 3 re-run as the experiment's control), every one a
# multi-`println!` blob stored as NORMALIZED_STDOUT and classified OTHER — so
# `behaviours_pinned_by` (which joins on an exact `then.value`) could never
# match one, and t1.5's cap discarded them for ranking last. The constrained
# shape produced 7 of 7 across two variants. `when` is therefore ONE
# expression, and the harness renders BOTH roles from it: `println!` to
# capture, `assert_eq!` to replay.

_OBSERVE_TO_KIND: Mapping[str, OutcomeKind] = {
    "display": OutcomeKind.DISPLAY_STRING,
    "debug": OutcomeKind.DEBUG_STRING,
    "alt_debug": OutcomeKind.ALT_DEBUG_STRING,
}
_KIND_TO_FMT: Mapping[OutcomeKind, str] = {
    OutcomeKind.DISPLAY_STRING: "{}",
    OutcomeKind.DEBUG_STRING: "{:?}",
    OutcomeKind.ALT_DEBUG_STRING: "{:#?}",
}
# A `when` that COMPARES discards the value it was supposed to specify:
# variant B of the experiment produced `format!("{}", b) == "world"` whose
# captured Then is `true`, carrying nothing. Identity predicates are the one
# exception, and the same carve-out `weak_assertion_reason` already makes for
# generated tests (`behaviour_pinning.py`, `_IDENTITY_METHODS`).
_COMPARISON_TOKENS = ("==", "!=", "<=", ">=", ".contains(")
# `&&` / `||` are refused for the same reason a comparison is, one step further
# on: `a.is::<X>() && b.is::<Y>()` prints one boolean, and the capture would
# store it as the identity of X alone — a pin that says something the run never
# measured. A conjunction is two behaviours; ask for them separately.
_LOGIC_TOKENS = ("&&", "||")
_IDENTITY_CALLS = (".is::<", ".downcast_ref::<", ".downcast_mut::<", ".downcast::<")


@dataclass(frozen=True, slots=True)
class CapturedProbe:
    """One `<BEHAVIOUR>` block of a synthesis reply, in the constrained shape."""

    name: str
    given: str
    when: str
    observe: str


def identity_type_of(when: str) -> str | None:
    """The `T` of an identity predicate `x.is::<T>()` / `x.downcast_ref::<T>()`,
    else None. `TYPE_IDENTITY` stores T as its value — `DOWNCAST_OK` carries a
    bool and NOT which type, which is why it was retired on 2026-08-26."""
    for call in _IDENTITY_CALLS:
        at = when.find(call)
        if at < 0:
            continue
        start = at + len(call)
        depth = 1
        i = start
        while i < len(when):
            if when[i] == "<":
                depth += 1
            elif when[i] == ">":
                depth -= 1
                if depth == 0:
                    inner = when[start:i].strip()
                    return inner or None
            i += 1
    return None


def without_turbofish(when: str) -> str:
    """`when` with every `::<…>` span removed, angle brackets included.

    The bare `<` and `>` of a comparison and the `<` and `>` of a turbofish are
    the same character, which is why the first version of this check omitted
    them and admitted `v.len() < 3` and `n > 0` — both of which observe a
    boolean and discard the value, exactly what the check exists to refuse.
    Deleting the turbofish spans first makes the remaining angle brackets
    unambiguous."""
    out: list[str] = []
    i = 0
    while i < len(when):
        if when.startswith("::<", i):
            depth = 0
            j = i + 2
            while j < len(when):
                if when[j] == "<":
                    depth += 1
                elif when[j] == ">":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(when):        # unbalanced: keep the tail verbatim
                out.append(when[i:])
                break
            i = j + 1
            continue
        out.append(when[i])
        i += 1
    return "".join(out)


def value_bearing_reason(when: str) -> str | None:
    """Why this `when` may NOT be captured, or None when it may.

    A comparison observes `true`/`false` and throws the value away — the same
    weakness `weak_assertion_reason` refuses on the generation side, arriving
    on the capture side. Identity predicates are exempt: for them the boolean
    IS the observable, and the type is kept in the Then's value."""
    w = (when or "").strip()
    if not w:
        return "empty when"
    if "\n" in w:
        return "when spans multiple lines — it must be ONE expression"
    if "println!" in w or "print!" in w:
        return "when prints; the harness prints it"
    if "assert" in w:
        return "when asserts; the harness records, it does not check"
    bare = without_turbofish(w)
    for tok in _LOGIC_TOKENS:
        if tok in bare:
            return (f"when combines two observables ({tok}) — one printed "
                    "boolean cannot say which half held; write them separately")
    if identity_type_of(w) is not None:
        return None
    for tok in (*_COMPARISON_TOKENS, "<", ">"):
        if tok in bare:
            return (f"when compares ({tok}) — a boolean discards the value; "
                    "write the expression whose value is the observable")
    return None


def observe_kind(observe: str, when: str = "") -> OutcomeKind:
    """The Then kind this capture stores. An identity `when` is TYPE_IDENTITY
    whatever the model said it would observe — the shape of the expression is
    the authority, not the label."""
    if identity_type_of(when) is not None:
        return OutcomeKind.TYPE_IDENTITY
    return _OBSERVE_TO_KIND.get((observe or "").strip().lower(),
                                OutcomeKind.DISPLAY_STRING)


def render_probe_program(
    given: str, when: str, observe: str = "display", *, crate: str = "",
) -> str:
    """The CAPTURE role: a runnable `fn main()` that PRINTS the observable.

    `crate` puts `use <crate>::*;` above `fn main()`, and omitting it is not a
    detail. The three probes this shape replaced each opened with
    `use anyhow::{anyhow, Error};` OUTSIDE `fn main()` — the one line the
    constrained shape has nowhere to put, since `given` is spliced INSIDE the
    body. Measured 2026-08-27 against a pristine copy of the original: a
    `given` of `let err = anyhow!("boom");` with no import fails to build with
    `error: cannot find macro `anyhow` in this scope`, so the capture is lost
    and only a miss records it. The replay half has had this since it was
    written (`as_integration_test` inserts the same line); this makes the two
    roles symmetric. A `use` the model puts in `given` anyway still shadows the
    glob and still compiles.

    Also what `exercises_by_coverage` re-runs — before this existed it re-ran
    `spec.given` directly, so a `given` that stopped being a whole program
    would have made every probe's exercises run fail `rc != 0` and be dropped
    with a log line."""
    kind = observe_kind(observe, when)
    body = (given or "").rstrip()
    if kind is OutcomeKind.TYPE_IDENTITY:
        expr = f'println!("{{}}", {when.strip()});'
    else:
        expr = f'println!("{_KIND_TO_FMT[kind]}", {when.strip()});'
    head = f"#[allow(unused_imports)]\nuse {crate}::*;\n\n" if crate else ""
    return (head + "fn main() {\n" + (body + "\n" if body else "")
            + "    " + expr + "\n}\n")


def parse_behaviour_specs(reply: str) -> list[CapturedProbe]:
    """Every `<BEHAVIOUR name="…"><GIVEN>…</GIVEN><WHEN>…</WHEN>
    <OBSERVE>…</OBSERVE></BEHAVIOUR>` block, in order.

    A block without a `<WHEN>` is dropped: the expression IS the behaviour.
    The class is NOT read even when present — measured 0 of 3 correct when the
    model chose it against 4 of 4 when it was supplied, and `classified()`
    re-derives it from kind and text anyway."""
    text = strip_think_blocks(reply)
    out: list[CapturedProbe] = []
    i = 0
    while (at := text.find(_OPEN_TAG, i)) >= 0:
        gt = text.find(">", at)
        if gt < 0:
            break
        close = text.find(_CLOSE_TAG, gt + 1)
        if close < 0:
            break
        block = text[gt + 1:close]
        when = _tagged(block, "WHEN")
        if when:
            out.append(CapturedProbe(
                name=_attr(text[at:gt], "name"),
                given=_strip_code_fence(_tagged(block, "GIVEN")),
                when=_strip_code_fence(when).strip(),
                observe=_tagged(block, "OBSERVE").strip() or "display"))
        i = close + len(_CLOSE_TAG)
    return out


def _tagged(block: str, tag: str) -> str:
    """The body of `<TAG>…</TAG>` inside one behaviour block, or ""."""
    o, c = f"<{tag}>", f"</{tag}>"
    a = block.find(o)
    if a < 0:
        return ""
    b = block.find(c, a + len(o))
    return block[a + len(o):b] if b >= 0 else ""


# The environment variables that change what a probe OBSERVES rather than how
# it builds. `RUST_BACKTRACE` is the measured one: `err.backtrace()` renders
# `<disabled>` without it and a captured backtrace with it, identically on
# every run either way, so the run-twice guard cannot see the difference.
OBSERVABLE_ENV_KEYS = ("RUST_BACKTRACE", "RUST_LIB_BACKTRACE")


def perturbed_env(env: Mapping[str, str]) -> dict[str, str]:
    """The env flip that EXPOSES an environment-dependent observable.

    Recording the capture environment says what the value was seen under; it
    does not say whether the value depends on it, and nothing downstream reads
    the record. Running the emitted replay a second time under this flip does
    say: measured 2026-08-27 against a pristine copy, `err.backtrace()` renders
    `"disabled backtrace"` on every run at a fixed env — so the run-twice guard
    is blind to it — and renders a captured backtrace under `RUST_BACKTRACE=1`.
    A capture that changes verdict across this flip is not a specification of
    the crate and must not enter the oracle."""
    out: dict[str, str] = {}
    for key in OBSERVABLE_ENV_KEYS:
        current = (env.get(key) or "").strip().lower()
        out[key] = "0" if current in ("1", "full") else "1"
    return out


def capture_env_of(env: Mapping[str, str]) -> str:
    """The observable-affecting slice of a capture environment, sorted and
    joined — the value stored on the spec so a replay can tell whether it is
    being judged under the conditions the Then was observed in."""
    return ";".join(f"{k}={env[k]}" for k in OBSERVABLE_ENV_KEYS if k in env)


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


# --- class-driven target selection -------------------------------------------
#
# The classes a REPRESENTATION decides, with the phrasing the synthesis prompt
# asks in. Behaviours are transversal: "an error built from a string downcasts
# to `&str`" spans the constructor, the vtable, `object_downcast` and
# `Error::downcast`. Selecting fns cannot reach one, which is why the previous
# selector's alphabetical `[:max_targets]` produced probes for
# `Error::backtrace` / `Error::into_boxed_dyn_error` / `Error::reallocate_…`
# and nothing of any class. Per-unit reachability is unit tests' job.
CLASS_PROMPTS: Mapping[str, str] = {
    "IDENTITY":
        "the concrete type a downcast finds on a value this crate constructed "
        "— the TypeId round-trip, named by type",
    "TRAIT_OBJECT_IDENTITY":
        "the type observed THROUGH a trait object (`&dyn std::error::Error`), "
        "which a wrapper representation can answer differently",
    "EXACT_FORMAT":
        "an exact rendering — Display, Debug, or the alternate `{:#?}` Debug, "
        "which differ from each other",
    "BOUNDED_RESOURCE":
        "what happens at a size or capacity boundary, and just past it",
    "OPTION_SEMANTICS":
        "when an optional observable is present and when it is absent",
}


def class_targets(
    specs: Iterable[BehaviourSpec],
    class_of: Callable[[BehaviourSpec], str],
    *,
    max_targets: int,
    classes: Sequence[str] = (),
) -> tuple[tuple[str, str], ...]:
    """`(class, what to write)` pairs for the classes this suite is THINNEST in,
    scarcest first, capped at `max_targets`.

    Scarcity, not absence, is the ordering: a class with zero specs and a class
    with two are both under-specified, and an absence can be an artifact of the
    classifier rather than a real hole — `CHAIN_SEQUENCE` and `DOWNCAST_OK` read
    zero on the live suite while `tests/test_chain.rs` contributes 16 specs under
    other kinds, and `DOWNCAST_OK` was deliberately retired on 2026-08-26 for
    carrying a bool without saying WHICH type. Ranking by count asks for more of
    what is thin without asserting that a zero is a gap.

    `class_of` is injected so the domain does not import the selector that reads
    a spec's class (`behaviour_select.behaviour_class_name`)."""
    if max_targets <= 0:
        return ()
    wanted = [c for c in (classes or tuple(CLASS_PROMPTS)) if c in CLASS_PROMPTS]
    counts = dict.fromkeys(wanted, 0)
    for spec in specs:
        name = class_of(spec)
        if name in counts:
            counts[name] += 1
    order = sorted(wanted, key=lambda c: (counts[c], wanted.index(c)))
    return tuple((c, CLASS_PROMPTS[c]) for c in order[:max_targets])


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
    """LEGACY free-form capture: `given` = a whole program, Then = its stdout.

    Kept for replies that still arrive in the old shape. The record it makes is
    the one measured unusable — NORMALIZED_STDOUT is in no format or identity
    set, so `classified()` returns OTHER, `behaviours_pinned_by` can never join
    it, and t1.5's cap discards it. Prefer :func:`captured_spec`."""
    spec = BehaviourSpec(
        behaviour_id=probe_behaviour_id(cfg_cluster, unit, n),
        given=program, when=unit,
        then=ObservableOutcome.stdout(normalize_stdout(stdout)),
        provenance=BehaviourProvenance.SYNTHESIZED,
        subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN,
        cfg_cluster=cfg_cluster, source_ref=f"probe::{unit}")
    return classified(spec, default_cluster=default_cluster or cfg_cluster)


def captured_spec(
    *,
    cfg_cluster: str,
    probe: CapturedProbe,
    n: int,
    stdout: str,
    default_cluster: str | None = None,
    capture_env: str = "",
) -> BehaviourSpec:
    """A capture in the constrained shape: `given` = the SETUP statements,
    `when` = the single expression, Then = the observed value under its typed
    kind. `stdout` is the RAW capture; :func:`printed_value` removes the one
    newline `println!` added and nothing else, because the replay asserts this
    value against what `format!` produces.

    The program is not stored — :func:`render_probe_program` rebuilds it from
    these three whenever a runnable form is needed (the capture run itself, and
    `exercises_by_coverage`). An identity `when` stores the TYPE as the Then's
    value with `ok` from the printed boolean, because that is what makes the pin
    say WHICH TypeId the round-trip must preserve."""
    unit = probe.name or "unnamed"
    text = printed_value(stdout)
    kind = observe_kind(probe.observe, probe.when)
    if kind is OutcomeKind.TYPE_IDENTITY:
        then = ObservableOutcome.type_identity(
            identity_type_of(probe.when) or "?", ok=text.strip() == "true")
    elif kind is OutcomeKind.DEBUG_STRING:
        then = ObservableOutcome.debug(text)
    elif kind is OutcomeKind.ALT_DEBUG_STRING:
        then = ObservableOutcome.alt_debug(text)
    else:
        then = ObservableOutcome.display(text)
    spec = BehaviourSpec(
        behaviour_id=probe_behaviour_id(cfg_cluster, unit, n),
        given=probe.given.rstrip(), when=probe.when,
        then=then,
        provenance=BehaviourProvenance.SYNTHESIZED,
        subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN,
        cfg_cluster=cfg_cluster, source_ref=f"probe::{unit}",
        capture_env=capture_env)
    return classified(spec, default_cluster=default_cluster or cfg_cluster)


def program_for(spec: BehaviourSpec, *, crate: str = "") -> str:
    """The runnable program for ANY probe spec — the stored one for a legacy
    free-form capture, a rendered one for the constrained shape.

    `exercises_by_coverage` re-runs this. Before it existed that caller passed
    `spec.given` straight to `cargo run --example`, so a `given` that stopped
    being a whole program would have failed every probe's exercises run with a
    log line and no other trace."""
    if spec.then is not None and spec.then.kind is OutcomeKind.NORMALIZED_STDOUT:
        return spec.given
    observe = {OutcomeKind.DEBUG_STRING: "debug",
               OutcomeKind.ALT_DEBUG_STRING: "alt_debug"}.get(
                   spec.then.kind if spec.then else OutcomeKind.DISPLAY_STRING,
                   "display")
    return render_probe_program(spec.given, spec.when, observe, crate=crate)


def render_replay_test(spec: BehaviourSpec, *, fn_name: str) -> str:
    """The REPLAY role: the same capture as a `#[test]` asserting the value it
    was observed to have. `""` when the spec cannot be replayed (a legacy
    free-form blob, or no Then).

    This is the half that makes a capture part of the cargo-test oracle. The
    expected value is the OBSERVED one — never a model's guess — which is the
    whole reason the capture route exists beside t0c's."""
    then = spec.then
    if then is None or then.kind is OutcomeKind.NORMALIZED_STDOUT:
        return ""
    body = spec.given.rstrip()
    setup = (body + "\n") if body else ""
    if then.kind is OutcomeKind.TYPE_IDENTITY:
        check = (f"    assert!({spec.when});" if then.ok
                 else f"    assert!(!({spec.when}));")
    else:
        fmt = _KIND_TO_FMT.get(then.kind, "{}")
        lit = rust_string_literal(then.value)
        check = f'    assert_eq!(format!("{fmt}", {spec.when}), {lit});'
    return (f"#[test]\nfn {fn_name}() {{\n{setup}{check}\n}}\n")


def rust_string_literal(text: str) -> str:
    """`text` as a Rust literal. A raw string when it holds a backslash or a
    quote and no `"#` sequence — the captured values are exact renderings and
    escaping them by hand is how an expected value stops matching itself."""
    if ("\\" in text or '"' in text) and '"#' not in text:
        return 'r#"' + text + '"#'
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"' + out + '"' 
