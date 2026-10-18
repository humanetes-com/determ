#!/usr/bin/env python3
"""Compute the Gherkin view's Given fixture on a stored behaviour book.

WHY. The view pasted every example's whole helper closure under a bare
`Given`: on the anyhow `stable` book written 2026-10-01, 6,796 of 8,818 view
lines were Given text, `assert_err` (the asserting helper) was printed as a
precondition 100 times, and 5,641 of the lines were helper text the store
holds once in 216. The view now prints a pointer per root and one index file.
This script computes, on the stored book, the six claims that change was
shipped on, and the same counts on the view files a previous pass wrote (the
negative control).

USAGE.
    python scripts/givens_view_fixture.py [--project tmp/anyhow] [--cluster stable]
Read-only: it loads `.skeleton/_behaviours_<cluster>.json`, renders the view
in memory, and reads `.skeleton/_features_<cluster>/` and `tests/` as they
are. No model, no cargo, no GPU.

HOW TO INTERPRET THE OUTPUT. One line per claim, `ok` or `FAIL`:
  (a) helper text in .feature  — stored helper texts that occur in any
      rendered `.feature`; the claim is 0.
  (b) root steps resolve       — root steps whose key is in the book's helper
      table and whose file exists under the workspace `tests/`; the claim is
      all of them.
  (c) observer in a Given      — examples whose asserting helper (`via`) is
      one of their own Given steps; the claim is 0.
  (d) examples with no root    — examples with no root step.
  (e) roots in the index       — distinct roots (`## ` headings under
      `# Givens`) in `_givens.md`.
  (f) view lines               — total lines of the rendered view, index
      included, against the lines on disk; how many are Given steps (step
      lines and setup doc strings) and how many are the index.
  (g) doc strings under a Given — Given/And steps followed by a rust doc
      string in the rendered view; the claim is 0 (`on disk:` prints the
      same count on the files the last bake wrote: 232 on the 2026-10-04
      stable view).
  (i) setups in the index      — `## ` headings under `# Setups` against
      the distinct setup texts of the book; the claim is equal.
  (D) phrases quoting code     — stored phrases `quotes_fixture_code`
      refuses against their root's text (11 of 17 on 2026-10-04).
`on disk:` is the negative control: the same counts over the files the last
bake wrote. If those files were written by this code the two readings agree
and the control says nothing; it bites only against a pre-change view.
Exit status 1 when (a), (b) or (c) fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.application.behaviour_book_build import feature_files  # noqa: E402
from pipeline_core.domain.behaviour import BehaviourSuite, example_given  # noqa: E402
from pipeline_core.domain.behaviour_book import (  # noqa: E402
    GIVENS_INDEX_NAME,
    given_roots,
    quotes_fixture_code,
)
from pipeline_core.domain.example_setup import helper_key_parts  # noqa: E402


def _given_lines(text: str) -> int:
    """Lines of `text` inside a Given/And step: the step line and its doc string."""
    n = 0
    inside = False
    for line in text.split("\n"):
        t = line.strip()
        if t.startswith(("Given", "And ")):
            inside = True
        elif t.startswith(("When", "Then", "Example:", "Rule:")):
            inside = False
        n += inside
    return n


def _flat(text: str) -> str:
    """`text` with every line's indentation removed: a doc string indents
    what it quotes, so a pasted helper is found only line by line."""
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--cluster", default="stable")
    a = ap.parse_args()
    project = Path(a.project)
    suite = BehaviourSuite.from_dict(json.loads(
        (project / ".skeleton" / f"_behaviours_{a.cluster}.json").read_text(encoding="utf-8")))
    by_id = {s.behaviour_id: s for s in suite.specs}
    shown = [by_id[i] for i in dict.fromkeys(i for f in suite.features for i in f.example_ids)
             if i in by_id]
    files = feature_files(suite)
    features = {n: t for n, t in files.items() if n.endswith(".feature")}
    index = files.get(GIVENS_INDEX_NAME, "")
    table = dict(suite.helpers)
    texts = [_flat(t) for t in table.values() if t.strip()]

    pasted = sum(1 for t in texts if any(t in _flat(f) for f in features.values()))
    givens = [example_given(s) for s in shown]
    root_steps = [k for g in givens for k in g.roots]
    resolved = [k for k in root_steps
                if k in table and (project / helper_key_parts(k)[0]).is_file()]
    observer_given = sum(
        1 for s, g in zip(shown, givens, strict=True)
        if s.via and any(helper_key_parts(k)[1] == s.via for k in g.roots))
    no_root = sum(1 for g in givens if not g.roots)
    index_roots = index.split("# Observers")[0].count("\n## ")
    lines = sum(t.count("\n") for t in files.values())

    print(f"book: {a.cluster}, {len(shown)} example(s) shown, {len(table)} helper(s), "
          f"{len(features)} feature file(s)")
    ok_a, ok_b, ok_c = pasted == 0, len(resolved) == len(root_steps), observer_given == 0
    print(f"(a) helper text in .feature: {pasted} of {len(texts)}  {'ok' if ok_a else 'FAIL'}")
    print(f"(b) root steps resolve: {len(resolved)} of {len(root_steps)}  {'ok' if ok_b else 'FAIL'}")
    print(f"(c) observer in a Given: {observer_given}  {'ok' if ok_c else 'FAIL'}")
    print(f"(d) examples with no root: {no_root}")
    print(f"(e) roots in the index: {index_roots}")
    print(f"(f) view lines: {lines} ({sum(_given_lines(t) for t in features.values())} in Given "
          f"steps, {index.count(chr(10))} in the index)")

    # Entry 309's amendment (2026-10-17): no code under any Given, each
    # distinct setup once in the index, and the stored phrases that
    # transcribe their fixture's code.
    doc_under_given = sum(_doc_under_given(t) for t in features.values())
    setups_in_index = index.split("# Setups")[1].split("# Observers")[0].count("\n## ") \
        if "# Setups" in index else 0
    distinct_setups = len({s.setup.strip() for s in suite.specs if s.setup.strip()})
    roots_all = {r.key: r for r in given_roots(suite.specs)}
    phrased = dict(suite.givens)
    quoting = sorted(k for k, ph in phrased.items()
                     if k in roots_all and quotes_fixture_code(ph, roots_all[k].text))
    ok_g = doc_under_given == 0
    print(f"(g) doc strings under a Given: {doc_under_given}  {'ok' if ok_g else 'FAIL'}")
    print(f"(i) setups in the index: {setups_in_index} of {distinct_setups} distinct setup(s)")
    print(f"(D) stored phrases that quote their fixture's code: {len(quoting)} of "
          f"{len(phrased)}: {', '.join(quoting[:8])}")

    disk = project / ".skeleton" / f"_features_{a.cluster}"
    if disk.is_dir():
        old = {p.name: p.read_text(encoding="utf-8") for p in sorted(disk.iterdir()) if p.is_file()}
        old_features = {n: t for n, t in old.items() if n.endswith(".feature")}
        old_pasted = sum(1 for t in texts if any(t in _flat(f) for f in old_features.values()))
        via_names = sorted({s.via for s in shown if s.via})
        via_texts = [_flat(t) for k, t in table.items() if helper_key_parts(k)[1] in via_names]
        via_copies = sum(_flat(f).count(t) for f in old_features.values() for t in via_texts)
        print(f"on disk: doc strings under a Given: "
              f"{sum(_doc_under_given(t) for t in old_features.values())}")
        print(f"on disk: {len(old)} file(s), {sum(t.count(chr(10)) for t in old.values())} line(s), "
              f"{sum(_given_lines(t) for t in old_features.values())} in Given steps, "
              f"helper text in .feature: {old_pasted} of {len(texts)}, "
              f"asserting-helper texts printed: {via_copies}")
    return 0 if ok_a and ok_b and ok_c and ok_g else 1


def _doc_under_given(text: str) -> int:
    """Given/And steps followed directly by a rust doc string."""
    lines = text.splitlines()
    return sum(1 for a, b in zip(lines, lines[1:], strict=False)
               if a.strip().startswith(("Given", "And")) and b.strip() == '"""rust')


if __name__ == "__main__":
    raise SystemExit(main())
