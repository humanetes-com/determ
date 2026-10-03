"""L1 e2e harness.

Drive the pipeline's ORCHESTRATION decisions (build-failure routing, keep/revert,
caps, scheduling) with a scripted model + scripted cargo — no live LLM, no real
cargo. These tests retire the "●●● await live run" tag on the logic items, which
react to model+cargo OUTPUT, not model QUALITY.

Seams (the single injection points):
  pipeline._ask_per_fn_body / _ask_raw         ← scripted model
  pipeline._run_cargo / _run_cargo_check_diag  ← scripted cargo
  _APP.frontier_from_diags (install_app_seams) ← scripted attribution (skips the
                                                 extractor binary, absent in CI py)

A "scripted" seam is either a constant, a list (popped left, last value repeats
once exhausted), or a callable evaluated per call. `diag(...)` builds a real
domain Diagnostic so the keep/revert metric (error count) is exercised for real.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pipeline
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.models import Diagnostic


def diag(file: str, line: int = 1, code: str = "E0277",
         message: str = "mismatched types") -> Diagnostic:
    """One structured cargo diagnostic, as `_run_cargo_check_diag` would emit."""
    return Diagnostic(code=code, file=file, line=line, message=message,
                      rendered=f"error[{code}]: {message}\n --> {file}:{line}")


_UNSET = object()


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


def install_scripted_turns(monkeypatch, reply: Callable[[list[dict]], str] | str
                           ) -> list[list[dict[str, Any]]]:
    """Script the transcript transport every fill route talks through
    (`model_interact.ask_turns`) and record each call's whole message list
    — system turn, opening turn, the model's replies, the harness's turns.
    `reply` is a constant or a callable over the messages."""
    import model_interact
    sent: list[list[dict[str, Any]]] = []

    def _turns(messages, **_k):
        sent.append([dict(m) for m in messages])
        return reply(messages) if callable(reply) else reply

    monkeypatch.setattr(model_interact, "ask_turns", _turns)
    # The single-prompt calls (retrospectives, notes) stay on `_ask_raw`;
    # answer them from the same script so nothing reaches a real server.
    monkeypatch.setattr(pipeline, "_ask_raw", lambda prompt, *a, **k: (
        reply([{"role": "user", "content": prompt}]) if callable(reply) else reply))
    return sent


def first_user(messages: list[dict[str, Any]]) -> str:
    """The opening turn of a recorded transcript."""
    return next((str(m.get("content") or "") for m in messages
                 if m.get("role") == "user"), "")


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
    # Port-boundary injection: swap the App's per-fn model seam.
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
    frontier   : Frontier (dict) for the App's `frontier_from_diags` seam — bypasses the extractor.

    Each is a constant / list / callable (see `_Script`).
    """
    scripts = {
        "check_diag": _Script(check_diag),
        "run": _Script(run),
    }

    class _ScriptedCargoRunner:
        """ports.cargo_runner.CargoRunner over the scripts — injected on _APP
        (port boundary) instead of patching the module fns."""

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

        def _implicated_keys(_diags: Any, src_dir: Any, *a: Any,
                             **k: Any) -> frozenset:
            # The peel implicates per BODY — (file, fn_name, one-line
            # container) — while the scripted frontier speaks FnLocator.
            # Resolve containers through the live structure reader (the fake
            # or the real syn extractor, whichever the test installed),
            # mirroring production: a span that resolves to no record
            # implicates nothing.
            from pathlib import Path as _P

            from pipeline_core.domain.attribution import container_key
            from pipeline_core.domain.models import FnLocator
            raw = fscript()
            # A lone locator is a 2-tuple: iterating it yields the file and
            # name strings, not one locator. A Frontier dict iterates as keys.
            if raw is None:
                locs: list = []
            elif isinstance(raw, dict):
                locs = list(raw)
            elif isinstance(raw, FnLocator):
                locs = [raw]
            else:
                locs = list(raw)
            recs = pipeline._APP.structure.enumerate_fns(_P(src_dir)) or []
            return frozenset(
                (loc.file, loc.fn_name,
                 container_key(str(r.get("container", "") or "")))
                for loc in locs
                for r in recs
                if r.get("file") == loc.file
                and r.get("fn_name") == loc.fn_name)

        install_app_seams(monkeypatch,
                          frontier_from_diags=lambda *a, **k: fscript(),
                          implicated_keys_from_diags=_implicated_keys)
    return scripts


def install_app_seams(monkeypatch, **seams: Any) -> None:
    """Swap named App seam fields (attribute_failures, behavior_frontier,
    collect_impl_blocks, compile_error_frontier, extract_tdd_frontier,
    fn_container_index, frontier_from_diags, u2_test_gate, ...) for scripted
    callables — the port-boundary form of the old `setattr(pipeline, "_x")`
    module-fn patches. Every consumer resolves through the live `pipeline._APP`
    (build_app's `app_ref`), so one swap covers all call sites."""
    import dataclasses as _dc
    monkeypatch.setattr(pipeline, "_APP", _dc.replace(pipeline._APP, **seams))


def install_scripted_attribution(monkeypatch, frontier: Any, kind: str = "scripted") -> _Script:
    """Swap the App's `attribute_failures` seam (TEST-mode fix path) →
    (frontier, kind), bypassing the test-reverse-map + extractor. `frontier`
    is constant / list / callable."""
    fscript = _Script(frontier)

    def _attr(*_a: Any, **_k: Any) -> AttributionResult:
        # Scripted frontier = direct evidence: these fakes drive routes that
        # expect the attributed test to be able to reject the fill.
        fr = fscript()
        return AttributionResult(fr, FailureKind(kind) if kind in tuple(FailureKind)
                                 else FailureKind.BEHAVIOR, dict(fr))

    install_app_seams(monkeypatch, attribute_failures=_attr)
    return fscript


class FakeStructureReader:
    """A `ports.structure.StructureReader` over scripted values.

    Each field is a value or a callable evaluated per call (same `_Script`
    semantics as the model/cargo seams). Inject via
    :func:`install_fake_extractor` — the port-boundary replacement for
    monkeypatching `pipeline._enumerate_*_via_extractor` module fns."""

    def __init__(self, *, fns: Any = None, types: Any = None, mods: Any = None,
                 consts: Any = None, dispatch_families: Any = None,
                 uses: Any = None) -> None:
        self._fns = _Script(fns) if callable(fns) else fns
        self._types = _Script(types) if callable(types) else types
        self._mods = mods
        self._uses = uses
        self._consts = consts
        self._families = dispatch_families

    def enumerate_fns(self, src_dir):  # noqa: ANN001, ANN201 — port signature
        return self._fns(src_dir) if isinstance(self._fns, _Script) else self._fns

    def enumerate_types(self, src_dir):  # noqa: ANN001, ANN201
        return (self._types(src_dir) if isinstance(self._types, _Script)
                else self._types)

    def enumerate_mods(self, src_dir):  # noqa: ANN001, ANN201
        return self._mods

    def enumerate_uses(self, src_dir):  # noqa: ANN001, ANN201
        return self._uses

    def enumerate_consts(self, src_dir):  # noqa: ANN001, ANN201
        return self._consts

    def enumerate_dispatch_families(self, src_dir):  # noqa: ANN001, ANN201
        return self._families

    def enumerate_doc_items(self, src_dir):  # noqa: ANN001, ANN201
        # None = extractor unavailable: the doc-test count takes its text scan.
        return None

    def parse_ok(self, path):  # noqa: ANN001, ANN201 — splice gate
        # Fail-open in tests: no real syn binary here, and the scripted fixtures
        # are trusted, so the parse gate never blocks a splice (mirrors the
        # production adapter's fail-open on an unavailable extractor).
        return True


def install_fake_extractor(monkeypatch, *, fns: Any = None, types: Any = None,
                           mods: Any = None, consts: Any = None,
                           dispatch_families: Any = None,
                           uses: Any = None) -> FakeStructureReader:
    """Swap the App's syn-extractor structure provider for a scripted fake —
    the port-boundary injection (`build_app`-style) replacing per-shim
    monkeypatches of `pipeline._enumerate_fns_via_extractor` et al. All shims
    and the fill engine resolve structure through `pipeline._APP` at call
    time, so one swap covers every read."""
    import dataclasses as _dc
    reader = FakeStructureReader(fns=fns, types=types, mods=mods, consts=consts,
                                 dispatch_families=dispatch_families, uses=uses)
    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP, structure=reader))
    return reader


def install_cargo_runner(monkeypatch, run: Callable[..., Any],
                         check_diag: Callable[..., Any]) -> None:
    """Swap the App's cargo runner for arbitrary callables: the
    port-boundary form of `setattr(pipeline, "_run_cargo…")`. `run` gets the
    (cmd, cwd, env, timeout) call and returns (rc, text); `check_diag`
    returns (rc, rendered, [Diagnostic])."""
    import dataclasses as _dc

    class _Runner:
        def run(self, cmd, cwd, env, timeout=180):  # noqa: ANN001, ANN201
            return run(cmd, cwd, env, timeout)

        def check_diag(self, cmd, cwd, env, timeout=180):  # noqa: ANN001, ANN201
            return check_diag(cmd, cwd, env, timeout)

    monkeypatch.setattr(pipeline, "_APP",
                        _dc.replace(pipeline._APP, cargo_runner=_Runner()))


def install_cluster_scan(monkeypatch, *, stub_records: Any = _UNSET,
                         impl_blocks: Any = _UNSET) -> None:
    """Script the App's cluster-scoped structure queries: stub-record
    scan and impl-block collection. Value-or-callable semantics like the other
    seams; omit a kwarg to keep the production behavior."""
    import dataclasses as _dc
    fields: dict[str, Any] = {}
    if stub_records is not _UNSET:
        s = _Script(stub_records) if callable(stub_records) else None
        fields["scan_stub_records_for_cluster"] = (
            (lambda *a, **k: s(*a, **k)) if s else (lambda *a, **k: stub_records))
    if impl_blocks is not _UNSET:
        s2 = _Script(impl_blocks) if callable(impl_blocks) else None
        fields["collect_impl_blocks_for_cluster"] = (
            (lambda *a, **k: s2(*a, **k)) if s2 else (lambda *a, **k: impl_blocks))
    monkeypatch.setattr(pipeline, "_APP", _dc.replace(pipeline._APP, **fields))
