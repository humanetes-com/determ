#!/usr/bin/env bash
# List (or reap) reconstructed test binaries left behind when a cargo
# timeout killed cargo and not the grandchild.
#
# WHY THIS EXISTS
#   Every bounded cargo timeout in this tree used `subprocess.run(timeout=)`,
#   which SIGKILLs the direct child (cargo) and leaves the test binary.
#   Measured 2026-09-08: 27 `deps/*--test-threads=1` processes with PPID 1,
#   every one `S<l futex_wait`, from runs `3014175-d98ed09e`,
#   `3371694-c8835690`, `3400546-41aeadee`, and the coverage hang of
#   `3500465-67c40e78` (pid 3605384, then PPID 1 after the pipeline
#   SIGTERM). `run_bounded` is the fix going forward; this script is the
#   one-shot cleanup of the table the old runner left, and the warning
#   `pre_run.sh` prints so a leftover cannot be mistaken for a live run
#   (`pgrep -f deps/` is how a watcher tells them apart).
#
# USAGE
#   scripts/orphan_test_binaries.sh           # list PPID-1 binaries
#   scripts/orphan_test_binaries.sh --kill    # SIGKILL them
#
#   No GPU. Safe while a pipeline is running: only processes whose parent
#   is init are touched. A live cargo's test binary has PPID = cargo.
#
# HOW TO INTERPRET THE OUTPUT
#   Each line is `pid ppid etime binary`. Count is the number of hung
#   binaries the next timeout would have added to rather than replaced.
#   `--kill` prints `killed N` and the same lines. Zero lines means the
#   table is clean.
set -euo pipefail

_KILL=false
for _arg in "$@"; do
  [[ "$_arg" == "--kill" ]] && _KILL=true
done

_list() {
  # pid ppid etime cmd — only the reconstructed test binary, parent init.
  ps -eo pid=,ppid=,etime=,cmd= | awk '
    $2 == 1 && $0 ~ /\/deps\/[^ ]+ --test-threads=1/ {
      pid=$1; ppid=$2; etime=$3
      cmd=""
      for (i=4; i<=NF; i++) cmd=cmd (i==4?"":" ") $i
      n=split(cmd, a, "/")
      bin=a[n]
      sub(/ --test-threads=1.*/, "", bin)
      print pid, ppid, etime, bin
    }'
}

_rows=$(_list)
if [[ -z "$_rows" ]]; then
  echo "orphan test binaries: 0"
  exit 0
fi

_n=$(printf '%s\n' "$_rows" | wc -l)
echo "orphan test binaries: $_n"
printf '%s\n' "$_rows"
if [[ "$_KILL" == true ]]; then
  _pids=$(printf '%s\n' "$_rows" | awk '{print $1}')
  # shellcheck disable=SC2086
  kill -KILL $_pids 2>/dev/null || true
  echo "killed $_n"
fi
