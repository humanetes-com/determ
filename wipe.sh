#!/usr/bin/env bash
# Wipe all generated code and restart the pipeline from scratch.
# Equivalent to the old restart.sh --y but with a clearer name.
exec "$(dirname "${BASH_SOURCE[0]}")/continue.sh" --wipe "$@"
