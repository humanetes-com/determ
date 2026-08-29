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
