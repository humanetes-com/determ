"""Fix-loop role records (P3 step 29): parameterized call-shaping records over
injected callables — NO I/O, NO external boundary, so their correct layer is
APPLICATION, not adapters. Living here lets the fix loop construct them
directly instead of receiving the CLASSES through its deps record.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.models import Diagnostic, FnRecord, Frontier
from pipeline_core.domain.reconstruction import SpliceOutcome


# --------------------------------------------------------------------------- #
# ports.fix_engine.CompileChecker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FixCheckerAdapter:
    run_cargo_check_diag: Callable[[str, Path, dict[str, str], int],
                                   tuple[int, str, list[Diagnostic]]]
    strip_ansi: Callable[[str], str]
    frontier_from_diags: Callable[[list[Diagnostic]], Frontier]

    def probe(self, cmd: str, cwd: Path, env: dict[str, str],
              timeout: int) -> CompileProbe:
        rc, rendered, diags = self.run_cargo_check_diag(cmd, cwd, env, timeout)
        ebf: dict[str, int] = {}
        for d in diags:
            f = d.file or ""
            if f:
                ebf[f] = ebf.get(f, 0) + 1
        return CompileProbe(rc=rc, rendered=self.strip_ansi(rendered),
                            frontier=self.frontier_from_diags(diags),
                            errors_by_file=ebf)


# --------------------------------------------------------------------------- #
# ports.fix_engine.FixModel
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FixModelAdapter:
    ask_fn: Callable[[str], str]

    def ask(self, prompt: str) -> str:
        return self.ask_fn(prompt)


# --------------------------------------------------------------------------- #
# ports.fix_engine.FixSplicer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FixSplicerAdapter:
    locate_fn_fn: Callable[[str, str], tuple[str, int, int, int] | None]
    is_pub_fn_fn: Callable[[str, str], bool]
    render_impl_block_fn: Callable[[str, tuple[FnRecord, ...]], str]
    splice_fn_fn: Callable[..., SpliceOutcome]
    splice_body_fn: Callable[..., SpliceOutcome]
    restore_signature_fn: Callable[[Path, str, str], bool]

    def locate_fn(self, text: str, name: str,
                  ) -> tuple[str, int, int, int] | None:
        return self.locate_fn_fn(text, name)

    def is_pub_fn(self, text: str, name: str) -> bool:
        return self.is_pub_fn_fn(text, name)

    def render_impl_block(self, text: str, members: tuple[FnRecord, ...]) -> str:
        return self.render_impl_block_fn(text, members)

    def splice_fn(self, file_path: Path, container: str, name: str, body: str, *,
                  where_clause: str | None = None,
                  allow_nonstub: bool = False) -> SpliceOutcome:
        return self.splice_fn_fn(file_path, container, name, body,
                                 where_clause=where_clause,
                                 allow_nonstub=allow_nonstub)

    def splice_body(self, file_path: Path, name: str, body: str, *,
                    allow_nonstub: bool = False) -> SpliceOutcome:
        return self.splice_body_fn(file_path, name, body,
                                   allow_nonstub=allow_nonstub)

    def restore_signature(self, file_path: Path, name: str,
                          skeleton_sig: str) -> bool:
        return self.restore_signature_fn(file_path, name, skeleton_sig)


# --------------------------------------------------------------------------- #
# ports.fix_engine.FixPrompts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FixPromptsAdapter:
    repo_spec_block_fn: Callable[..., str]
    gotchas_block_fn: Callable[[str], str]
    tests_block_fn: Callable[[frozenset[str]], str]

    def repo_spec_block(self, *, file_rel: str = "", fn_name: str = "",
                        container: str = "") -> str:
        return self.repo_spec_block_fn(file_rel=file_rel, fn_name=fn_name,
                                       container=container)

    def gotchas_block(self, output: str) -> str:
        return self.gotchas_block_fn(output)

    def tests_block(self, names: frozenset[str]) -> str:
        return self.tests_block_fn(names)


# --------------------------------------------------------------------------- #
# ports.fix_engine.FrontierBuilder
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FrontierBuilderAdapter:
    compile_frontier_fn: Callable[[str], Frontier]
    tdd_frontier_fn: Callable[[str], Frontier]
    behavior_frontier_fn: Callable[[str],
                                   tuple[Frontier, dict[str, tuple[str, str]]]]

    def compile_frontier(self, output: str) -> Frontier:
        return self.compile_frontier_fn(output)

    def tdd_frontier(self, output: str) -> Frontier:
        return self.tdd_frontier_fn(output)

    def behavior_frontier(self, output: str,
                          ) -> tuple[Frontier, dict[str, tuple[str, str]]]:
        return self.behavior_frontier_fn(output)
