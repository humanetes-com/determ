#!/usr/bin/env bash
# Back-compat shim: resume_from_t2.sh == restart_from.sh task2
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restart_from.sh" task2 "$@"
