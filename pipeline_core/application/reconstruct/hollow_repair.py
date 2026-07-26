"""Un-nest fn bodies that are only a re-declaration of their own fn.

A reply that restates the WHOLE item where a BODY was asked for splices as
``fn f(..) { fn f(..) { work() } }``: the outer body is empty, the work sits in
a nested item nothing calls. It parses, it type-checks, it holds no
``unimplemented!()`` — so every stub scan reads the fn as FILLED and never
re-attempts it. The fn is silently dead for the rest of the run.

Refusing the shape at the splice only stops NEW instances. Whatever is already
on disk stays invisible, because the predicate that would re-queue it is the
same one that failed to flag it. This module is the other half: a deterministic
repair pass over a tree on disk that splices the nested body back where it
belongs. It is not a revert — the model's work is kept, only the wrapper goes.

Two cases make the pass mandatory rather than cosmetic:

* a hollow ``#[test]`` PASSES while asserting nothing, and test targets are
  immutable to the fill loop, so no other mechanism can ever repair it — the
  oracle silently counts a pass that proves nothing;
* the whole-item reply often carries its own closing brace, which lands just
  past the outer fn and leaves the file unparseable — one dead cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.splice_txn import (
    ParseValidator,
    parse_guarded_write,
)
from pipeline_core.domain.rust_fn_headers import fn_headers, locate_all_fns
from pipeline_core.domain.rust_scan import redeclared_inner_body

# One pass repairs one fn (every later offset shifts), so the cap bounds fns
# per file, not retries of the same fn.
_MAX_REPAIRS_PER_FILE = 32


@dataclass(frozen=True, slots=True)
class HollowRepair:
    """One un-nested fn."""

    file: str
    fn_name: str
    is_test: bool
    dropped_stray_brace: bool


def _is_test_fn(text: str, sig_start: int) -> bool:
    """`#[test]` among the attributes immediately above the signature."""
    return "#[test]" in text[max(0, sig_start - 200):sig_start]


def _next_hollow(text: str) -> tuple[str, int, int, int, str] | None:
    """First (name, sig_start, body_start, body_end, inner) whose body is only a
    re-declaration of itself and has recoverable content, in file order."""
    best: tuple[str, int, int, int, str] | None = None
    for name in dict.fromkeys(h.name for h in fn_headers(text, line_anchored=False)):
        for _sig, ss, bs, be in locate_all_fns(text, name):
            inner = redeclared_inner_body(text[bs:be], name)
            if inner and (best is None or ss < best[1]):
                best = (name, ss, bs, be, inner)
    return best


def _candidate(text: str, body_start: int, body_end: int, inner: str,
               sig_start: int) -> str:
    base = ""
    for ch in text[text.rfind("\n", 0, sig_start) + 1:sig_start]:
        if ch not in (" ", "\t"):
            break
        base += ch
    indented = "\n" + "\n".join(
        (base + "    " + ln) if ln.strip() else "" for ln in inner.splitlines()
    ) + "\n" + base
    return text[:body_start] + indented + text[body_end:]


def _stray_brace_at(text: str, body_end: int) -> int:
    """Index of a lone ``}`` sitting just past the fn's own closing brace, else -1.

    `body_end` indexes that closing brace; the whole-item reply's surplus brace
    is the next non-whitespace character after it."""
    i = body_end + 1
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i if i < len(text) and text[i] == "}" else -1


def repair_hollow_bodies(src_dir: Path,
                         validate_parse: ParseValidator | None = None,
                         ) -> list[HollowRepair]:
    """Un-nest every hollow body under `src_dir`, keeping each file parseable.

    A file that parsed before the repair must still parse after it, or the
    repair is rolled back. A file that did NOT parse before is repaired anyway:
    unwrapping cannot make an already-broken file worse, and the surplus brace
    is usually the reason it was broken. A brace is only ever deleted when
    doing so is what made the file parse — never on a guess."""
    out: list[HollowRepair] = []
    for path in sorted(src_dir.rglob("*.rs")):
        parsed_before = validate_parse(path) if validate_parse else True
        for _ in range(_MAX_REPAIRS_PER_FILE):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                break
            hit = _next_hollow(text)
            if hit is None:
                break
            name, sig_start, body_start, body_end, inner = hit
            pre = text.encode("utf-8")
            cand = _candidate(text, body_start, body_end, inner, sig_start)
            stray = _stray_brace_at(text, body_end)
            trimmed = (_candidate(text[:stray] + text[stray + 1:],
                                  body_start, body_end, inner, sig_start)
                       if stray >= 0 else None)
            committed: bool | None = None
            for candidate, dropped in ((cand, False), (trimmed, True)):
                if candidate is None:
                    continue
                if parse_guarded_write(path, candidate.encode("utf-8"), pre,
                                       validate_parse):
                    committed = dropped
                    break
            if committed is None:
                if parsed_before:
                    break            # unwrapping broke a healthy file — leave it
                path.write_bytes(cand.encode("utf-8"))
                committed = False
            out.append(HollowRepair(path.name, name,
                                    _is_test_fn(text, sig_start), committed))
    return out
