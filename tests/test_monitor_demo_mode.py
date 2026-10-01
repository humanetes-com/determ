"""Demo mode is never a surprise: it says so on screen and one key stops it.

The monitor cycled Monitor -> Pipeline -> Stream every few seconds with nothing
on screen saying why and no key to stop it (2026-09-30).
"""
from __future__ import annotations

import monitor as m
from pipeline_core.monitor.render import plain


def test_banner_names_the_mode_and_the_key_that_stops_it(monkeypatch):
    monkeypatch.setattr(m, "_DEMO", True)
    monkeypatch.setattr(m, "_action_msg", "")
    msg = plain(m._bottom_bar()[0])
    assert "[d]emo mode enabled" in msg and "press d to stop" in msg


def test_no_banner_when_demo_is_off(monkeypatch):
    monkeypatch.setattr(m, "_DEMO", False)
    monkeypatch.setattr(m, "_action_msg", "")
    assert "[d]emo mode" not in plain(m._bottom_bar()[0])
    assert "[d]emo" in plain(m._bottom_bar()[1])   # discoverable while off


def test_d_toggles_demo(monkeypatch):
    monkeypatch.setattr(m, "_DEMO", True)
    m._apply_key("d")
    assert m._DEMO is False
    m._apply_key("d")
    assert m._DEMO is True


def test_choosing_a_view_yourself_ends_demo_mode(monkeypatch):
    monkeypatch.setattr(m, "_DEMO", True)
    m._apply_key("2")
    assert m._DEMO is False and m._view == "pipeline"


def test_e_opens_the_phase_view_and_closes_it(monkeypatch):
    monkeypatch.setattr(m, "_DEMO", False)
    monkeypatch.setattr(m, "_view", "monitor")
    m._apply_key("e")
    assert m._view == "phases"
    m._apply_key("e")
    assert m._view == "monitor"
