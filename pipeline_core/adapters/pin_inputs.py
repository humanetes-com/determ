"""What the pinning-test writer is shown about one blocking body (adapter).

Every input is a fact the pipeline already holds after strip, none of it an
original body (Hard Rule 11): the stub inventory's cfg predicates (which
worlds compile the body), the construction facts baked before strip (the
public calls the original's MIR records as reaching it), the rebuilt tree's
own records (the body's signature), and the oracle's own test files (the
crate-level header a test of that world carries).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..domain.attribution import canon_site
from ..domain.cfg import CfgSet, unreachable_under_every_cfg_set
from ..domain.fn_execution import site_label
from ..domain.fn_routes import WorldFacts
from ..domain.generation import parse_gen_tests_integration_filename
from ..domain.models import FnSite

__all__ = ["worlds_compiling", "route_lines", "signature_text", "example_header"]

_HEADER_LINES = 30


def _key(site: FnSite) -> tuple[str, str, str]:
    c = canon_site(site)
    return c.file, c.fn_name, "".join(c.container.split())


def worlds_compiling(site: FnSite,
                     site_preds: Iterable[tuple[FnSite, tuple[str, ...] | None]],
                     clusters: Sequence[Any]) -> list[Any]:
    """The baked worlds whose cfg-set compiles `site`. A site the inventory
    holds no predicate for is compiled by every world."""
    preds: tuple[str, ...] | None = None
    for s, p in site_preds:
        if _key(s) == _key(site):
            preds = p
            break
    if not preds:
        return list(clusters)
    return [c for c in clusters
            if not unreachable_under_every_cfg_set(
                preds, [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)])]


def route_lines(worlds: Sequence[WorldFacts], world_name: str, site: FnSite) -> list[str]:
    """The route comment lines the world's facts hold for `site`, matched
    with the container's spacing folded (the facts are keyed by the
    extractor's rendering of the original; the inventory by the rebuilt
    tree's)."""
    want = "".join(site_label(canon_site(site)).split())
    for w in worlds:
        if w.cluster != world_name:
            continue
        for label, routes in w.routes.items():
            if "".join(label.split()) == want:
                return routes.lines()
        hit = w.routes_for(site)
        return hit.lines()
    return []


def signature_text(records: Iterable[dict[str, Any]], root: Path, site: FnSite) -> str:
    """The body's signature as the rebuilt tree spells it, with its impl
    header, or "" when the record is not found."""
    for r in records:
        rec_site = FnSite(str(r.get("file", "")), str(r.get("fn_name", "")),
                          str(r.get("container", "") or ""))
        if _key(rec_site) != _key(site):
            continue
        try:
            raw = (root / rec_site.file).read_bytes()
        except OSError:
            return ""
        start, body = int(r.get("sig_byte_start", 0)), int(r.get("body_byte_start", 0))
        sig = raw[start:body].decode("utf-8", errors="replace").strip()
        head = f"// in `{rec_site.container}`\n" if rec_site.container else ""
        return head + sig + " { unimplemented!() }"
    return ""


def example_header(project_dir: Path, gate: str) -> str:
    """The crate-level attribute and import lines of a generated oracle test
    file gated exactly by `gate` ("" gate: one with no `#![cfg(` line). Of
    the matching files, the one with the most crate-level attributes: a
    world's feature gates (`#![feature(...)]`) sit in some of its files and
    not others, and a test that needs one does not build without it."""
    tests = project_dir / "tests"
    if not tests.is_dir():
        return ""
    want = gate.strip()
    best: tuple[int, str] = (-1, "")
    for p in sorted(tests.glob("*.rs")):
        if parse_gen_tests_integration_filename(p.name) is None:
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        first_cfg = next((ln.strip() for ln in lines if ln.strip().startswith("#![cfg(")), "")
        if first_cfg != want:
            continue
        # Top-level lines only (a `use` inside a test body is not the
        # header), and not the gate: the pipeline writes that line itself.
        head = [ln for ln in lines[:_HEADER_LINES]
                if ln.startswith(("#![", "use ", "extern crate", "#[allow("))
                and not ln.startswith("#![cfg(")]
        n_attrs = sum(1 for ln in head if ln.startswith("#!["))
        if n_attrs > best[0]:
            best = (n_attrs, "\n".join(head).strip())
    return best[1]
