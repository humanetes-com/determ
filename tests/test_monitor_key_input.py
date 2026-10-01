"""Only a keypress becomes a key: every terminal report is parsed to its end.

A terminal's modifier/focus/query-reply sequences (ESC [ 1 ; 5 A, ESC O q,
ESC [ ? 6 ; 1 q) used to leak their tail bytes as bare keys, and a leaked "q"
quits the monitor with no click. The CSI-only decoder that fixed those still
leaked, measured through a pty on 2026-09-30: a legacy mouse report's three
coordinate bytes, an OSC or DCS reply's payload, a bracketed paste, and a
sequence whose ESC arrived 80 ms before its tail — each a stray "q", digit,
"a" or "d" that quit the monitor or switched its view with nobody typing.
"""
import contextlib
import os
import queue
import sys
import time

import pytest

from pipeline_core.monitor.key_input import ESC_WAIT, KeyDecoder, KeyReader

# Every report form the monitor can receive, each carrying bytes that are
# monitor commands (q quits; 1-4, d, e, l, h, f switch views; a, c act).
REPORTS = {
    "legacy mouse click at column 81": b"\x1b[M q2",
    "legacy mouse wheel at column 17": b"\x1b[Ma1+",
    "SGR mouse press/release": b"\x1b[<0;12;5M\x1b[<0;12;5m",
    "urxvt mouse": b"\x1b[32;81;18M",
    "focus in/out": b"\x1b[I\x1b[O",
    "cursor position report": b"\x1b[12;40R",
    "DA1 reply": b"\x1b[?62;22c",
    "DECRQM reply": b"\x1b[?6;1q",
    "OSC 11 colour reply, ST": b"\x1b]11;rgb:1111/2222/3333\x1b\\",
    "OSC title reply, BEL": b"\x1b]l1234 dq\x07",
    "DCS reply": b"\x1bP1$r0m\x1b\\",
    "APC reply": b"\x1b_Gi=1;OK\x1b\\",
    "bracketed paste": b"\x1b[200~q 1 2 d a c\x1b[201~",
    "ctrl-arrow": b"\x1b[1;5C",
    "SS3 keypad": b"\x1bOq",
}


def _decode(data: bytes) -> list[str]:
    d = KeyDecoder()
    return d.feed(data) + d.timeout()


@pytest.mark.parametrize("name", REPORTS)
def test_a_report_yields_no_key(name):
    assert _decode(REPORTS[name]) == []


def test_keys_around_reports_survive_in_order():
    stream = b"2" + b"".join(REPORTS.values()) + b"\x1b[A\x1b[6~\x1bOBx"
    assert _decode(stream) == ["2", "UP", "PGDN", "DOWN", "x"]


def test_a_sequence_split_across_reads_is_still_one_sequence():
    # The tail arrives in a later read (a slow link): no wait expired between.
    d = KeyDecoder()
    keys = d.feed(b"\x1b") + d.feed(b"[12;") + d.feed(b"40R") + d.feed(b"1")
    assert keys == ["1"]


def test_a_lone_escape_is_the_escape_key_only_once_nothing_follows():
    d = KeyDecoder()
    assert d.feed(b"\x1b") == []
    assert d.wait() == ESC_WAIT
    assert d.timeout() == ["ESC"]
    assert d.wait() is None


def test_a_sequence_cut_short_is_dropped_not_decoded():
    d = KeyDecoder()
    assert d.feed(b"\x1b]11;rgb:12") == []
    assert d.timeout() == []
    assert d.feed(b"q") == ["q"]   # the stream is back in ground state


def test_kitty_protocol_keypresses_decode_to_the_key():
    assert _decode(b"\x1b[113u\x1b[27u\x1b[113;5u") == ["q", "ESC"]


def _until_x(reader: KeyReader) -> list[str]:
    keys: list[str] = []
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (not keys or keys[-1] != "x"):
        with contextlib.suppress(queue.Empty):
            keys.append(reader.get(timeout=0.1))
    return keys


def _read_all(payload: bytes, monkeypatch) -> list[str]:
    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r"))
    reader = KeyReader()
    reader.start()
    os.write(w, payload)
    keys = _until_x(reader)
    reader.stop()
    return keys


def test_reader_never_leaks_a_quit_key(monkeypatch):
    keys = _read_all(b"".join(REPORTS.values()) + b"\x1b[A\x1b[6~x", monkeypatch)
    assert keys == ["UP", "PGDN", "x"]


def test_reader_waits_out_a_delayed_sequence_tail(monkeypatch):
    # ESC, then the rest of a cursor report 80 ms later: under ESC_WAIT, so
    # the tail is still read as the sequence it belongs to.
    r, w = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(r, "r"))
    reader = KeyReader()
    reader.start()
    os.write(w, b"\x1b")
    time.sleep(0.08)
    os.write(w, b"[12;40Rx")
    keys = _until_x(reader)
    reader.stop()
    assert keys == ["x"]
    assert reader.recent_input() == b"\x1b[12;40Rx"
