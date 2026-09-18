"""Regex-free Task2 file-planning helpers, pinned against the regexes they replaced.

- split_api_surface_by_file ← `<FILE path="([^"]+)">(.*?)</FILE>` (DOTALL)
- cargo_entry_point_paths   ← `[lib].*?path` / `[[bin]].*?path` Cargo.toml regexes
"""
from __future__ import annotations

import re

from pipeline_core.domain.cargo_toml import cargo_entry_point_paths
from pipeline_core.domain.llm_output import split_api_surface_by_file

_OLD_FILE_RE = re.compile(r'<FILE path="([^"]+)">(.*?)</FILE>', re.DOTALL)


def _old_split(s: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(0)) for m in _OLD_FILE_RE.finditer(s)]


def test_split_api_surface_matches_old_regex() -> None:
    samples = [
        '<FILE path="src/lib.rs">pub fn a(){}</FILE>',
        'pre <FILE path="a.rs">x</FILE> mid <FILE path="b.rs">y\nz</FILE> end',
        '<FILE path="x">no close tag',
        '<FILE path="">empty path</FILE>',            # `+` rejects empty → no match
        '<FILE path="a.rs">nested <FILE path="b">in</FILE> out</FILE>',
        '<FILE path="a.rs"junk>missing close-bracket</FILE>',
        "no markers", "",
    ]
    for s in samples:
        assert split_api_surface_by_file(s) == _old_split(s), s


def test_split_api_surface_returns_full_block() -> None:
    [(path, block)] = split_api_surface_by_file('<FILE path="m.rs">BODY</FILE>')
    assert path == "m.rs"
    assert block == '<FILE path="m.rs">BODY</FILE>'   # includes the framing


def test_cargo_entry_point_paths_lib_then_bins() -> None:
    toml = ('[package]\nname="x"\n[lib]\npath="src/foo.rs"\n'
            '[[bin]]\nname="b1"\npath="src/bin/b1.rs"\n'
            '[[bin]]\nname="b2"\npath="src/bin/b2.rs"\n')
    assert cargo_entry_point_paths(toml) == ["src/foo.rs", "src/bin/b1.rs", "src/bin/b2.rs"]


def test_cargo_entry_point_paths_empty_when_absent_or_malformed() -> None:
    assert cargo_entry_point_paths('[package]\nname="x"\n') == []
    assert cargo_entry_point_paths("not valid toml {{{") == []
    assert cargo_entry_point_paths('[lib]\nname="x"\n') == []   # lib without path
