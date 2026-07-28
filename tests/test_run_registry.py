"""C1: FileRunRegistry (live-run registry) + crate_display_name + log trim."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_core.adapters.log_rotation import archived_logs, rotate
from pipeline_core.adapters.run_registry import FileRunRegistry, RunRegistryEntry
from pipeline_core.domain.crate_name import crate_display_name


def test_run_registry_entry_as_dict_shape() -> None:
    # as_dict must reproduce the historical 12-key registry record, in order, so
    # the persisted JSON monitor.py reads stays byte-stable.
    e = RunRegistryEntry(
        pid=42, project="anyhow", project_dir="/tmp/anyhow", pipeline_log="/l/p.log",
        run_log=None, stdout_log="/l/o.log", stream_log="/l/s.log",
        events_log="/l/e.jsonl", guard_log="/l/g.log", argv=["--repo", "x"],
        cwd="/w", started_at=1700,
    )
    d = e.as_dict()
    assert list(d.keys()) == [
        "pid", "project", "project_dir", "pipeline_log", "run_log", "stdout_log",
        "stream_log", "events_log", "guard_log", "argv", "cwd", "started_at"]
    assert d["pid"] == 42 and d["run_log"] is None and d["argv"] == ["--repo", "x"]
    # registry accepts the serialized dict unchanged (register keys on "pid").
    assert d["pid"] == e.pid


def test_crate_display_name_strips_known_suffixes() -> None:
    assert crate_display_name("anyhow-master") == "anyhow"
    assert crate_display_name("argh-main") == "argh"
    assert crate_display_name("foo-trunk") == "foo"
    assert crate_display_name("bare") == "bare"


def test_register_writes_entry_and_prunes_dead_pids(tmp_path: Path) -> None:
    reg_path = tmp_path / "runs.json"
    reg_path.write_text(json.dumps([{"pid": 999999}]), encoding="utf-8")  # dead pid
    FileRunRegistry(reg_path).register({"pid": os.getpid(), "project": "p"})
    data = json.loads(reg_path.read_text())
    pids = [r["pid"] for r in data]
    assert os.getpid() in pids
    assert 999999 not in pids  # dead pid pruned


def test_register_replaces_own_duplicate(tmp_path: Path) -> None:
    reg_path = tmp_path / "runs.json"
    reg = FileRunRegistry(reg_path)
    reg.register({"pid": os.getpid(), "project": "old"})
    reg.register({"pid": os.getpid(), "project": "new"})
    data = json.loads(reg_path.read_text())
    mine = [r for r in data if r["pid"] == os.getpid()]
    assert len(mine) == 1 and mine[0]["project"] == "new"


def test_register_tolerates_malformed_existing(tmp_path: Path) -> None:
    reg_path = tmp_path / "runs.json"
    reg_path.write_text("not json", encoding="utf-8")
    FileRunRegistry(reg_path).register({"pid": os.getpid()})
    assert isinstance(json.loads(reg_path.read_text()), list)


def test_unregister_removes_pid(tmp_path: Path) -> None:
    reg_path = tmp_path / "runs.json"
    reg = FileRunRegistry(reg_path)
    reg.register({"pid": os.getpid()})
    reg._unregister(os.getpid())
    assert json.loads(reg_path.read_text()) == []


def test_rotate_noop_when_small(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("short\n", encoding="utf-8")
    rotate(str(f), 1024)
    assert f.read_text() == "short\n"
    assert archived_logs(str(f)) == []


def test_rotate_keeps_every_line_it_moves_out(tmp_path: Path) -> None:
    # The head must survive rotation. The old trim deleted it in place, so a
    # plateau spanning several runs was unreadable — four of the five runs had
    # been overwritten while the pipeline was still appending to the same file.
    f = tmp_path / "log.txt"
    body = "".join(f"line{i}\n" for i in range(1000))
    f.write_text(body, encoding="utf-8")
    rotate(str(f), 200)
    assert f.read_text() == ""                      # active file starts fresh
    assert Path(f"{f}.1").read_text() == body       # nothing lost
    assert archived_logs(str(f)) == [f"{f}.1"]


def test_rotate_shifts_older_archives_up_without_overwriting(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    for round_n in range(3):
        f.write_text(f"round{round_n}\n" * 100, encoding="utf-8")
        rotate(str(f), 50)
    # .1 is the NEWEST archive, .3 the oldest; ascending shifts would have
    # clobbered .2 with .1 before .2 itself moved.
    assert Path(f"{f}.1").read_text().startswith("round2")
    assert Path(f"{f}.2").read_text().startswith("round1")
    assert Path(f"{f}.3").read_text().startswith("round0")
    assert archived_logs(str(f)) == [f"{f}.1", f"{f}.2", f"{f}.3"]


def test_rotate_missing_file_is_safe(tmp_path: Path) -> None:
    rotate(str(tmp_path / "nope.txt"), 100)  # no raise
    assert archived_logs(str(tmp_path / "nope.txt")) == []


def test_rotate_keeps_an_already_open_appender_writing_to_the_active_file(
        tmp_path: Path) -> None:
    # THE case that matters and the one the file-based tests above cannot see:
    # the process appending to this log is not the one rotating it. The launcher
    # redirects the pipeline's stdout here and that descriptor can never reopen,
    # so rotation must preserve the INODE. Renaming the active file instead
    # leaves the appender bound to the archive: the active log stays empty
    # forever, never trips the size check again, and the archive grows unbounded
    # — the exact inverse of what rotation is for.
    f = tmp_path / "log.txt"
    f.write_text("old\n" * 100, encoding="utf-8")
    with open(f, "a") as writer:          # the launcher's `>>` descriptor
        rotate(str(f), 50)
        writer.write("after-rotation\n")
        writer.flush()

    assert f.read_text() == "after-rotation\n"      # live lines stay live
    assert Path(f"{f}.1").read_text() == "old\n" * 100
    assert "after-rotation" not in Path(f"{f}.1").read_text()


def test_rotate_leaves_no_nul_padding_for_an_open_appender(tmp_path: Path) -> None:
    # An O_APPEND descriptor recomputes its offset to EOF on every write, so an
    # in-place truncation cannot strand it past the end. Pinned because the
    # padding this rules out is silent: the file reads as valid text to `tail`
    # while every tool that splits it into lines sees one giant NUL blob.
    f = tmp_path / "log.txt"
    f.write_text("x" * 5000 + "\n", encoding="utf-8")
    with open(f, "a") as writer:
        rotate(str(f), 100)
        writer.write("clean\n")
        writer.flush()
    assert b"\x00" not in f.read_bytes()


def test_launchers_open_the_rotated_log_in_append_mode() -> None:
    # Cross-language half of the invariant above: rotation truncates in place,
    # which is only safe for O_APPEND writers. `>` would keep its offset and pad
    # the log with NULs on the first rotation. Nothing else enforces this — the
    # Python side cannot observe how the shell opened the file.
    root = Path(__file__).resolve().parent.parent
    for script in ("continue.sh", "restart.sh"):
        body = (root / script).read_text(encoding="utf-8")
        launches = [ln for ln in body.splitlines()
                    if "nohup" in ln and "pipeline.py" in ln]
        assert launches, f"{script}: no pipeline launch line found"
        for ln in launches:
            assert ">> \"$LOG_DIR/pipeline_anyhow.log\"" in ln, (
                f"{script}: run log must be opened with >> (append), got: {ln.strip()}")
