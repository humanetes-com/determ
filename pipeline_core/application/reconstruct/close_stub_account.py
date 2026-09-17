"""What a closing cluster's remaining stubs are, by augmentation's measurement.

A TDD cluster closes green (or on the reference floor) with the stubs no failing
test reached still `unimplemented!()` — the driver fills only what a red test
reaches, and `skeleton-D` is skipped by design. Until 2026-09-17 that close said
nothing about them: run `301146-2d8a5f06` closed `nightly` at iter 2 with
`6 stub(s) remaining` and the first word about those six was the end-of-run
gate's exit 1, hours later.

The close now reads the cluster's function-execution record
(`.cluster_augmented/<cluster>__<module>.fns.json`) and classifies each stub
site the moment the cluster closes:

- `unreached at augmentation` — targeted test blocks compiled and ran and the
  coverage run after them still counted 0; no test enters the body, so no fill
  could have been verified. The end gate does not count these.
- `EXECUTED at augmentation` — the original's suite entered this body, and the
  reconstruction's suite closes green without reaching it. Measured on anyhow's
  augmented tree (2026-09-17) this is most of that run's six: the original's
  nightly suite executed `request_ref`, `provide_ref_backtrace` and `provide`
  in `src/nightly.rs` and `erase` in `src/error.rs`. The record cannot say
  which of two causes holds — the filled callers route around the body, or
  the test that entered it is not in this oracle — so the line names both.
- `not accounted` — pending, unmeasured, or no record at all.

It reports; it neither fills nor excuses anything.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pipeline_core.domain.fn_execution import (
    FnCoverageRecord,
    StubAccount,
    classify_stub_sites,
    site_label,
)
from pipeline_core.domain.stub_inventory import StubInventory


def report_close_stubs(
    *,
    stubs: StubInventory,
    record: FnCoverageRecord | None,
    cluster_name: str,
    close_kind: str,
    tag: str,
    log: Callable[[str], None],
    emit: Callable[..., Any],
) -> StubAccount | None:
    """Log and emit the closing cluster's stub sites by classification.
    None when there is no stub to account for."""
    sites = [s.site for s in stubs.sites]
    if not sites:
        return None
    account = classify_stub_sites(sites, [record] if record is not None else [])
    head = (f"  {tag} [phase-c/tdd] cluster {cluster_name} closes ({close_kind}) "
            f"with {stubs.render_count()} still unimplemented")
    if record is None:
        log(head + " — no augmentation function record for this cluster, so "
                   "none of them is accounted: " + ", ".join(site_label(s) for s in sites))
    elif not record.measured:
        log(head + f" — augmentation did not measure function execution "
                   f"({record.reason}), so none of them is accounted: "
            + ", ".join(site_label(s) for s in sites))
    else:
        log(head + ":")
        for site, entry in account.unreached:
            log(f"      unreached at augmentation (targeted attempt(s) "
                f"{', '.join(str(a) for a in entry.attempts) or 'none recorded'}): "
                f"{site_label(site)}")
        for site in account.executed:
            log(f"      EXECUTED at augmentation: {site_label(site)} — the "
                "original's suite entered this body and this cluster's suite "
                "closes without reaching it (the filled callers route around "
                "it, or the test that entered it is not in this oracle)")
        for site in account.unaccounted:
            log(f"      not accounted by the record: {site_label(site)}")
    emit("phase_c.tdd.close_stubs", data={
        "cluster": cluster_name, "close": close_kind,
        "record": ("absent" if record is None
                   else "measured" if record.measured else "unmeasured"),
        "unreached": [site_label(s) for s, _ in account.unreached],
        "executed_at_augmentation": [site_label(s) for s in account.executed],
        "unaccounted": [site_label(s) for s in account.unaccounted],
    })
    return account
