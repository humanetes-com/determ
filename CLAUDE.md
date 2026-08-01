# CLAUDE.md — conventions for code written in this repo

## Comments and docstrings must stand alone

**Never reference a tracker ID in source, tests, prompts, log lines or event
payloads.** That means no `debt 91.2`, no `debt #46 point 2`, no `FR-018`, no
`plan #77 T1`, no `#82`, and no bare `91.5)` shorthand.

**Why.** The person reading the file has the repository and nothing else. The
`.md` trackers are not distributed with it — `technical_debt.md`,
`handover.md` and the plan files are untracked working notes, and several have
been rewritten or archived since the IDs in the code were written. A number
that resolves to nothing is worse than no citation: it reads as a promise of
context that cannot be redeemed, and it invites the next reader to go looking
for a file that is not there.

**Instead, write the fact.** Everything a tracker entry would have supplied
belongs in the comment itself, in whatever length the point deserves:

- what the defect was, in terms of this code;
- the observation that establishes it — a measured count, a log line, a date,
  a run id, a file and symbol;
- why the fix is shaped the way it is, and what it deliberately does not do.

```python
# WRONG
# Cap the producer set (debt 91.2).

# RIGHT
# Escalating on a generic parameter names every type and therefore none:
# `impl<E> StdError for E` yielded the subject `E`, which matched 33 of the
# crate's 186 fns on the live tree (2026-08-01). Only names the crate declares
# as a struct or enum are answerable.
```

Run ids, dates, file paths, symbol names and measured numbers are all
encouraged — they are verifiable from the repository or from the logs the
repository produces. Session numbers ("session 308") are tracker references by
another name; leave them out too.

**On touch, scrub.** When you edit a file that already carries such a
reference, remove it and replace it with the fact it stood for. The trackers
keep their own numbering; the code does not mirror it.
