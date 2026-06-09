"""L1 e2e harness — CODE_ARCHITECTURE.md R10.

Drive the pipeline's ORCHESTRATION decisions (build-failure routing, keep/revert,
caps, scheduling) with a scripted model + scripted cargo — no live LLM, no real
cargo. These tests retire the "●●● await live run" tag on the logic items (R9.1,
R9.2, W3, session-56 cfg-filter), which react to model+cargo OUTPUT, not model
QUALITY.

Seams (the single injection points, verified session 58):
  pipeline._ask_per_fn_body / _ask_raw         ← scripted model
  pipeline._run_cargo / _run_cargo_check_diag  ← scripted cargo
  pipeline._frontier_from_diags                ← scripted attribution (skips the
                                                 extractor binary, absent in CI py)

A "scripted" seam is either a constant, a list (popped left, last value repeats
once exhausted), or a callable evaluated per call. `diag(...)` builds a real
domain Diagnostic so the keep/revert metric (error count) is exercised for real.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pipeline
from pipeline_core.domain.models import Diagnostic


def diag(file: str, line: int = 1, code: str = "E0277",
         message: str = "mismatched types") -> Diagnostic:
    """One structured cargo diagnostic, as `_run_cargo_check_diag` would emit."""
    return Diagnostic(code=code, file=file, line=line, message=message,
                      rendered=f"error[{code}]: {message}\n --> {file}:{line}")


class _Script:
    """A scripted value source: constant | list (left-pop, last repeats) | callable."""

    def __init__(self, spec: Any):
        self._spec = spec
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        spec = self._spec
        if callable(spec):
            return spec(*args, **kwargs)
        if isinstance(spec, list):
            if len(spec) > 1:
                return spec.pop(0)
            return spec[0] if spec else None
        return spec


def install_scripted_model(
    monkeypatch,
    per_fn_body: Callable[[str], str] | str = "{ unimplemented!() }",
    raw: Callable[..., str] | str | None = None,
) -> _Script:
    """Patch the model seams. `per_fn_body` maps fn_name→new body (str or callable).
    Returns the per-fn `_Script` so a test can assert call count."""
    body_src = per_fn_body if callable(per_fn_body) else (lambda _fn: per_fn_body)

    def _fake_per_fn(prompt, fn, *a, **k):  # _ask_per_fn_body → (body, meta)
        return body_src(fn), None

    script = _Script(_fake_per_fn)
    monkeypatch.setattr(pipeline, "_ask_per_fn_body", script)
    if raw is not None:
        monkeypatch.setattr(pipeline, "_ask_raw", _Script(raw))
    return script


def install_scripted_cargo(
    monkeypatch,
    check_diag: Any,
    run: Any = (0, "test result: ok. 0 passed; 0 failed"),
    frontier: Any = None,
) -> dict[str, _Script]:
    """Patch the cargo + attribution seams.

    check_diag : (rc, rendered, [Diagnostic]) — what `_run_cargo_check_diag` returns.
    run        : (rc, text)                   — what `_run_cargo` returns.
    frontier   : Frontier (dict) for `_frontier_from_diags` — bypasses the extractor.

    Each is a constant / list / callable (see `_Script`).
    """
    scripts = {
        "check_diag": _Script(check_diag),
        "run": _Script(run),
    }
    monkeypatch.setattr(pipeline, "_run_cargo_check_diag",
                        lambda *a, **k: scripts["check_diag"]())
    monkeypatch.setattr(pipeline, "_run_cargo",
                        lambda *a, **k: scripts["run"]())
    if frontier is not None:
        fscript = _Script(frontier)
        scripts["frontier"] = fscript
        monkeypatch.setattr(pipeline, "_frontier_from_diags",
                            lambda *a, **k: fscript())
    return scripts


def install_scripted_attribution(monkeypatch, frontier: Any, kind: str = "scripted") -> _Script:
    """Patch `_attribute_failures` (TEST-mode fix path) → (frontier, kind), bypassing
    the test-reverse-map + extractor. `frontier` is constant / list / callable."""
    fscript = _Script(frontier)
    monkeypatch.setattr(pipeline, "_attribute_failures",
                        lambda *a, **k: (fscript(), kind))
    return fscript
