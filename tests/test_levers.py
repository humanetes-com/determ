"""The lever registry is only useful if every owner it names exists — a
refactor that moves a mechanism must break here, loudly, not silently orphan
a lever a future diagnosis verdict would select (application/levers.py)."""

from pipeline_core.application.levers import LEVERS, validate_registry


def test_every_lever_owner_imports_and_has_the_symbol():
    assert validate_registry() == []


def test_lever_names_are_unique_and_carry_their_contracts():
    names = [lv.name for lv in LEVERS]
    assert len(names) == len(set(names))
    for lv in LEVERS:
        # The evidence contract is the mechanical-validation hook for a
        # diagnosis verdict; an empty one would make the lever selectable
        # on no evidence at all.
        assert lv.evidence.strip(), lv.name
        assert lv.summary.strip(), lv.name
