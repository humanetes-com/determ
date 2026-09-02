"""The contradiction guard reads quoted literals; the pins extraction is
empty for a one-argument `assert!(s.contains("x"))`, which is the shape of
the two convicting tests of run `3003-93e9a9bf`. The call sites hand the
guard the pins plus the asserted-output literals, quoted."""
from __future__ import annotations

from pipeline_core.domain.retro_gate import (
    pins_for_contradiction_guard,
    prescription_removes_pinned_observable,
)


def test_observables_reach_the_guard_as_quoted_literals() -> None:
    text = pins_for_contradiction_guard("", {"Stack backtrace", "\n\nCaused by:"})
    assert '"Stack backtrace"' in text
    assert '"\\n\\nCaused by:"' in text
    guidance = ("In the debug impl the non-alternate arm must not emit a Debug "
                "backtrace and must not re-collect the cause chain.")
    assert prescription_removes_pinned_observable(guidance, text)


def test_pins_alone_are_the_pre_fix_input_and_stay_blind_here() -> None:
    # Negative control: the pins extraction of a one-argument assert is
    # empty, and an empty pins string can fire nothing.
    assert pins_for_contradiction_guard("", ()) == ""
    assert not prescription_removes_pinned_observable("must not emit a backtrace", "")


def test_pins_and_observables_are_joined_and_quotes_escaped() -> None:
    text = pins_for_contradiction_guard('contains("a")', {'say "hi"'})
    assert text.startswith('contains("a") ')
    assert '"say \\"hi\\""' in text
