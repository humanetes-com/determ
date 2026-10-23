"""Read how a failing test fails, by running it alone in its own process.

The reference floor excuses a red test only when it fails the way the
original fails (`domain.same_failure`). This is the one reader both
sides use: the reference measurement reads the original's failures with it
(twice, uncached), and every gate that judges this tree against the floor
reads this tree's with it.

The test runs from the executable cargo named on the capture's `Running`
line, with the harness arguments that select exactly it
(`same_failure.alone_args`), in the tree's directory and the env of
the world it failed in, plus `CARGO_MANIFEST_DIR` (cargo sets it when it
runs a test binary, and a harness may read it: trybuild does). No cargo
rebuild happens; the executable is the one the capture ran. A test with no
executable (a doc test) is read from its failure block in the capture; one
whose named executable is gone is not read at all. A
test that PASSES when run alone gives no reading: what failed in the suite
is not this test's own failure.

A reading of this tree is cached on the executable's identity (path, mtime,
size), the test and the env: an integration test binary is relinked when
the crate changes, so a cached reading never outlives the tree it read.
On the 2026-10-22 pair, the one such test (`ui`) took about 18 s alone.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ..domain.same_failure import (
    FailureSignature,
    FailureToRead,
    alone_args,
    signature_of,
)

Pair = tuple[str, str]

_CACHE: dict[tuple[str, int, int, str, str], FailureSignature | None] = {}


def _env_digest(env: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(env):
        h.update(f"{k}\0{env[k]}\0".encode())
    return h.hexdigest()[:16]


def _roots(cwd: Path, env: Mapping[str, str], extra: Iterable[str]) -> list[str]:
    roots = [str(cwd), str(cwd.resolve()), *extra]
    for key in ("CARGO_TARGET_DIR", "CARGO_HOME", "HOME"):
        if env.get(key):
            roots.append(str(env[key]))
    home = os.path.expanduser("~")
    if home and home != "~":
        roots.append(home)
    return roots


def read_one(f: FailureToRead, *, cwd: Path, env: Mapping[str, str],
             timeout: int, roots: Iterable[str] = (),
             cached: bool = True) -> FailureSignature | None:
    """How `f` fails in the tree at `cwd`, or None when it could not be read
    or passed alone."""
    all_roots = _roots(cwd, env, roots)
    if f.exe_path is None:
        # No executable to run alone (a doc test): its failure block is the
        # whole report rustdoc printed for it.
        return signature_of(f.body, roots=all_roots) if f.body else None
    exe = Path(f.exe_path)
    if not exe.is_absolute():
        exe = cwd / exe
    if not exe.is_file():
        # The capture named an executable that is gone: an unread failure,
        # never its libtest block. That block is the same for any failing
        # case of a harness (`ui`'s reads `1 of 7 tests failed`), so signing
        # it would excuse another failure under the same name.
        return None
    st = exe.stat()
    key = (str(exe), st.st_mtime_ns, st.st_size, f.name, _env_digest(env))
    if cached and key in _CACHE:
        return _CACHE[key]
    run_env = dict(env)
    run_env.setdefault("CARGO_MANIFEST_DIR", str(cwd))
    try:
        res = subprocess.run(
            [str(exe), *alone_args(f.name)], cwd=cwd,
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=run_env)
    except (OSError, subprocess.SubprocessError):
        return None
    sig = (signature_of((res.stdout or "") + "\n" + (res.stderr or ""),
                        roots=all_roots)
           if res.returncode != 0 else None)
    if cached:
        _CACHE[key] = sig
    return sig


def read_alone(todo: Sequence[FailureToRead], *, cwd: Path,
               env: Mapping[str, str], timeout: int,
               roots: Iterable[str] = ()) -> dict[Pair, FailureSignature | None]:
    """`read_one` for every entry of `todo` (a `SignatureReader` once the
    keyword arguments are bound)."""
    extra = list(roots)
    return {f.pair: read_one(f, cwd=cwd, env=env, timeout=timeout, roots=extra)
            for f in todo}
