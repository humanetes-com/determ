"""_log_attribution_gap — the give-up-iter diagnostic that disambiguates a
parser gap from an attribution gap when tests fail but no fn was targeted."""
from __future__ import annotations

from types import SimpleNamespace

from pipeline_core.application.reconstruct.phase_c_tdd import _log_attribution_gap


def _cap() -> tuple[SimpleNamespace, list[str]]:
    lines: list[str] = []
    return SimpleNamespace(log=lines.append), lines


def test_gap_logs_names_when_parsed_but_unattributed() -> None:
    deps, lines = _cap()
    _log_attribution_gap(deps, "[stable]", 3, fail_count=54,
                         cur_failed={"a", "b"}, frontier_size=0, kind="no_frontier")
    assert len(lines) == 1
    out = lines[0]
    assert "ATTRIBUTION GAP" in out
    assert "54 test(s) fail" in out and "parsed=2" in out
    assert "candidates(pre-exhaust-filter)=0" in out
    assert "a, b" in out


def test_gap_flags_parser_when_nothing_parsed() -> None:
    deps, lines = _cap()
    _log_attribution_gap(deps, "[stable]", 4, fail_count=54,
                         cur_failed=set(), frontier_size=0, kind="no_frontier")
    assert "parsed=0" in lines[0] and "(none parsed)" in lines[0]


def test_gap_silent_when_no_failures() -> None:
    deps, lines = _cap()
    _log_attribution_gap(deps, "[stable]", 1, fail_count=0,
                         cur_failed=set(), frontier_size=0, kind="runtime")
    assert lines == []
