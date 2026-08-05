"""Role ports for the shared fix-loop use-case (`application.reconstruct.fix_unit`
+ `application.reconstruct.generate_fix`, P3.C).

Each Protocol is a narrow ROLE (ISP) — a fix unit receives only the seams it
needs: the compile probe, the model call, the source-tree splicer, and the
prompt-context assembler. The pure pieces a unit also uses (the prompt BUILDERS
in `prompts.fix_prompts`, body extraction in `domain.llm_output`, the typed
targets/session/report in `domain`) are imported directly — they are not I/O and
do not cross a port. `pipeline.py` satisfies these structurally at the
composition root; tests pass fakes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.models import FnRecord, Frontier
from pipeline_core.domain.reconstruction import SpliceOutcome


@runtime_checkable
class CompileChecker(Protocol):
    """Run the scoped compile-only check and return a structured probe (exit
    code + ANSI-stripped render + the (file, fn)→errors frontier built from the
    STRUCTURED diagnostics, never the colour-coded text)."""

    def probe(self, cmd: str, cwd: Path, env: dict[str, str],
              timeout: int) -> CompileProbe: ...


@runtime_checkable
class FixModel(Protocol):
    """The local LLM call (Hard Rule 7 — never an external model)."""

    def ask(self, prompt: str) -> str: ...


@runtime_checkable
class FixSplicer(Protocol):
    """Source-tree mutation for the fix loop: locate a fn's signature/body byte
    range, splice a new body into the CORRECT container (optionally adding a
    leading `where` clause), test pub-ness for the API-surface guard, and render
    an impl block's current source for the prompt."""

    def locate_fn(self, text: str, name: str,
                  ) -> tuple[str, int, int, int] | None: ...
    def is_pub_fn(self, text: str, name: str) -> bool: ...
    def render_impl_block(self, text: str, members: tuple[FnRecord, ...]) -> str: ...
    def splice_fn(
        self, file_path: Path, container: str, name: str, body: str, *,
        where_clause: str | None = ..., allow_nonstub: bool = ...,
    ) -> SpliceOutcome: ...
    def splice_body(
        self, file_path: Path, name: str, body: str, *,
        allow_nonstub: bool = ...,
    ) -> SpliceOutcome: ...
    # The collision-safe write. `splice_body` locates by bare name and refuses
    # `ambiguous_name` the moment a file holds two fns of that name — on the
    # anyhow tree that is the common case (`error.rs::from` 4x, `kind.rs::new`
    # 3x), and the one-shot behaviour fill refused `src/error.rs::from` that way
    # on 2026-08-05 after a full model call. Passing the signature the caller
    # already located re-resolves the target through the syn record, so the body
    # lands on the definition the prompt was built from.
    def splice_body_scoped(
        self, file_path: Path, name: str, container: str, sig_text: str,
        new_body: str, *, allow_nonstub: bool = ...,
    ) -> SpliceOutcome: ...
    def restore_signature(
        self, file_path: Path, name: str, skeleton_sig: str,
    ) -> bool: ...


@runtime_checkable
class FixPrompts(Protocol):
    """The I/O-touching prompt CONTEXT pieces (FR/genesis/API repository spec,
    crate gotchas relevant to an error, and the test sources a set of fns must
    satisfy). The prompt builders themselves are pure and imported directly; this
    port only supplies the context strings they interpolate."""

    def repo_spec_block(self, *, file_rel: str = ..., fn_name: str = ...,
                        container: str = ...) -> str: ...
    def gotchas_block(self, output: str) -> str: ...
    def tests_block(self, names: frozenset[str]) -> str: ...


@runtime_checkable
class FrontierBuilder(Protocol):
    """Attribute a failure to (file, fn) repair targets. Each method returns an
    ALREADY test-oracle-filtered frontier (FR-018): the compile-error frontier,
    the runtime-panic (unimplemented-stub) frontier, and the behavioural frontier
    (assertion/wrong-value failures attributed to the fns they exercise, paired
    with the failing tests' source for the prompt)."""

    def compile_frontier(self, output: str) -> Frontier: ...
    def tdd_frontier(self, output: str) -> Frontier: ...
    def behavior_frontier(
        self, output: str,
    ) -> tuple[Frontier, dict[str, tuple[str, str]]]: ...
