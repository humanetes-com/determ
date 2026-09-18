"""The immutable context one ``generate_fix_loop`` call shares across every fix
unit it drives.

Before this VO the inner cargo-check loops (`_fix_single_fn`, `_fix_impl_block`)
captured ~8 loop parameters by closure — project dir, the check command and its
derived check-only form, cargo env, timeout, model, FR path, the test-file set,
and the allowed-dir guard. Closure capture made the shared context implicit and
untestable in isolation. :class:`FixSession` names it: one frozen value passed
explicitly to each unit, so a unit is constructible (and unit-testable) without
re-deriving the loop's environment.

Pure data — no I/O, no behaviour. The check-only command is derived ONCE at
construction (`for_check_cmd`) so every unit re-checks with the same compile-only
form the loop chose, never re-deriving it per attempt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FixSession:
    """Read-only per-loop context shared by every :class:`FixUnit`.

    `check_only_cmd` is the compile-only form of `check_cmd` (tests stripped) the
    inner per-unit loops re-run; it is derived once at the composition root so a
    unit never re-derives it. `env` is treated as read-only (the cargo env
    resolved once for the whole loop)."""

    project_dir: Path
    check_only_cmd: str
    env: Mapping[str, str]
    timeout: int
    model: str
    fr_path: Path | None = None
    existing_test_files: frozenset[str] = frozenset()
    allowed_dir_prefix: str = ""

    @classmethod
    def for_check_cmd(
        cls, *, project_dir: Path, check_cmd: str,
        derive_check_only: Callable[[str], str], env: Mapping[str, str],
        timeout: int, model: str, fr_path: Path | None = None,
        existing_test_files: frozenset[str] = frozenset(),
        allowed_dir_prefix: str = "",
    ) -> FixSession:
        """Build a session from the loop's full `check_cmd`, deriving the
        compile-only command once via the injected `derive_check_only`."""
        return cls(
            project_dir=project_dir,
            check_only_cmd=derive_check_only(check_cmd),
            env=env, timeout=timeout, model=model, fr_path=fr_path,
            existing_test_files=existing_test_files,
            allowed_dir_prefix=allowed_dir_prefix,
        )

    def is_writable(self, file_rel: str) -> bool:
        """A target file is writable iff it is not a preserved test file and lies
        under the allowed dir prefix (when one is set). The two guards every fix
        path applied ad-hoc before splicing — named once here."""
        if file_rel in self.existing_test_files:
            return False
        return not (self.allowed_dir_prefix
                    and not file_rel.startswith(self.allowed_dir_prefix))
