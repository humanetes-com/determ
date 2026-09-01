#!/usr/bin/env python3
"""
WHY
    `technical_debt.md` holds only open work; an entry whose every point is
    implemented (or refuted/withdrawn) moves verbatim to
    `completed_tech_debt.md` (CLAUDE.md, "`technical_debt.md` holds only
    open work"). The only precedent before this script was
    `tools/split_tech_debt.py` -- a one-shot with hardcoded line ranges,
    guarded to refuse ever running again. There was no reusable tool, so
    every archival since session 281 has been a hand cut-and-paste: locate
    the entry, cut it, paste it under a new archive header, and hope
    nothing else in the same edit was still relying on the line numbers
    that move underneath it.

USAGE
    python tools/rotate_technical_debt.py <entry-number> [<entry-number> ...] \
        [--date YYYY-MM-DD] [--force]

    <entry-number> is the integer in a top-level heading
    `# <N> -- ...` in technical_debt.md (e.g. `180`). Only whole top-level
    entries move -- an entry's `## <N>.<M>` subsections and any trailing
    validation/amendment section travel with it automatically, since they
    fall inside its span by construction (span = from its `# ` heading to
    the next `# ` heading in the file).

    Range headings (`# 151-158 -- ...`, `# 132-140 -- ...`) and the
    individually-numbered `##` children living under them are NOT resolved
    by number -- a bare id there is ambiguous (which sibling under the
    shared intro?) and moving one out from under an open group requires
    copying the group's intro into the archive alongside it, which is a
    per-entry judgment call, not a mechanical span cut. Move those by hand
    with Edit, per CLAUDE.md's "Group headings whose children split... stay
    in technical_debt.md while any child does; copy the intro into the
    archive alongside the moved children."

    --date defaults to today. Pass it explicitly to backdate an archival
    that covers a past session's close-out.

    --force skips the OPEN-marker safety check described below -- use it
    only after confirming by hand that the flagged "OPEN" is not a live
    remainder (e.g. it names something already closed elsewhere in the
    same entry).

    The judgment of WHAT is fully closed is made BEFORE invoking this
    script, by reading the entry. This script performs the move only, and
    refuses on anything it cannot move unambiguously and verbatim.

HOW TO INTERPRET THE OUTPUT
    One line per requested id:
      move <N>: <line-count> lines            entry was moved
      skip <N>: already archived              idempotent no-op: <N> already
                                               has a top-level heading in
                                               completed_tech_debt.md
      refuse <N>: <reason>                    no match, ambiguous match, a
                                               NUMBERING COLLISION (the id is in
                                               completed_tech_debt.md under a
                                               DIFFERENT title), or an OPEN
                                               marker without --force
      warn: unnumbered top-level heading ...  an entry with no id: it bounds its
                                               neighbours' spans correctly but
                                               cannot be archived until it has one

    A "refuse" id leaves BOTH files untouched for that id -- the script
    either moves an entry whole or not at all, never partially. Exit code
    is 1 if any id was refused, 0 otherwise (a "skip" is not a failure).
"""
from __future__ import annotations

import argparse
import datetime
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBT = ROOT / "technical_debt.md"
DONE = ROOT / "completed_tech_debt.md"

TOP_HEADING = re.compile(r"^#\s+(\d+)(?:[–-]\d+)?\s")
OPEN_MARKER = re.compile(r"\bOPEN\b")


def parse_top_level_spans(
    text: str,
) -> tuple[list[str], dict[int, tuple[int, int]], set[int], list[tuple[int, str]]]:
    """Spans end at the next top-level heading of ANY shape, numbered or not.

    An entry's span used to end at the next `# <number>` heading, which made an
    UNNUMBERED top-level entry invisible. Measured 2026-09-01: one entry had been
    filed with a prose heading and no id, and it sat between two numbered ones —
    so the preceding entry's span ran 385 lines past its own end and swallowed
    the unnumbered entry whole. The OPEN check caught it that day only because
    the swallowed entry happened to contain the word; `--force` would have
    archived open work as part of a closed entry, and the archive is verbatim, so
    there is no signal afterwards that it happened. Any `# ` line is a boundary
    now, and unnumbered ones are reported so they get an id (only a numbered
    entry is addressable by this script at all)."""
    lines = text.splitlines(keepends=True)
    bounds: list[int] = []            # every top-level heading, the span boundaries
    headings: list[tuple[int, int]] = []   # (line_idx, entry_id), numbered only
    unnumbered: list[tuple[int, str]] = []  # (1-based line, heading text)
    for i, line in enumerate(lines):
        if not line.startswith("# "):
            continue
        bounds.append(i)
        m = TOP_HEADING.match(line)
        if m:
            headings.append((i, int(m.group(1))))
        elif i != 0:  # line 0 is the file's own title
            unnumbered.append((i + 1, line.strip()))
    spans: dict[int, tuple[int, int]] = {}
    seen: dict[int, int] = {}
    for line_idx, entry_id in headings:
        nxt = [b for b in bounds if b > line_idx]
        end = nxt[0] if nxt else len(lines)
        seen[entry_id] = seen.get(entry_id, 0) + 1
        spans[entry_id] = (line_idx, end)
    ambiguous = {eid for eid, count in seen.items() if count > 1}
    for eid in ambiguous:
        spans.pop(eid, None)
    return lines, spans, ambiguous, unnumbered


def archived_headings(done_text: str, entry_id: int) -> list[str]:
    """Heading lines in the archive that carry this id, whatever their title."""
    return [m.group(0).strip() for m in
            re.finditer(rf"^#\s+{entry_id}\D.*$", done_text, re.MULTILINE)]


def _title_of(heading: str) -> str:
    """A heading's identity for the archived-vs-live comparison: everything
    after `# <id> `, whitespace-collapsed. Two entries with the same id and
    different titles are a NUMBERING COLLISION, not an idempotent re-run."""
    return " ".join(re.sub(r"^#\s+\d+(?:[\u2013-]\d+)?\s*[\u2014-]?\s*", "", heading).split())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Archive fully-closed technical_debt.md entries into completed_tech_debt.md, verbatim.")
    ap.add_argument("ids", nargs="+", type=int, help="top-level entry numbers, e.g. 180 178")
    ap.add_argument("--date", default=None, help="archive date, YYYY-MM-DD (default: today)")
    ap.add_argument("--force", action="store_true", help="skip the OPEN-marker safety check")
    args = ap.parse_args()

    date = args.date or datetime.date.today().isoformat()

    debt_text = DEBT.read_text(encoding="utf-8")
    done_text = DONE.read_text(encoding="utf-8")
    lines, spans, ambiguous_ids, unnumbered = parse_top_level_spans(debt_text)
    for line_no, heading in unnumbered:
        print(f"warn: unnumbered top-level heading at line {line_no} "
              f"({heading[:60]!r}) -- not addressable by id; give it one")

    to_move: list[tuple[int, int, int]] = []  # (entry_id, start, end)
    results: list[tuple[int, str, str]] = []

    for entry_id in args.ids:
        archived = archived_headings(done_text, entry_id)
        if archived:
            live_span = spans.get(entry_id)
            live_heading = lines[live_span[0]].strip() if live_span else ""
            live_title = _title_of(live_heading)
            # An id already in the archive is a no-op ONLY when it is the SAME
            # entry. A DIFFERENT title under the same id is a numbering
            # collision, and it has happened: on 2026-08-29 one entry was
            # archived and an unrelated one was filed the same day under the
            # number it had just vacated. A bare id match then reports "already
            # archived" for the live entry, which is checked BEFORE the OPEN
            # marker and before --force, so that entry could never be moved by
            # this script at all -- it simply sat in the open file, and three
            # days later still read as open work. Refuse and print both
            # headings, so the operator renumbers the live one (ids are unique
            # across the two files by the open file's own stated rule) instead
            # of losing it.
            if live_title and not any(_title_of(h) == live_title for h in archived):
                results.append((entry_id, "refuse",
                                 f"NUMBERING COLLISION -- completed_tech_debt.md already has "
                                 f"'{archived[0][:70]}...' under id {entry_id}, and the live entry is "
                                 f"'{live_heading[:70]}...'. Renumber the live entry (ids are unique "
                                 f"across both files) and re-run."))
                continue
            results.append((entry_id, "skip", "already archived"))
            continue
        if entry_id in ambiguous_ids:
            results.append((entry_id, "refuse",
                             f"{entry_id} matches more than one top-level '# {entry_id} ...' heading"))
            continue
        span = spans.get(entry_id)
        if span is None:
            results.append((entry_id, "refuse",
                             f"no top-level entry '# {entry_id} --' found -- range headings and "
                             f"'##' children under them are not resolved by number, move by hand"))
            continue
        start, end = span
        span_text = "".join(lines[start:end])
        if not args.force:
            m = OPEN_MARKER.search(span_text)
            if m:
                ctx = span_text[max(0, m.start() - 40):m.start() + 40].replace("\n", " ")
                results.append((entry_id, "refuse",
                                 f"contains an OPEN marker ('...{ctx}...') -- re-check completeness "
                                 f"or pass --force"))
                continue
        to_move.append((entry_id, start, end))
        results.append((entry_id, "move", f"{end - start} lines"))

    if to_move:
        block_by_id = {eid: "".join(lines[start:end]) for eid, start, end in to_move}
        for _eid, start, end in sorted(to_move, key=lambda t: -t[1]):
            del lines[start:end]
        DEBT.write_text("".join(lines), encoding="utf-8")

        ordered = sorted(to_move, key=lambda t: t[1])  # file order, for the archive appendix
        ids_label = ", ".join(str(eid) for eid, _, _ in ordered)
        appendix = f"\n---\n\n## Archived {date} from technical_debt.md (entry {ids_label})\n\n"
        appendix += "".join(block_by_id[eid] for eid, _, _ in ordered)
        DONE.write_text(done_text.rstrip("\n") + "\n" + appendix.rstrip("\n") + "\n", encoding="utf-8")

    for entry_id, status, msg in results:
        print(f"{status} {entry_id}: {msg}")

    return 1 if any(status == "refuse" for _, status, _ in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
