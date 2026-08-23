#!/usr/bin/env python3
"""Which production fns lose their ONLY named test if a gen_tests sibling goes?

WHY THIS EXISTS
---------------
Coverage augmentation at task0c writes `gen_tests` siblings against the ORIGINAL
source, and about ten of anyhow's 34 reach into the private layout — they name a
private item, so the layout t1.5 authors afterwards must provide that name or the
cluster stops compiling (the 2026-08-06 incident, run `1171697-3838c847`). The two
answers on the table are to stop generating such tests (prevention, at the task0c
admission gate) and to let the layout retrigger regenerate an offending sibling
(rescue). Both remove or rewrite a test that some production fn may be the only
named consumer of, and a unit with no attributed test falls back to the loop's
weaker unattributed-stub aim.

Nothing measured that price. This does — as a LOWER BOUND, by the identifier
reverse-map only. It is not the runtime attributor: `application/reconstruct/
attribution.py` also attributes through panic sites, rustc diagnostics and an LLM
classifier, none of which are computable without a run. So read a small number
here as "the name-level price is small", never as "no unit loses attribution".

Measured 2026-08-22 on `tmp/anyhow`, cluster `stable`: 4 fns — `no_backtrace` and
`vtable` (sole mention `error.gen_tests_4.rs`), `cast` and `thiserror_provide`
(sole mention `error.gen_tests_6.rs`). 39 of 81 scanned fn names are mentioned by
no test file at all, which is the calibration for how weak the proxy is.

USAGE
-----
    .venv/bin/python scripts/measure_sole_attribution.py [--project tmp/anyhow]
                                                         [--cluster stable]

Reads `.skeleton-filled/<cluster>/src/*.rs` and `<project>/tests/*.rs`. Runs no
cargo, calls no model, writes nothing — safe against a live run. Preconditions:
the cluster tree must be on disk (task2a has seeded it at least once).

The private-touching sibling set is not derived here — it is the pin-origin set
printed by `scripts/measure_oracle_pinning.py --detail`, and it is passed with
`--siblings` (comma-separated file names) when the crate is not anyhow. The
default is anyhow's ten, so a re-bake that renumbers the siblings needs the
origin list re-read from that script before this one is believed.

HOW TO INTERPRET THE OUTPUT
---------------------------
  production fn names scanned   the denominator: `fn <name>` in the cluster's
                                non-gen_tests sources, minus a stop-list of
                                trait-method names (`fmt`, `drop`, `from`, ...)
                                too common to attribute by token.
  sole mention of N: [...]      per private-touching sibling, the fns whose only
                                textual mention across ALL test files is that
                                sibling. These are the units that would drop to
                                unattributed if the sibling were removed rather
                                than regenerated.
  TOTAL                         the decision number. The rule fixed before the
                                measurement was taken: a total in single digits
                                means prevention may ship ALONE; a total in the
                                tens means it ships with a replacement source of
                                internal attribution, because that many units
                                falling to weaker aim in one move is a change to
                                the loop's targeting, not a test-hygiene fix.
  fns mentioned by NO test file how blind the proxy is overall. A large share
                                means many units are already attributed by panic
                                site or diagnostic rather than by name, so the
                                TOTAL above understates by an unknown factor —
                                report it alongside the total, never alone.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re

# Trait-method and prelude names too common to attribute by bare token: a test
# mentioning `fmt` says nothing about which of the crate's eleven `fn fmt` it
# reaches, so counting them would manufacture attribution that does not exist.
_TOO_COMMON = frozenset({
    "main", "fmt", "drop", "clone", "default", "from", "new", "source",
    "description", "cause", "deref", "deref_mut", "eq", "ne", "hash",
    "next", "next_back", "len", "provide",
})

_ANYHOW_PRIVATE_TOUCHING = (
    "ensure.gen_tests_1.rs,fmt.gen_tests_2.rs,ptr.gen_tests_1.rs,"
    "error.gen_tests_1.rs,error.gen_tests_4.rs,error.gen_tests_6.rs,"
    "wrapper.gen_tests_1.rs,wrapper.gen_tests_2.rs,wrapper.gen_tests_3.rs,"
    "wrapper.gen_tests_6.rs"
)

_FN = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--siblings", default=_ANYHOW_PRIVATE_TOUCHING,
                    help="comma-separated private-touching sibling file names "
                         "(from measure_oracle_pinning.py --detail)")
    args = ap.parse_args()

    root = pathlib.Path(args.project)
    src = root / ".skeleton-filled" / args.cluster / "src"
    if not src.is_dir():
        print(f"no cluster tree at {src}")
        return 1
    private_touching = frozenset(s.strip() for s in args.siblings.split(",") if s.strip())

    prod = [p for p in sorted(src.glob("*.rs")) if ".gen_tests_" not in p.name]
    tests = sorted(src.glob("*.gen_tests_*.rs"))
    tests += sorted((root / "tests").glob("*.rs"))

    fns = {m.group(1) for p in prod for m in _FN.finditer(p.read_text(errors="ignore"))}
    fns -= _TOO_COMMON

    mention: dict[str, set[str]] = collections.defaultdict(set)
    for p in tests:
        toks = set(_IDENT.findall(p.read_text(errors="ignore")))
        for f in fns & toks:
            mention[f].add(p.name)

    print(f"{args.project} / {args.cluster}")
    print(f"  production fn names scanned: {len(fns)}; test files: {len(tests)}; "
          f"private-touching siblings: {len(private_touching)}")
    sole: dict[str, list[str]] = collections.defaultdict(list)
    for f, files in mention.items():
        if len(files) == 1:
            only = next(iter(files))
            if only in private_touching:
                sole[only].append(f)
    total = 0
    for k in sorted(sole):
        total += len(sole[k])
        print(f"    {k:28} sole mention of {len(sole[k]):2}: {sorted(sole[k])}")
    print(f"  TOTAL fns whose only mentioning test file is a private-touching "
          f"sibling: {total}")
    print(f"  fns mentioned by NO test file at all: "
          f"{sum(1 for f in fns if f not in mention)} of {len(fns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
