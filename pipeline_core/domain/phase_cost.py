"""What a pipeline phase cost, and on what code, configuration and starting tree.

"Did this change make t0c slower" used to be answered by hand each time, from a
record that could not answer it: `phase.exit` carried a wall time keyed by the
git HEAD (identical across every uncommitted edit between two commits), mixed
real exits with three-millisecond `skipped` ones, and no phase said how much
WORK it bought. Wall time alone moves with GPU contention and vLLM cache warmth;
the calls, tokens and cargo invocations a phase paid for move only when the
code or its input does.

This module is the pure half:

* :class:`PhaseWork` — the work counters one phase accumulated;
* :func:`classify_cargo` — what one spawned process was, read from its argv;
* :func:`work_delta` — the counters one structured event adds;
* :class:`PhaseCostFold` — the fold from the event stream (`phase.enter` /
  `phase.exit` carrying a `scope` id, every other event carrying the
  `in_scope` of the phase that emitted it) to one :class:`PhaseSample` per
  closed phase, the cost charged inclusively up the scope chain;
* :class:`CodeIdentity` and :class:`RunConfig` — the two halves of "same
  code, same configuration" a comparison is keyed on.

The comparison itself is :mod:`pipeline_core.domain.phase_cost_baseline`.
"""
from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from .model_defaults import resolve_route_order
from .phase_registry import PHASE_ENTER, PHASE_EXIT

# ── work counters ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PhaseWork:
    """Counters one phase (or one run) accumulated. Mutable: the fold adds to
    an open phase's counters as its events arrive."""

    llm_calls: int = 0
    llm_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_chars: int = 0
    compiles: int = 0
    test_runs: int = 0
    other_cargo: int = 0

    def add(self, other: PhaseWork) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def copy(self) -> PhaseWork:
        return PhaseWork(**self.as_dict())

    def as_dict(self) -> dict[str, int]:
        return {f.name: int(getattr(self, f.name)) for f in fields(self)}

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cargo(self) -> int:
        """Compiles and test runs: the cargo invocations that cost build time."""
        return self.compiles + self.test_runs

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> PhaseWork:
        return cls(**{f.name: int(row.get(f.name) or 0) for f in fields(cls)})


WORK_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(PhaseWork))


# ── cargo invocations ────────────────────────────────────────────────────────


class CargoKind(StrEnum):
    COMPILE = "compile"   # build / check / clippy / rustc / doc / `test --no-run`
    TEST = "test"         # test / nextest / llvm-cov / tarpaulin / bench / miri
    OTHER = "other"       # metadata, fmt, clean, tree, `llvm-cov report`, …


_TEST_SUBCOMMANDS = frozenset({"test", "t", "nextest", "llvm-cov", "tarpaulin",
                               "bench", "miri"})
_COMPILE_SUBCOMMANDS = frozenset({"build", "b", "check", "c", "clippy", "rustc",
                                  "doc", "d"})
# `cargo llvm-cov <sub>` forms that only read an existing profile.
_LLVM_COV_REPORTING = frozenset({"report", "clean", "show-env"})
# Global cargo flags that take their value as the NEXT token.
_VALUED_GLOBAL_FLAGS = frozenset({"-Z", "-C", "--config", "--color",
                                  "--manifest-path"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _invocation_kind(tokens: Sequence[str]) -> CargoKind:
    """`tokens` is one cargo invocation AFTER the `cargo` word."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("+"):          # toolchain selector
            i += 1
            continue
        if tok in _VALUED_GLOBAL_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return CargoKind.OTHER
    sub, rest = tokens[i], tokens[i + 1:]
    if sub in _TEST_SUBCOMMANDS:
        if "--no-run" in rest:
            return CargoKind.COMPILE
        if sub == "llvm-cov":
            nxt = next((t for t in rest if not t.startswith("-")), "")
            if nxt in _LLVM_COV_REPORTING:
                return CargoKind.OTHER
        return CargoKind.TEST
    if sub in _COMPILE_SUBCOMMANDS:
        return CargoKind.COMPILE
    return CargoKind.OTHER


def _scan(tokens: Sequence[str]) -> list[CargoKind]:
    kinds: list[CargoKind] = []
    i = 0
    while i < len(tokens):
        name = _basename(tokens[i])
        if name == "rustc":
            kinds.append(CargoKind.COMPILE)
        if name == "cargo":
            j = i + 1
            while (j < len(tokens) and tokens[j] not in _SEPARATORS
                   and _basename(tokens[j]) != "cargo"):
                j += 1
            kinds.append(_invocation_kind(tokens[i + 1:j]))
            i = j
            continue
        i += 1
    return kinds


def classify_cargo(argv: Sequence[str]) -> list[CargoKind]:
    """The cargo (and bare rustc) invocations one spawned process makes.

    `argv` is what `subprocess.Popen` was given, after its own shell expansion:
    a `sh -c "<cmd>"` command is tokenised and every `cargo` in it counts, so
    `cargo build && cargo test` is a compile and a test run. A wrapper in front
    (`timeout 600 cargo test`, `env X=1 cargo build`) is skipped over. Anything
    that is not cargo or rustc is an empty list."""
    if not argv:
        return []
    if _basename(argv[0]) in _SHELLS and "-c" in argv:
        k = list(argv).index("-c")
        if k + 1 < len(argv):
            try:
                tokens = shlex.split(argv[k + 1])
            except ValueError:
                tokens = argv[k + 1].split()
            return _scan(tokens)
        return []
    return _scan(argv)


# ── the events that carry work ───────────────────────────────────────────────

CARGO_SPAWN = "cargo.spawn"


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def work_delta(record: Mapping[str, Any]) -> PhaseWork | None:
    """The counters one structured event adds, or None when it carries none."""
    ev = record.get("event")
    data = record.get("data")
    data = data if isinstance(data, Mapping) else {}
    if ev == "llm.request":
        return PhaseWork(llm_calls=1)
    if ev == "llm.response":
        resp = data.get("response")
        chars = _int(resp.get("char_count")) if isinstance(resp, Mapping) else 0
        return PhaseWork(llm_ms=_int(record.get("duration_ms")), response_chars=chars)
    if ev in ("llm.stream_meta", "llm.followup_meta"):
        return PhaseWork(prompt_tokens=_int(data.get("prompt_tokens")),
                         completion_tokens=_int(data.get("completion_tokens")))
    if ev == CARGO_SPAWN:
        kinds = data.get("kinds")
        kinds = kinds if isinstance(kinds, list) else []
        return PhaseWork(compiles=kinds.count(CargoKind.COMPILE.value),
                         test_runs=kinds.count(CargoKind.TEST.value),
                         other_cargo=kinds.count(CargoKind.OTHER.value))
    return None


# ── one closed phase ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhaseKey:
    """Which phase: the registry id plus the module and cluster it ran for. Two
    clusters' t0c are different work and are never pooled."""

    phase: str
    module: str = ""
    cluster: str = ""

    def label(self) -> str:
        where = "/".join(p for p in (self.module, self.cluster) if p)
        return f"{self.phase}[{where}]" if where else self.phase


@dataclass(frozen=True, slots=True)
class PhaseSample:
    key: PhaseKey
    scope: str
    parent_scope: str | None
    started: str
    wall_ms: int
    outcome: str
    work: PhaseWork

    @property
    def top_level(self) -> bool:
        return self.parent_scope is None


@dataclass(slots=True)
class _Frame:
    key: PhaseKey
    parent: str | None
    started: str
    work: PhaseWork = field(default_factory=PhaseWork)


class PhaseCostFold:
    """Fold one process's event stream into closed-phase samples.

    `phase.enter` with a `scope` opens a frame; `phase.exit` with the same
    `scope` closes it into a :class:`PhaseSample`. Any other event whose
    :func:`work_delta` is non-empty is charged to the frame named by its
    `in_scope` and to every ancestor still open — a step's calls are also its
    phase's calls. An event with no `in_scope` (a context-free daemon thread,
    or before the first phase) still counts toward :attr:`run_work`.

    Not thread-safe: the caller serialises :meth:`feed`."""

    def __init__(self) -> None:
        self._frames: dict[str, _Frame] = {}
        self.run_work = PhaseWork()

    def open_scopes(self) -> int:
        return len(self._frames)

    def feed(self, record: Mapping[str, Any]) -> PhaseSample | None:
        ev = record.get("event")
        if ev == PHASE_ENTER:
            sid = record.get("scope")
            if isinstance(sid, str) and sid:
                parent = record.get("parent_scope")
                self._frames[sid] = _Frame(
                    key=PhaseKey(str(record.get("phase") or ""),
                                 str(record.get("module") or ""),
                                 str(record.get("cluster") or "")),
                    parent=parent if isinstance(parent, str) and parent else None,
                    started=str(record.get("ts") or ""))
            return None
        if ev == PHASE_EXIT:
            sid = record.get("scope")
            fr = self._frames.pop(sid, None) if isinstance(sid, str) else None
            if fr is None:
                return None
            return PhaseSample(key=fr.key, scope=str(sid), parent_scope=fr.parent,
                               started=fr.started,
                               wall_ms=_int(record.get("duration_ms")),
                               outcome=str(record.get("outcome") or "ok"),
                               work=fr.work)
        delta = work_delta(record)
        if delta is None:
            return None
        self.run_work.add(delta)
        sid = record.get("in_scope")
        seen: set[str] = set()
        while isinstance(sid, str) and sid in self._frames and sid not in seen:
            seen.add(sid)
            frame = self._frames[sid]
            frame.work.add(delta)
            sid = frame.parent
        return None


# ── identity: same code, same configuration ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class CodeIdentity:
    """`key` is the content hash of the code that decides what a run does: the
    pipeline's Python (`measurement.code_identity`, which leaves out the
    observer library) plus the skeleton extractor's sources. A monitor-only or
    test-only edit does not split a baseline; an extractor edit does.
    `tree_stamp` (the pre-run gate's stamp) and `commit` (HEAD) are landmarks
    for a human, never the comparison key: HEAD is identical across every
    uncommitted edit between two commits."""

    key: str
    tree_stamp: str | None = None
    commit: str | None = None


# Read in this order so the stored JSON is stable across runs.
_KNOBS: tuple[str, ...] = (
    "MODEL", "PHASE_C_ROUTE_ORDER", "PHASE_C_DRIVER", "VLLM_REASONING_STRENGTH",
    "VLLM_MAX_MODEL_LEN", "VLLM_MAX_THINK_CHARS", "VLLM_REASONING_BUDGET",
    "VLLM_THINKING_BUDGET", "PIPELINE_PARALLELISM",
)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The knobs that change what a run does, as the pipeline resolves them.

    One source for the performance ledger and the phase-cost store: the ledger
    used to resolve these itself at append time, after the run, from whatever
    `.env` said by then."""

    values: tuple[tuple[str, Any], ...]
    route_order_source: str = "model-default"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RunConfig:
        model = env.get("MODEL", "")
        route_env = env.get("PHASE_C_ROUTE_ORDER")
        resolved: dict[str, Any] = {
            "MODEL": model,
            "PHASE_C_ROUTE_ORDER": str(resolve_route_order(route_env, model)),
            "PHASE_C_DRIVER": env.get("PHASE_C_DRIVER", "tdd"),
            "VLLM_REASONING_STRENGTH": env.get("VLLM_REASONING_STRENGTH", "high"),
            "VLLM_MAX_MODEL_LEN": _env_int(env, "VLLM_MAX_MODEL_LEN", 0) or None,
            "VLLM_MAX_THINK_CHARS": _env_int(env, "VLLM_MAX_THINK_CHARS", 60000),
            "VLLM_REASONING_BUDGET": env.get("VLLM_REASONING_BUDGET", "fixed"),
            "VLLM_THINKING_BUDGET": _env_int(env, "VLLM_THINKING_BUDGET", 0) or None,
            "PIPELINE_PARALLELISM": _env_int(env, "PIPELINE_PARALLELISM", 0) or None,
        }
        return cls(values=tuple((k, resolved[k]) for k in _KNOBS),
                   route_order_source=("env" if (route_env or "").strip()
                                       else "model-default"))

    def as_dict(self) -> dict[str, Any]:
        """The ledger's historical `env` block, `route_order_source` included."""
        out = dict(self.values)
        out["route_order_source"] = self.route_order_source
        return out

    @property
    def fingerprint(self) -> str:
        """Hash of the resolved values only. Where a route order came from is
        not behaviour: an env var naming the model default runs identically."""
        blob = json.dumps(dict(self.values), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
