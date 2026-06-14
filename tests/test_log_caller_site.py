"""caller_site (pipeline_core/log.py) — frame-walk tag used to attribute llm.request."""
from __future__ import annotations

from pipeline_core import log


def test_caller_site_reports_calling_frame() -> None:
    def issuer() -> str:
        return log.caller_site()  # depth=2 → this test fn (issuer's caller)

    site = issuer()
    assert "test_caller_site_reports_calling_frame" in site
    assert "test_log_caller_site.py:" in site


def test_caller_site_depth_zero_is_self() -> None:
    site = log.caller_site(0)
    assert site.startswith("caller_site (log.py:")


def test_caller_site_out_of_range_is_question_mark() -> None:
    assert log.caller_site(9999) == "?"
