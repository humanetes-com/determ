"""Integration: a file request longer than the cap is reachable to its end,
and an identical request is not paid for twice.

Real file readers over a task2a-shaped workspace (the cluster tree whose `tests`
is a symlink into the project, read through `reconstruction_file_reader`), the
same answerer object the slice session is handed (`make_request_answerer`), and
the real follow-up renderer. No model, no cargo — runs in the default suite.

The class: a capped answer used to be `text[:cap]` with a truncated flag and no
address past it. Run `603120-24aabfc5` (2026-09-16) asked twice for a
55,930-char file, got the same first 40,000 chars both times, and the second
copy spent the session's transcript bound. The workspace below is invented;
only the file's length and the layout matter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
    reconstruction_file_reader,
)
from pipeline_core.domain.context_pull import extract_context_pull

pytestmark = pytest.mark.integration

_CONTINUE = "To read them, reply "


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "common.rs").write_text("pub fn helper() {}\n")
    (project / "src").mkdir()
    (project / "src" / "lib.rs").write_text("// stripped shell\n")
    cluster = project / ".skeleton-filled" / "main"
    (cluster / "src").mkdir(parents=True)
    body = "".join(f"pub fn filler_{i:05d}() -> u32 {{ {i} }}\n"
                   for i in range(2_500))
    (cluster / "src" / "big.rs").write_text(body + "pub fn far_end() {}\n")
    (cluster / "tests").symlink_to(project / "tests")
    return cluster, project


def _answerer(cluster: Path, project: Path, max_pulls: int = 8):
    logs: list[str] = []
    sources = PullSources(
        read_file=reconstruction_file_reader(cluster, project),
        find_tests=lambda _n: "", type_def=lambda _n: "",
        full_error=lambda: "")
    return make_request_answerer(sources, log=logs.append,
                                 emit=lambda *_a, **_k: None,
                                 max_pulls=max_pulls), logs


def test_a_file_over_the_cap_is_read_to_its_end_by_continuations(
        tmp_path: Path) -> None:
    cluster, project = _workspace(tmp_path)
    assert (cluster / "src" / "big.rs").stat().st_size > 40_000
    answer, logs = _answerer(cluster, project)
    reply = '<REQUEST kind="file" arg="src/big.rs"/>'
    transcript = ""
    for _ in range(8):
        add = answer(reply) or ""
        transcript += add
        if _CONTINUE not in add:
            break
        nxt = extract_context_pull(add[add.index(_CONTINUE):])
        assert nxt is not None and nxt.from_line > 1
        reply = nxt.tag()
    assert "pub fn far_end() {}" in transcript
    assert transcript.count("pub fn filler_01234()") == 1
    assert any("truncated" in ln for ln in logs)


def test_a_repeated_request_is_not_sent_twice(tmp_path: Path) -> None:
    cluster, project = _workspace(tmp_path)
    answer, logs = _answerer(cluster, project)
    req = '<REQUEST kind="file" arg="src/big.rs"/>'
    first = answer(req) or ""
    second = answer(req) or ""
    assert "pub fn filler_00000()" in first
    assert "pub fn filler_00000()" not in second
    assert len(second) < 1_000
    assert "repeat — not re-sent" in logs[-1]


def test_the_oracle_tests_still_resolve_through_the_symlink(tmp_path: Path) -> None:
    cluster, project = _workspace(tmp_path)
    answer, _ = _answerer(cluster, project)
    out = answer('<REQUEST kind="file" arg="tests/common.rs"/>') or ""
    assert "pub fn helper() {}" in out and _CONTINUE not in out


def test_negative_control_head_cut_never_reaches_the_end(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fix cap injected: every answer is the file's head, whatever
    window was asked for, so no request sequence reaches the end."""
    from pipeline_core.application.reconstruct import pull_resolver
    from pipeline_core.domain.context_pull import LineWindow

    monkeypatch.setattr(pull_resolver, "window_lines",
                        lambda text, _start, cap: LineWindow(text[:cap], 1, 1, 2))
    cluster, project = _workspace(tmp_path)
    answer, _ = _answerer(cluster, project)
    seen = "".join(answer(r) or "" for r in (
        '<REQUEST kind="file" arg="src/big.rs"/>',
        '<REQUEST kind="file" arg="src/big.rs" from_line="2"/>',
        '<REQUEST kind="file" arg="src/big.rs" from_line="2400"/>'))
    assert "pub fn far_end() {}" not in seen
