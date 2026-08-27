#!/usr/bin/env bash
# Archive the current handover.md into previous_handovers.md, then write the
# NEXT session's stub into handover.md — number, date, empty sections, and
# pinned blocks already in the file.
#
# WHY the stub. The writer used to grep handover.md for `session N`, add one,
# and paste pins from handover_pinned.md. That is reading the artefact to
# recover a number the first line already carried, and it is how a pin got
# dropped: the paste was a second, optional step. This script owns both.
# The writer fills the bold title and the three sections; they do not
# compute N and they do not re-assemble pins.
#
# Idempotent: refuses to re-archive a header already in previous_handovers.md;
# a second run on the unwritten stub is a no-op. A run whose header is already
# archived but handover.md was never replaced (crash between archive and stub)
# writes the stub and stops.
#
# PINNED blocks — content between `<!-- PINNED:BEGIN name -->` and
# `<!-- PINNED:END name -->` is standing reference, not a session record: it has
# a different lifecycle from the session it sits in. Archiving it would bury it
# in previous_handovers.md (which nobody loads). The archived copy is stripped;
# the blocks are mirrored to handover_pinned.md AND written into the new stub.
set -euo pipefail

root="${ROTATE_HANDOVER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cur="$root/handover.md"
arc="$root/previous_handovers.md"
pin="$root/handover_pinned.md"

STUB_TITLE="UNWRITTEN — replace this title"

[[ -f "$cur" ]] || { echo "rotate_handover: $cur missing" >&2; exit 1; }

hdr="$(head -1 "$cur")"
case "$hdr" in
  "# Handover — session "*) ;;
  *) echo "rotate_handover: unexpected first line: $hdr" >&2; exit 1 ;;
esac

if [[ ! "$hdr" =~ session\ ([0-9]+)\  ]]; then
  echo "rotate_handover: no session number in: $hdr" >&2
  exit 1
fi
cur_n="${BASH_REMATCH[1]}"
next=$((cur_n + 1))
today="$(date +%Y-%m-%d)"

write_stub() {
  local n="$1"
  local pin_src="$2"
  {
    printf '# Handover — session %s (%s) — **%s**\n\n' "$n" "$today" "$STUB_TITLE"
    printf '## ON RETURN\n\n'
    printf '## State at handover\n\n'
    printf '## Key facts\n\n'
    if [[ -n "$pin_src" && -s "$pin_src" ]]; then
      cat "$pin_src"
      # The pin file is a concatenation of blocks and may lack a trailing newline.
      printf '\n'
    fi
  } > "$cur"
  echo "rotate_handover: next session $n"
  local npin=0
  if [[ -n "$pin_src" && -s "$pin_src" ]]; then
    npin="$(grep -c 'PINNED:BEGIN' "$pin_src" || true)"
    echo "rotate_handover: pinned $npin"
    echo "rotate_handover: wrote stub handover.md (pins already in the file — fill the title and the three sections; do not grep for the number)"
  else
    echo "rotate_handover: pinned 0"
    echo "rotate_handover: wrote stub handover.md (no pinned blocks — fill the title and the three sections; do not grep for the number)"
  fi
}

if [[ "$hdr" == *"$STUB_TITLE"* ]]; then
  echo "rotate_handover: already rotated — fill session $cur_n in handover.md"
  echo "rotate_handover: next session $cur_n"
  echo "rotate_handover: pinned $(grep -c 'PINNED:BEGIN' "$cur" || true)"
  exit 0
fi

# Split on the markers. awk, not a sed range: an unterminated BEGIN must be a
# hard error, not a silently truncated archive.
awk_split='
  /<!-- PINNED:BEGIN/ { inpin=1; npin++ }
  { if (inpin) print > pinf; else print > outf }
  /<!-- PINNED:END/   { inpin=0 }
  END { if (inpin) { print "unterminated PINNED:BEGIN" > "/dev/stderr"; exit 1 }
        print npin+0 }
'
tmp_out="$(mktemp)"; tmp_pin="$(mktemp)"
trap 'rm -f "$tmp_out" "$tmp_pin"' EXIT
n="$(awk -v outf="$tmp_out" -v pinf="$tmp_pin" "$awk_split" "$cur")"

pin_src=""
if [[ "$n" -gt 0 ]]; then
  cp "$tmp_pin" "$pin"
  pin_src="$tmp_pin"
elif [[ -s "$pin" ]]; then
  # Current file had no pins (the paste the stub now owns used to miss). Carry
  # the last mirrored set rather than writing a pinless stub over a known one.
  pin_src="$pin"
  echo "rotate_handover: current handover had no pins; carrying from $pin"
fi

if [[ -f "$arc" ]] && grep -qxF "$hdr" "$arc"; then
  echo "rotate_handover: already archived — $hdr"
  write_stub "$next" "$pin_src"
  exit 0
fi

{ printf '\n---\n\n'; cat "$tmp_out"; } >> "$arc"

if [[ "$n" -gt 0 ]]; then
  echo "rotate_handover: archived (minus $n pinned block(s)) — $hdr"
else
  echo "rotate_handover: archived — $hdr"
fi
write_stub "$next" "$pin_src"
