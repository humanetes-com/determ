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
    # Port-boundary injection (P3-S2): swap the App's per-fn model seam.
    # `_ask_raw` stays a module seam — it is the TRANSPORT under the real
    # AskClient (tests scripting raw exercise the guard logic on purpose).
    import dataclasses as _dc
    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP, ask_per_fn_body=script))
    if raw is not None:
        monkeypatch.setattr(pipeline, "_ask_raw", _Script(raw))
    # Retrospective is a distinct model seam: a fix-loop that exhausts calls
    # `_ask_retrospective` → the REAL `_ASK_CLIENT` (not `_ask_per_fn_body`/`_ask_raw`).
    # Offline, that meant real TCP connects to an absent server — ~30s/attempt of
    # dead time (conftest no-ops sleep, not socket connect). Stub it to "" (the same
    # empty-retro the absent server produced, only instant) so offline tests stay offline.
    monkeypatch.setattr(pipeline, "_ask_retrospective", lambda *a, **k: "")
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

    class _ScriptedCargoRunner:
        """ports.cargo_runner.CargoRunner over the scripts — injected on _APP
        (port boundary, P3-S2) instead of patching the module fns."""

        def run(self, cmd, cwd, env, timeout=180):  # noqa: ANN001, ANN201
            return scripts["run"]()

        def check_diag(self, cmd, cwd, env, timeout=180):  # noqa: ANN001, ANN201
            return scripts["check_diag"]()

    import dataclasses as _dc
    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP,
                                    cargo_runner=_ScriptedCargoRunner()))
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


class FakeStructureReader:
    """A `ports.structure.StructureReader` over scripted values (P3-S2).

    Each field is a value or a callable evaluated per call (same `_Script`
    semantics as the model/cargo seams). Inject via
    :func:`install_fake_extractor` — the port-boundary replacement for
    monkeypatching `pipeline._enumerate_*_via_extractor` module fns."""

    def __init__(self, *, fns: Any = None, types: Any = None, mods: Any = None,
                 consts: Any = None, dispatch_families: Any = None) -> None:
        self._fns = _Script(fns) if callable(fns) else fns
        self._types = _Script(types) if callable(types) else types
        self._mods = mods
        self._consts = consts
        self._families = dispatch_families

    def enumerate_fns(self, src_dir):  # noqa: ANN001, ANN201 — port signature
        return self._fns(src_dir) if isinstance(self._fns, _Script) else self._fns

    def enumerate_types(self, src_dir):  # noqa: ANN001, ANN201
        return (self._types(src_dir) if isinstance(self._types, _Script)
                else self._types)

    def enumerate_mods(self, src_dir):  # noqa: ANN001, ANN201
        return self._mods

    def enumerate_consts(self, src_dir):  # noqa: ANN001, ANN201
        return self._consts

    def enumerate_dispatch_families(self, src_dir):  # noqa: ANN001, ANN201
        return self._families


def install_fake_extractor(monkeypatch, *, fns: Any = None, types: Any = None,
                           mods: Any = None, consts: Any = None,
                           dispatch_families: Any = None) -> FakeStructureReader:
    """Swap the App's syn-extractor structure provider for a scripted fake —
    the port-boundary injection (`build_app`-style) replacing per-shim
    monkeypatches of `pipeline._enumerate_fns_via_extractor` et al. All shims
    and the fill engine resolve structure through `pipeline._APP` at call
    time, so one swap covers every read."""
    import dataclasses as _dc
    reader = FakeStructureReader(fns=fns, types=types, mods=mods, consts=consts,
                                 dispatch_families=dispatch_families)
    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP, structure=reader))
    return reader
