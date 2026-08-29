"""Identity-contract AST gate: a planted FnLocator conviction write is red;
the live tree after site-at-write is clean."""
from __future__ import annotations

import ast

from tools.check_identity_contract import hits_in_tree, scan_core


def test_planted_convicting_setdefault_fnlocator_is_red() -> None:
    src = '''
from pipeline_core.domain.models import FnLocator
convicting = {}
convicting.setdefault(FnLocator("src/a.rs", "from"), set()).add("t")
'''
    hits = hits_in_tree(ast.parse(src))
    assert hits, "the planted dummy must be visible to the gate"
    assert any("setdefault" in lab for _ln, lab in hits)


def test_planted_convicting_frontier_annotation_is_red() -> None:
    """The pre-fix write was ``convicting: Frontier = {}``, not a
    ``setdefault(FnLocator(`` literal. That shape must fail the gate."""
    src = '''
from pipeline_core.domain.models import Frontier
convicting: Frontier = {}
'''
    hits = hits_in_tree(ast.parse(src))
    assert hits, "the Frontier annotation must be visible to the gate"
    assert any("Frontier" in lab for _ln, lab in hits)


def test_sitefrontier_annotation_is_not_a_hit() -> None:
    src = '''
from pipeline_core.domain.models import SiteFrontier
convicting: SiteFrontier = {}
'''
    assert hits_in_tree(ast.parse(src)) == []


def test_planted_convicting_mapping_fnlocator_is_red() -> None:
    src = '''
from collections.abc import Mapping
from pipeline_core.domain.models import FnLocator
convicting: Mapping[FnLocator, set[str]] = {}
'''
    hits = hits_in_tree(ast.parse(src))
    assert hits, "Mapping[FnLocator, …] on convicting must be visible"
    assert any("Frontier" in lab for _ln, lab in hits)


def test_dispatch_frontier_annotation_is_not_a_hit() -> None:
    src = '''
from pipeline_core.domain.models import Frontier
dispatch: Frontier = {}
'''
    assert hits_in_tree(ast.parse(src)) == []


def test_dispatch_setdefault_is_not_a_hit() -> None:
    src = '''
from pipeline_core.domain.models import FnLocator
merged = {}
merged.setdefault(FnLocator("src/a.rs", "from"), set()).add("t")
frontier.setdefault(FnLocator("src/a.rs", "fmt"), set())
'''
    assert hits_in_tree(ast.parse(src)) == []


def test_live_pipeline_core_has_no_fnlocator_conviction_writes() -> None:
    found = scan_core()
    assert found == [], found
