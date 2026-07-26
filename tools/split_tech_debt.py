"""One-shot archival split of technical_debt.md (session 281).

Keeps the genuinely-open items in `technical_debt.md` and appends everything
resolved to `completed_tech_debt.md`, preserving the original text verbatim so
the provenance survives. Ranges are 1-based inclusive line numbers of the file
as it stood at the start of the session; re-running after the split is a no-op
by design (the guard below refuses a file that no longer matches).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBT = ROOT / "technical_debt.md"
DONE = ROOT / "completed_tech_debt.md"

# header+oracle, the two still-open BRAINSTORMs, B3, Plan #77
KEEP = [(1, 67), (208, 210), (212, 212), (220, 251)]
# plateau block, the RESOLVED sweep-oracle BRAINSTORM, the typing item, #88, #58
ARCHIVE = [(69, 206), (211, 211), (214, 218)]


def slice_lines(lines: list[str], spans: list[tuple[int, int]]) -> list[str]:
    out: list[str] = []
    for a, b in spans:
        out.extend(lines[a - 1:b])
    return out


def main() -> int:
    lines = DEBT.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) != 251 or not lines[68].startswith("## Plan #89"):
        print("technical_debt.md is not the pre-split file — refusing.", file=sys.stderr)
        return 1
    DONE.write_text(
        DONE.read_text(encoding="utf-8").rstrip("\n")
        + "\n\n---\n\n# Archived 2026-07-26 (session 281)\n\n"
        + "Closed on measurement, not on analysis — see the session-281 handover\n"
        + "for the numbers each closure rests on.\n\n"
        + "".join(slice_lines(lines, ARCHIVE)).rstrip("\n") + "\n",
        encoding="utf-8")
    DEBT.write_text("".join(slice_lines(lines, KEEP)), encoding="utf-8")
    print(f"archived {sum(b - a + 1 for a, b in ARCHIVE)} lines; "
          f"kept {sum(b - a + 1 for a, b in KEEP)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
