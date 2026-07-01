"""C1: FileRunRegistry (live-run registry) + crate_display_name + log trim."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_core.adapters.log_rotation import trim_head
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


def test_trim_head_noop_when_small(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("short\n", encoding="utf-8")
    trim_head(str(f), 1024)
    assert f.read_text() == "short\n"


def test_trim_head_drops_head_at_newline(tmp_path: Path) -> None:
    f = tmp_path / "log.txt"
    f.write_text("".join(f"line{i}\n" for i in range(1000)), encoding="utf-8")
    trim_head(str(f), 200)
    out = f.read_text()
    assert len(out.encode()) <= 200
    assert out.startswith("line")  # cut at a line boundary, not mid-line


def test_trim_head_missing_file_is_safe(tmp_path: Path) -> None:
    trim_head(str(tmp_path / "nope.txt"), 100)  # no raise
