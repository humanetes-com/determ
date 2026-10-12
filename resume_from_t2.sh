#!/usr/bin/env bash
# Back-compat shim: resume_from_t2.sh == restart_from.sh task2.
# task2 drops the per-cluster fill's products. --keep-fills leaves them.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restart_from.sh" task2 "$@"
