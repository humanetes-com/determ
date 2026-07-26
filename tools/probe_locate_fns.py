#!/usr/bin/env python3
"""Print what `locate_all_fns` believes about a bare fn name in a file.

The reverter rewrites every span this returns, so a span that covers anything
other than a real fn BODY corrupts the tree. Use it to check a name before
trusting a revert: a trait method DECLARATION (`fn f(self) -> T;`) has no body
and must not appear here at all.

    tools/probe_locate_fns.py <file.rs> <fn_name>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.rust_fn_headers import locate_all_fns  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    locs = locate_all_fns(text, sys.argv[2])
    print(f"{len(locs)} span(s) for fn '{sys.argv[2]}'")
    for sig, ss, bs, be in locs:
        line = text.count("\n", 0, ss) + 1
        span_lines = text.count("\n", bs, be)
        print(f"  L{line:<5} sig={sig!r}")
        print(f"         body spans {span_lines} line(s): {text[bs:be][:70]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
