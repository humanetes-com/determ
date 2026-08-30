"""Walk: run `500301-6a66bf86` (2026-08-15, cluster `stable`) — the sweep that
localized the crash, graded the component that owns it, and was refused by a
budget two operator SIGTERMs had spent.

The recording, one iteration: `pass=39 fail=48 oracle=87 (improved=False,
stall=1/3); frontier=61 fn(s) (merged), 0 skipped as exhausted`, 30 expected
executions counted as failing across 3 crashed binaries. Everything upstream of
the aim worked, and this is the run that proves it: the ASAN sweep localized
`unittests src/lib.rs::error::gen_tests_1::test_construct_from_display` as a
`heap-use-after-free` and blamed `FnLocator(src/error.rs, drop)` with
`provenance=frame_derived` — an in-crate ORIGIN, not the crashing test's own
frames — and the aim graded the 34-name construct/vtable component OWNS: `the
observed fault is inside code 1 component(s) declare`.

Then it refused: `2 consecutive mechanical failure(s) already refunded here —
the model attempt is not what is failing`. Neither of those two was a model
attempt. `reconcile_interrupted_attempts` books a refund for an attempt whose
process did not come back, and charges it as mechanical; runs
`462635-c8369188` (SIGTERM at 56m56s) and `496645-9b9de7e1` (SIGTERM at 24s)
were both stopped by the operator while that component was authoring, so the
store reached `mech:<mega key>: 2` at a cap of 2 with every other budget clear
(`attempts[<mega>] = 0` of 2, `rejected:<mega> = 2` of 3 and epoch-stale, no
`sig:` refutation). `mech:` has exactly one clear in the codebase — the
accepted-re-author path, downstream of this refusal — so the lock was permanent
for every later run.

What the fix makes REACHABLE (never a score): a mechanical streak is stamped
with the build that earned it, and a streak from another build does not refuse.
The live store carries no stamp at all, so the first run under new code reaches
the component again — with its two destruction attempts intact.

Negative control: the identical store read by a caller that names no build,
which is the pre-fix reader. The recorded decision must reproduce — refused,
nothing charged, nothing committed, and the reason printed on its own line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.models import FnLocator, FnRecord
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# 87 measured + 20 never reported = the 107-test full oracle of the run's
# `iter 1 binaries` line.
_INVENTORY = 107
_CRASHED_TEST = "test_construct_from_display"
_DROP = FnLocator("src/error.rs", "drop")
# The build the run itself ran under (`code=459594789b98` on its banner), and
# the one before it (`27883c3b25e4`, run `462635-c8369188`).
_THIS_BUILD = "459594789b98"
_REVERSE_MAP = {f"t{i}": {("src/error.rs", "new")} for i in range(80, 108)}


def _asan_site():
    """The recorded site (`phase_c.crash.localized`, iter 1): ASAN, so origin
    frames ARE reported, and the innermost of them is in-crate — which is what
    makes `blamed_locator` a real fn the crate declares rather than the
    generated test file the access frames name."""
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    return CrashSite(
        binary_label="unittests src/lib.rs",
        test_name=f"error::gen_tests_1::{_CRASHED_TEST}",
        signal="AddressSanitizer: heap-use-after-free",
        frames=(CrashFrame(file_rel="src/error.gen_tests_1.rs", line=17,
                           fn_name=_CRASHED_TEST, symbol=_CRASHED_TEST),),
        origin_frames=(CrashFrame(file_rel="src/error.rs", line=301,
                                  fn_name="drop", symbol="drop"),),
        origin_capable=True,
        raw_report=("==500301==ERROR: AddressSanitizer: heap-use-after-free\n"
                    "  freed by thread T0 here:\n    #0 <anyhow::Error>::drop"))


def _mega_records(*, stub_drop: bool = False):
    """The component's own bodies as the aim grades them. `drop` is not one of
    the 34 NAMES — it grades OWNS through its container, `impl Drop for Error`,
    whose self type is. That indirection is the whole reason the live line
    reads `inside code 1 component(s) declare` for a fn no component key
    spells.

    The recorded drop was filled. A filled-body frame blame is a fill, not a
    layout re-author, and is pinned in test_retrigger_layout. Mechanical-
    streak reachability is the class that remains after that gate, so those
    tests pass stub_drop=True to keep the layout route open.
    """
    body = ("unimplemented!()" if stub_drop
            else "unsafe { (vtable(self.inner.ptr).object_drop)(self.inner) }")
    return [
        FnRecord(file="error.rs", fn_name="drop", container="impl Drop for Error",
                 sig_text="fn drop(&mut self)",
                 body_text=body,
                 has_unimplemented_body=stub_drop),
        FnRecord(file="error.rs", fn_name="construct",
                 sig_text=("unsafe fn construct<E>(error: E, vtable: &'static "
                           "ErrorVTable, backtrace: Option<Backtrace>) -> Self"),
                 body_text="let inner = Box::new(ErrorImpl { vtable, backtrace });"),
    ]


def _recording_retrigger():
    """The recorded outcome — refused, with the reason the run printed — and
    the evidence the driver handed it, captured for the aim below."""
    from pipeline_core.application.retrigger_layout import RetriggerResult
    seen: list[Any] = []

    def _retrig(pd, mod, cl, sd, output, crash_evidence=None, **_kw):
        seen.append(crash_evidence)
        return RetriggerResult(fired=False, reason="mechanical budget spent")
    return _retrig, seen


def _replay(monkeypatch, tmp_path):
    walk = load_walk("500301-6a66bf86")
    retrig, seen = _recording_retrigger()
    events: list[tuple[str, dict]] = []
    logs = replay(walk, monkeypatch, tmp_path, events=events,
                  retrigger=retrig, reverse_map=_REVERSE_MAP,
                  localize=lambda *a, **k: (_asan_site(),),
                  inventory=_INVENTORY)
    return logs, events, seen


def _aim(evidence, attempts: dict[str, int], code_id: str | None,
         *, stub_drop: bool = False):
    """Drive the REAL `retrigger_layout` with the evidence the driver built and
    the store the live run read. Authoring is stubbed to REJECTED, so `fired`
    means the walk REACHED the component and spent an attempt on it — the
    decision under test — and no assertion here depends on what a model
    would have written."""
    from pipeline_core.application.retrigger_layout import retrigger_layout
    from tests.test_retrigger_layout import _CRATE, _mega_fakes
    f = _mega_fakes(compiles=False, attempts=dict(attempts), code_id=code_id,
                    fn_records=_mega_records(stub_drop=stub_drop))
    return retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                            f.deps(), crash_evidence=evidence), f


def _locked_store() -> dict[str, int]:
    """The live store's row for the mega component at the moment of the
    refusal: `mech:<34-name key>: 2`, and nothing else — the destruction cap
    was at 0 of 2, both attempts having been refunded."""
    from tests.test_retrigger_layout import _MEGA_KEY
    return {f"mech:{_MEGA_KEY}": 2}


def test_the_sweep_hands_the_aim_an_in_crate_origin(
        tmp_path: Path, monkeypatch) -> None:
    """Upstream of the lock, and it worked: the aim is handed `src/error.rs::
    drop` as ORIGIN blame, not the crashing test's `src/error.gen_tests_1.rs`
    frame — the access path stays in the evidence keys, where it belongs."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    handed = [e for e in seen if e is not None]
    assert handed, "the fire gate was never reached"
    assert handed[0].blamed_fns == frozenset({_DROP})
    assert not any(k.file.endswith("gen_tests_1.rs")
                   for k in handed[0].blamed_fns)


def test_negative_control_the_locked_store_refuses_the_graded_component(
        tmp_path: Path, monkeypatch) -> None:
    """The recorded drop was filled. Under new code that evidence never
    opens a re-author — filled-body frame blame is a fill. The mechanical
    cap is not consulted. Negative control of the old lockout is the
    stub-drop tests below, where the layout route still opens."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    res, f = _aim(next(e for e in seen if e is not None), _locked_store(), None)

    assert not res.fired
    assert res.reason == "filled-body frame blame"
    assert any("names a FILLED body" in ln for ln in f.logs)
    assert not f.committed
    assert not any("mechanical failure(s) already refunded" in ln
                   for ln in f.logs)


def test_a_streak_no_build_has_earned_reaches_the_component_again(
        tmp_path: Path, monkeypatch) -> None:
    """Reachability, which is all a walk may assert: the same evidence and the
    same two `mech:` rows, read by a build that has not failed here, charges
    attempt 1 against the component the crash owns."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    res, f = _aim(next(e for e in seen if e is not None), _locked_store(),
                  _THIS_BUILD, stub_drop=True)

    assert res.fired, "the aim was still refused by a streak from another build"
    assert f.committed and "attempt 1" in f.committed[0]
    assert "object_reallocate_boxed" in f.committed[0]   # the mega component


def test_an_operator_stop_no_longer_spends_the_budget(
        tmp_path: Path, monkeypatch) -> None:
    """And the charge that filled the cap is not booked again. The store as it
    stood when run `462635-c8369188` was stopped mid-authoring — one attempt
    charged, its marker and its pid on file — settled against the death record
    that names that pid: the attempt comes back, the budget does not move."""
    _logs, _events, seen = _replay(monkeypatch, tmp_path)
    from tests.test_retrigger_layout import _MEGA_KEY
    store = {_MEGA_KEY: 1, "inflight:" + _MEGA_KEY: 1,
             "inflightpid:" + _MEGA_KEY: 462635}
    from pipeline_core.application.retrigger_layout import retrigger_layout
    from tests.test_retrigger_layout import _CRATE, _mega_fakes
    f = _mega_fakes(compiles=False, attempts=store, code_id=_THIS_BUILD,
                    fn_records=_mega_records(stub_drop=True),
                    stopped_pids={462635})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=next(
                               e for e in seen if e is not None))

    assert res.fired
    assert "mech:" + _MEGA_KEY not in f.attempt_saves[0]
    assert any("NOT counted as a mechanical failure" in ln for ln in f.logs)
