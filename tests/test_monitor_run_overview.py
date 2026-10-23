"""The health tab's run overview: status words, the start time in the machine's
zone, and the monitor following a status change (the finished run that showed
no sign it was over)."""

from datetime import UTC, timedelta, timezone

import pytest

from pipeline_core.monitor import run_overview as ro

START = "  PIPELINE STARTED — 2026-10-22 09:11:35 UTC  pipeline.py=1  run_id=1-a"
SUCCESS = "SUCCESS: all tests passed on iteration 1."


def test_the_last_run_is_the_one_read():
    lines = ["x", START, "a", "  PIPELINE STARTED — 2026-10-23 01:00:00 UTC", "b"]
    assert ro.last_run_segment(lines)[-1] == "b" and len(ro.last_run_segment(lines)) == 2
    # a rotated-out header: the whole log is the run's, never "not started"
    assert ro.last_run_segment(["tail", SUCCESS]) == ["tail", SUCCESS]
    assert ro.last_run_segment([]) == []


def test_status_words():
    seg = [START, "work"]
    assert ro.status_of(True, seg) == ro.RUNNING
    assert ro.status_of(False, seg) == ro.STOPPED
    assert ro.status_of(False, [*seg, SUCCESS]) == ro.FINISHED
    assert ro.status_of(False, []) == ro.NOT_STARTED


def test_a_success_in_an_earlier_run_does_not_finish_the_last_one():
    lines = [START, SUCCESS, START, "work"]
    assert ro.status_of(False, ro.last_run_segment(lines)) == ro.STOPPED


def test_start_is_shown_in_the_given_zone():
    seg = [START]
    assert ro.run_started(seg, timezone(timedelta(hours=2))).startswith("2026-10-22 11:11:35")
    assert ro.run_started(["garbage"]) == ""
    # header rotated out: the run's first recorded event gives the start
    assert ro.run_started(["tail"], UTC, "2026-10-22T09:11:35.300+00:00") == "2026-10-22 09:11:35 UTC"


def test_overview_names_the_four_facts():
    out = ro.overview_lines("finished", [START], "/w/anyhow", "https://x/y", UTC)
    assert out[:4] == ["Status:         finished", "Repository:     https://x/y",
                       "Started:        2026-10-22 09:11:35 UTC", "Generated code: /w/anyhow"]


def test_reader_rereads_only_when_the_log_moved(tmp_path):
    log = tmp_path / "p.log"
    log.write_text(START + "\nwork\n")
    r = ro.StatusReader(log)
    assert r.read(False)[0] == ro.STOPPED
    log.write_text(START + "\nwork\n" + SUCCESS + "\n")
    assert r.read(False)[0] == ro.FINISHED
    assert r.read(True)[0] == ro.RUNNING


def test_reader_without_a_log(tmp_path):
    assert ro.StatusReader(tmp_path / "none.log").read(False)[0] == ro.NOT_STARTED


@pytest.mark.parametrize("seq,expect", [
    ([ro.RUNNING, ro.RUNNING], False),
    ([ro.RUNNING, ro.FINISHED], True),
    ([ro.STOPPED, ro.RUNNING], True),
])
def test_status_change_is_reported_once_and_never_on_the_first_reading(tmp_path, seq, expect):
    running = {"v": False}
    feed = ro.OverviewFeed(tmp_path / "none.log", str(tmp_path / "events.jsonl"),
                           lambda: None, lambda: running["v"])
    statuses = iter(seq)
    feed.read = lambda: (next(statuses), [])   # type: ignore[method-assign]
    assert feed.status_changed() is False
    assert feed.status_changed() is expect


def test_feed_reads_real_inputs(tmp_path):
    log = tmp_path / "p.log"
    log.write_text(START + "\n" + SUCCESS + "\n")
    feed = ro.OverviewFeed(log, str(tmp_path / "events.jsonl"), lambda: str(tmp_path), lambda: False)
    status, lines = feed.read()
    assert status == ro.FINISHED and lines[3] == f"Generated code: {tmp_path}"
    assert feed.status_changed() is False
