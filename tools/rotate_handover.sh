#!/usr/bin/env bash
# Archive the current handover.md into previous_handovers.md.
# Run BEFORE overwriting handover.md with the new session's block.
# Idempotent: refuses if this session's header is already archived, so a
# repeated run cannot duplicate a session in the archive.
#
# PINNED blocks — content between `<!-- PINNED:BEGIN name -->` and
# `<!-- PINNED:END name -->` is standing reference, not a session record: it has
# a different lifecycle from the session it happens to sit in. Archiving it
# would bury it in previous_handovers.md (which nobody loads) the moment that
# session rotates out. So the archived copy gets the blocks stripped, and they
# are mirrored to handover_pinned.md so the next session can paste them back
# verbatim even if handover.md is overwritten carelessly.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cur="$root/handover.md"
arc="$root/previous_handovers.md"
pin="$root/handover_pinned.md"

[[ -f "$cur" ]] || { echo "rotate_handover: $cur missing" >&2; exit 1; }

hdr="$(head -1 "$cur")"
case "$hdr" in
  "# Handover — session "*) ;;
  *) echo "rotate_handover: unexpected first line: $hdr" >&2; exit 1 ;;
esac

if [[ -f "$arc" ]] && grep -qxF "$hdr" "$arc"; then
  echo "rotate_handover: already archived — $hdr"
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

{ printf '\n---\n\n'; cat "$tmp_out"; } >> "$arc"

if [[ "$n" -gt 0 ]]; then
  cp "$tmp_pin" "$pin"
  echo "rotate_handover: archived (minus $n pinned block(s)) — $hdr"
  echo "rotate_handover: CARRY FORWARD $n pinned block(s) into the new handover.md — mirrored to $pin"
else
  echo "rotate_handover: archived — $hdr"
fi
