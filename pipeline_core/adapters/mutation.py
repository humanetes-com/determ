"""Mutation-testing behaviour hardening.

cargo-mutants drives this: it finds SURVIVING ("missed") mutants — behaviour
changes the current test suite is blind to — and the harden loop asks
the model for BLACK-BOX behaviour tests (public API only; the model
never sees source bodies or mutant diffs) that compile, pass on the correct
code, and lower the surviving-mutant count.

Lifted verbatim out of the pipeline.py god-file. The composition root injects the
cargo env builder, the LLM `ask`, a log sink, and the two caps; the pure
fence-strip and the api-surface reader are imported directly.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.text import strip_markdown_fences
from .artifacts import load_api_surface


@dataclass(frozen=True, slots=True)
class MutationTester:
    """cargo-mutants survivor scan + behaviour-hardening loop, deps injected."""

    cargo_env: Callable[[], dict[str, str]]
    ask: Callable[..., str]
    log: Callable[[str], None]
    task0c_model: str
    single_call_char_limit: int

    def survivors(self, project_dir: Path, cluster: Any = None,
                  file_rel: str | None = None, timeout: int = 1200) -> list[str] | None:
        """Run cargo-mutants and return the SURVIVING mutants (the behaviour
        changes the suite is blind to). None when cargo-mutants is unavailable
        or no report was produced (it exits non-zero when mutants survive, so
        the report is read regardless of exit code)."""
        if not (Path.home() / ".cargo" / "bin" / "cargo-mutants").exists() \
                and shutil.which("cargo-mutants") is None:
            return None
        cmd = ["cargo"]
        tc = getattr(cluster, "toolchain", None) if cluster is not None else None
        if tc:
            cmd.append(f"+{tc}")
        cmd += ["mutants", "--no-shuffle"]
        if file_rel:
            cmd += ["--file", file_rel]
        try:
            subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, errors="replace",
                           timeout=timeout, env=self.cargo_env())
        except Exception:
            return None
        missed = project_dir / "mutants.out" / "missed.txt"
        if not missed.exists():
            return None
        try:
            return [ln.strip() for ln in missed.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except OSError:
            return None

    def kill_check(self, project_dir: Path, cluster: Any, survivor: str,
                   test_code: str, target_rel: str, timeout: int = 900) -> tuple[bool, bool]:
        """Discrimination harness. Returns (real_passes, kills_survivor).
        Attaches `test_code` as a test module to `target_rel`, runs the real
        suite (must pass on the correct code), then re-runs cargo-mutants scoped
        to the survivor's file and checks the survivor moved missed→caught.
        Always restores the file. Black-box (public API only)."""
        surv_file = survivor.split(":", 1)[0] if survivor else None
        tgt = project_dir / target_rel
        if not tgt.exists():
            return (False, False)
        sib = tgt.with_name(f"{tgt.stem}.mut_candidate.rs")
        decl = f'\n#[cfg(test)]\n#[allow(clippy::all, unused)]\n#[path = "{sib.name}"]\nmod mut_candidate;\n'
        orig = tgt.read_text(encoding="utf-8")
        tc = getattr(cluster, "toolchain", None) if cluster is not None else None
        try:
            sib.write_text(test_code, encoding="utf-8")
            tgt.write_text(orig.rstrip() + decl, encoding="utf-8")
            cmd = ["cargo"] + ([f"+{tc}"] if tc else []) + ["test", "--quiet"]
            r = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, errors="replace",
                               timeout=timeout, env=self.cargo_env())
            if r.returncode != 0:
                return (False, False)            # invalid test (won't compile/pass)
            if not surv_file:
                return (True, False)
            remaining = self.survivors(project_dir, cluster, file_rel=surv_file,
                                       timeout=timeout)
            killed = remaining is not None and survivor not in remaining
            return (True, killed)
        except Exception:
            return (False, False)
        finally:
            tgt.write_text(orig, encoding="utf-8")
            try:
                if sib.exists():
                    sib.unlink()
            except OSError:
                pass

    def harden_file(self, project_dir: Path, module_name: str, cluster: Any,
                    file_rel: str, max_rounds: int = 3, timeout: int = 900) -> str:
        """Generate BLACK-BOX behaviour tests (public API only) that reduce
        the surviving mutants for `file_rel`. Tests are accepted only when they
        (a) compile + pass on the correct code and (b) actually lower the
        surviving-mutant count. Returns the accumulated kept test code."""
        baseline = self.survivors(project_dir, cluster, file_rel, timeout)
        if not baseline:
            return ""
        tgt = project_dir / file_rel
        if not tgt.exists():
            return ""
        api = (load_api_surface(project_dir, module_name) or "")[: self.single_call_char_limit // 3]
        sib = tgt.with_name(f"{tgt.stem}.mut_hardening.rs")
        decl = (f'\n#[cfg(test)]\n#[allow(clippy::all, unused)]\n'
                f'#[path = "{sib.name}"]\nmod mut_hardening;\n')
        orig = tgt.read_text(encoding="utf-8")
        tc = getattr(cluster, "toolchain", None) if cluster is not None else None
        kept: list[str] = []
        cur = set(baseline)
        try:
            for _r in range(max_rounds):
                if not cur:
                    break
                prompt = (
                    "Write Rust `#[test]` functions that exercise and ASSERT the exact "
                    "observable behaviour of this crate's PUBLIC API — return values, "
                    "panics/Err, and precise formatted output (Display/Debug). Cover "
                    "edge cases and boundaries; pin exact strings/values with "
                    "`assert_eq!`. Use ONLY the public API below — do not reference "
                    "internal items. The current suite still misses subtle behaviour "
                    "changes; make your assertions tight enough to catch them.\n"
                    f'<PUBLIC_API crate="{module_name}">\n{api}\n</PUBLIC_API>\n'
                    "Output ONLY the test code (no prose, no explanation)."
                )
                try:
                    resp = self.ask(prompt, model=self.task0c_model)
                except Exception:
                    break
                code = strip_markdown_fences(resp or "").strip()
                if not code:
                    continue
                sib.write_text("\n\n".join(kept + [code]), encoding="utf-8")
                tgt.write_text(orig.rstrip() + decl, encoding="utf-8")
                cmd = ["cargo"] + ([f"+{tc}"] if tc else []) + ["test", "--quiet"]
                try:
                    rr = subprocess.run(cmd, cwd=project_dir, capture_output=True,
                                        text=True, errors="replace", timeout=timeout, env=self.cargo_env())
                except Exception:
                    continue
                if rr.returncode != 0:
                    continue   # invalid tests — discard this round
                remaining = self.survivors(project_dir, cluster, file_rel, timeout)
                if remaining is None:
                    continue
                if len(remaining) < len(cur):
                    kept.append(code)
                    cur = set(remaining)
                    self.log(f"  [mut-harden] {file_rel}: survivors "
                             f"{len(baseline)}→{len(cur)} (kept round {_r + 1})")
            return "\n\n".join(kept)
        finally:
            tgt.write_text(orig, encoding="utf-8")
            try:
                if sib.exists():
                    sib.unlink()
            except OSError:
                pass
