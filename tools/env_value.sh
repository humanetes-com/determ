#!/usr/bin/env bash
#
# env_value.sh — resolve one setting the way the pipeline does: an already-set
# shell variable wins, then `.env`, then the built-in default.
#
# That precedence mirrors `load_dotenv`, which does not override variables the
# environment already carries — so a one-off `OPERATOR_EFFORT=high ./run.sh`
# keeps working while `.env` stays the durable home for the setting.
#
# Usage:  tools/env_value.sh KEY [DEFAULT]
# Prints the resolved value (possibly empty) on stdout.
set -euo pipefail

KEY="${1:?usage: env_value.sh KEY [DEFAULT]}"
DEFAULT="${2-}"
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"

# An exported empty value is still a deliberate choice, so test for existence
# rather than for non-emptiness.
if [[ -n "${!KEY+x}" ]]; then
  printf '%s\n' "${!KEY}"
  exit 0
fi

# Last uncommented assignment wins, matching how a shell would source the file.
# `-f2-` keeps values containing `=`; surrounding quotes are stripped so
# KEY="value" and KEY=value resolve identically.
line="$(grep -E "^[[:space:]]*${KEY}=" "$ENV_FILE" 2>/dev/null | tail -1 || true)"
if [[ -z "$line" ]]; then
  printf '%s\n' "$DEFAULT"
  exit 0
fi

value="${line#*=}"
value="${value#"${value%%[![:space:]]*}"}"      # ltrim
value="${value%"${value##*[![:space:]]}"}"      # rtrim
if [[ ${#value} -ge 2 && ${value:0:1} == '"' && ${value: -1} == '"' ]]; then
  value="${value:1:-1}"
elif [[ ${#value} -ge 2 && ${value:0:1} == "'" && ${value: -1} == "'" ]]; then
  value="${value:1:-1}"
fi

# An empty assignment in .env means "unset", not "override with empty" — that is
# how the file's own placeholder rows (`BACKEND=`) read.
if [[ -z "$value" ]]; then
  printf '%s\n' "$DEFAULT"
else
  printf '%s\n' "$value"
fi
