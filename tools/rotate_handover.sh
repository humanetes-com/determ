#!/usr/bin/env bash
# Archive the current handover.md into previous_handovers.md.
# Run BEFORE overwriting handover.md with the new session's block.
# Idempotent: refuses if this session's header is already archived, so a
# repeated run cannot duplicate a session in the archive.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cur="$root/handover.md"
arc="$root/previous_handovers.md"

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

{ printf '\n---\n\n'; cat "$cur"; } >> "$arc"
echo "rotate_handover: archived — $hdr"
