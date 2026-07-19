"""Crash localization (pure domain).

A test binary that dies mid-run (SIGSEGV / abort) takes every unreported
test's verdict with it — the sweep sees WHICH tests vanished but not WHY.
Reverse-mapping vanished tests to fns is weak evidence (a test statically
reaches dozens of fns); the faulting STACK is exact. These types carry a
debugger backtrace as typed evidence: the innermost in-crate frame names the
fn whose body must change, outranking any test-name correlation during
attribution, and the rendered form is injected into that unit's fill prompt
so the model sees the actual crash path instead of guessing from a vanished
test list.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.models import FnLocator


@dataclass(frozen=True, slots=True)
class CrashFrame:
    """One backtrace frame inside the crate under reconstruction.

    ``fn_name`` is the bare fn identifier (last path segment of the symbol,
    generics stripped) so it matches extractor records; ``symbol`` keeps the
    full demangled path for prompt display."""

    file_rel: str
    line: int
    fn_name: str
    symbol: str


@dataclass(frozen=True, slots=True)
class CrashSite:
    """A localized binary crash: which test was in flight, what signal, the
    in-crate faulting frames innermost-first, and (when the detector reports
    it — ASAN does) the ``origin_frames`` of the code that CREATED the
    invalid memory being accessed."""

    binary_label: str
    test_name: str | None
    signal: str
    frames: tuple[CrashFrame, ...]
    origin_frames: tuple[CrashFrame, ...] = ()

    @property
    def blamed(self) -> CrashFrame | None:
        """The frame whose fn must change: the innermost ORIGIN frame when
        known (the fn that fabricated the invalid memory — e.g. a dangling
        stack-'static's constructor), else the innermost faulting frame."""
        if self.origin_frames:
            return self.origin_frames[0]
        return self.frames[0] if self.frames else None

    @property
    def blamed_locator(self) -> FnLocator | None:
        f = self.blamed
        return FnLocator(f.file_rel, f.fn_name) if f else None

    @property
    def all_frames(self) -> tuple[CrashFrame, ...]:
        return self.frames + self.origin_frames

    def render_evidence(self) -> str:
        """Prompt-ready description of the crash. Concrete frames only — the
        model acts on the actual faulting path, not on prose about crashes."""
        where = f" while running `{self.test_name}`" if self.test_name else ""
        lines = [
            f"Test binary `{self.binary_label}` died with {self.signal}"
            f"{where} — it never reported its remaining tests.",
            "Faulting access (innermost first, crate frames only):",
        ]
        lines += [f"  #{i} {fr.symbol} at {fr.file_rel}:{fr.line}"
                  for i, fr in enumerate(self.frames)]
        if self.origin_frames:
            lines.append("The invalid memory it reads was CREATED by:")
            lines += [f"  #{i} {fr.symbol} at {fr.file_rel}:{fr.line}"
                      for i, fr in enumerate(self.origin_frames)]
        f = self.blamed
        if f is not None:
            lines.append(
                f"Fix the pointer/lifetime scheme in `{f.fn_name}` "
                f"({f.file_rel}:{f.line}) — this class of silent crash means "
                "memory that outlived its owner (dangling 'static from a "
                "stack local, wrong cast target, double free). Do not patch "
                "the calling test's fn.")
        return "\n".join(lines)


def _strip_generics(symbol: str) -> str:
    """`ErrorImpl<()>::error` → `ErrorImpl::error` via bracket-depth scan."""
    out: list[str] = []
    depth = 0
    for ch in symbol:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def bare_fn_name(symbol: str) -> str:
    """Bare fn identifier from a demangled symbol:
    `anyhow::error::ErrorImpl<()>::error` → `error`;
    `anyhow::error::{impl#6}::drop` → `drop`."""
    return _strip_generics(symbol).rsplit("::", 1)[-1].strip()


def _crate_frames(
    raw_frames: list[dict[str, object]],
    crate_file_prefix: str,
    max_frames: int,
) -> tuple[CrashFrame, ...]:
    frames: list[CrashFrame] = []
    for rec in raw_frames:
        file_ = str(rec.get("file") or "")
        func = str(rec.get("func") or "")
        try:
            line = int(rec.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if not file_.startswith(crate_file_prefix) or not func:
            continue
        frames.append(CrashFrame(
            file_rel=file_, line=line,
            fn_name=bare_fn_name(func), symbol=func))
        if len(frames) >= max_frames:
            break
    return tuple(frames)


def site_from_frames(
    binary_label: str,
    test_name: str | None,
    signal: str,
    raw_frames: list[dict[str, object]],
    *,
    origin_raw_frames: list[dict[str, object]] | None = None,
    crate_file_prefix: str = "src/",
    max_frames: int = 6,
) -> CrashSite:
    """Build a :class:`CrashSite` from debugger frame records
    (``{"file": …, "line": …, "func": …}``), keeping only frames whose file
    lies inside the crate source tree (relative ``src/…`` paths — the
    compiler embeds workspace-relative paths for crate-local code, absolute
    paths for std/deps). ``origin_raw_frames`` (ASAN's allocation stack)
    localizes the code that created the invalid memory."""
    return CrashSite(
        binary_label=binary_label, test_name=test_name,
        signal=signal or "fatal signal",
        frames=_crate_frames(raw_frames, crate_file_prefix, max_frames),
        origin_frames=_crate_frames(origin_raw_frames or [],
                                    crate_file_prefix, max_frames))


def crash_evidence_by_locator(
    sites: tuple[CrashSite, ...],
) -> dict[FnLocator, str]:
    """Map every fn appearing in any site's frames to that site's rendered
    evidence — the innermost (blamed) frame first, but callers up the stack
    also receive the evidence: when the blamed fn's fix requires a consistent
    scheme across its callers, each of them sees the same concrete crash."""
    out: dict[FnLocator, str] = {}
    for site in sites:
        text = site.render_evidence()
        for fr in site.all_frames:
            key = FnLocator(fr.file_rel, fr.fn_name)
            if key in out and text in out[key]:
                continue
            out[key] = (out[key] + "\n\n" + text) if key in out else text
    return out


def _parse_asan_frame_line(line: str) -> dict[str, object] | None:
    """``#N 0xADDR in SYMBOL FILE:LINE[:COL]`` → frame record, else None."""
    toks = line.split()
    if len(toks) < 5 or toks[2] != "in":
        return None
    loc = toks[-1]
    symbol = " ".join(toks[3:-1])
    parts = loc.rsplit(":", 2)
    file_ = parts[0]
    line_no = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    return {"func": symbol, "file": file_, "line": line_no}


def parse_asan_report(
    output: str,
) -> tuple[str, list[dict[str, object]], list[dict[str, object]]] | None:
    """Extract ``(error_kind, access_frames, origin_frames)`` from an
    AddressSanitizer report.

    Frame lines are ``#N 0xADDR in SYMBOL FILE:LINE[:COL]``. The report's
    first stack is the faulting ACCESS; the second (under ``…is located in
    stack of…`` / ``allocated by…``) is the ORIGIN of the invalid memory —
    for a dangling stack-'static that stack names the constructor whose
    frame died. Returns None when no ASAN error is present."""
    kind: str | None = None
    stacks: list[list[dict[str, object]]] = []
    in_stack = False
    for raw in output.splitlines():
        line = raw.strip()
        if kind is None:
            if "ERROR: AddressSanitizer:" in line:
                tail = line.split("ERROR: AddressSanitizer:", 1)[1].strip()
                kind = "AddressSanitizer: " + (tail.split(" on ")[0].split(" at ")[0]
                                               or "error")
            continue
        if line.startswith("#"):
            if not in_stack:
                in_stack = True
                stacks.append([])
            rec = _parse_asan_frame_line(line)
            if rec is not None and len(stacks) <= 2:
                stacks[-1].append(rec)
        else:
            in_stack = False
    if kind is None:
        return None
    access = stacks[0] if stacks else []
    origin = stacks[1] if len(stacks) > 1 else []
    return kind, access, origin


def relativize_frame_files(
    frames: list[dict[str, object]], workspace_root: str,
) -> list[dict[str, object]]:
    """ASAN emits absolute paths; strip the workspace prefix so records match
    the extractor's repo-relative ``src/…`` convention (gdb already emits
    relative paths for crate-local code)."""
    root = workspace_root.rstrip("/") + "/"
    out: list[dict[str, object]] = []
    for rec in frames:
        f = str(rec.get("file") or "")
        if f.startswith(root):
            rec = {**rec, "file": f[len(root):]}
        out.append(rec)
    return out
