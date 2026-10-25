"""A crash is localized in the build world it crashed in.

The one-tree sweep runs every world in one joined capture
(`test_results.join_world_captures`), so the same binary labels appear once
per world. Before 2026-10-25 every re-run of a crash took the home env, the
one sanitized build was the home world's (a test only another world builds
was not in it), crashes were keyed by the bare label, and a home report
superseded another world's debugger site of the same label. Found when the
reading of run `1700492-f6c4d66c` (2026-10-24) walked the localizer's three
callers: all three passed the home env. Invented captures.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.asan_sweep import asan_target_dir_name
from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    crash_keys,
    localize_crashes,
)
from pipeline_core.domain.test_results import (
    join_world_captures,
    parse_binary_results,
)

_CLEAN = """\
    Running tests/probe.rs (target/debug/deps/probe-111)
running 1 test
test reads_value ... ok
test result: ok. 1 passed; 0 failed; 0 ignored
"""

_CRASHED = """\
    Running tests/probe.rs (target/worlds/alt/debug/deps/probe-222)
running 2 tests
test reads_value ... ok
test drops_twice ...
"""

_ASAN = """\
    Running tests/probe.rs (target-asan-alt/debug/deps/probe-999)
running 2 tests
test drops_twice ... ==7==ERROR: AddressSanitizer: heap-use-after-free on address 0x7f
READ of size 8 at 0x7f thread T2
    #0 0x55a in <crate::Thing as core::ops::drop::Drop>::drop /w/src/thing.rs:40:5
    #1 0x55b in probe::drops_twice /w/tests/probe.rs:12:5
freed by thread T2 here:
    #0 0x55c in free
    #1 0x55d in crate::thing::release /w/src/thing.rs:22:9
    #2 0x55e in probe::drops_twice /w/tests/probe.rs:11:5
"""

_HOME_ENV = {"WORLD": "base"}
_ALT_ENV = {"WORLD": "alt", "RUSTFLAGS": "--cfg alt"}


def _joined(base: str, alt: str) -> str:
    return join_world_captures([("base", base), ("alt", alt)])


def _deps(gdb_result, asan_result):
    calls: dict[str, list] = {"gdb": [], "asan": []}

    def _gdb(exe, cwd, env, **kw):
        calls["gdb"].append((Path(exe).name, env))
        return gdb_result

    def _asan(cmd, cwd, env, **kw):
        calls["asan"].append((kw.get("world"), env))
        return asan_result

    return CrashLocalizerDeps(gdb_localize=_gdb, asan_sweep=_asan,
                              log=lambda _m: None), calls


def _world_env(world: str) -> dict[str, str] | None:
    return {"base": _HOME_ENV, "alt": _ALT_ENV}.get(world)


def test_the_crash_of_another_world_is_rerun_and_sanitized_in_that_world() -> None:
    frames = [{"func": "crate::thing::{impl#1}::drop", "file": "src/thing.rs", "line": 40}]
    deps, calls = _deps(("SIGSEGV", frames), (101, _ASAN))
    sites = localize_crashes(deps, _joined(_CLEAN, _CRASHED), "cargo test",
                             Path("/w"), _world_env)
    assert calls["gdb"] == [("probe-222", _ALT_ENV)]
    assert calls["asan"] == [("alt", _ALT_ENV)]
    assert sites and all(s.world == "alt" for s in sites)
    assert {s.key for s in sites} == {"alt::tests/probe.rs"}
    assert "(cfg world `alt`)" in sites[0].render_evidence()
    # NEGATIVE CONTROL: the pre-change binding, every world re-run under the
    # home env, hands the other world's binary and sanitized build the home
    # world's flags.
    deps0, calls0 = _deps(("SIGSEGV", frames), (101, _ASAN))
    localize_crashes(deps0, _joined(_CLEAN, _CRASHED), "cargo test",
                     Path("/w"), lambda _w: _HOME_ENV)
    assert calls0["gdb"] == [("probe-222", _HOME_ENV)]
    assert calls0["asan"] == [("alt", _HOME_ENV)]


def test_a_world_with_no_env_is_localized_by_nobody() -> None:
    deps, calls = _deps(("SIGSEGV", []), (101, _ASAN))
    sites = localize_crashes(deps, _joined(_CLEAN, _CRASHED), "cargo test",
                             Path("/w"), lambda w: _HOME_ENV if w == "base" else None)
    assert sites == () and calls == {"gdb": [], "asan": []}


def test_the_same_binary_crashing_in_two_worlds_is_two_crashes() -> None:
    binaries = parse_binary_results(_joined(_CRASHED, _CRASHED))
    keys = crash_keys(binaries)
    # Not isolated: a crash is its binary (`crash_key` with no test).
    assert keys == {"base::tests/probe.rs", "alt::tests/probe.rs"}, keys
    # A crash seen before the change in one world does not excuse the other
    # world's crash after it.
    frames = [{"func": "crate::thing::{impl#1}::drop", "file": "src/thing.rs", "line": 40}]
    deps, calls = _deps(("SIGSEGV", frames), None)
    sites = localize_crashes(
        deps, _joined(_CRASHED, _CRASHED), "cargo test", Path("/w"),
        _world_env, exclude={"base::tests/probe.rs"})
    assert [s.world for s in sites] == ["alt"]


def test_each_world_has_its_own_sanitized_target_dir() -> None:
    assert asan_target_dir_name("") == "target-asan"
    assert asan_target_dir_name("alt") == "target-asan-alt"
    assert asan_target_dir_name("feature/x") == "target-asan-feature-x"
