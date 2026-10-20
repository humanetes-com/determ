"""A sweep's pass count in each build world, and the one rule that compares two.

The one-tree loop's sweep runs every test command under each world and joins
the captures (`test_results.join_world_captures`), so its pass count is a sum
over worlds. A sum hides a world that got worse behind a world that got
better: a tree that gained two tests in the default world and lost one in the
other read as an improvement, moved the high-water and became the tree the
batch ratchet defends (found in review, 2026-10-20). Every consumer that asks
"is this tree at least as good as that one" asks it here, world by world.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.test_results import parse_test_counts, world_sections


@dataclass(frozen=True, slots=True)
class WorldScore:
    """Passing tests per world of one sweep; one-world captures key `""`."""

    passed: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of_capture(cls, capture: str) -> WorldScore:
        return cls({w: parse_test_counts(text)[0]
                    for w, text in world_sections(capture or "").items()})

    def lost_against(self, best: WorldScore | None) -> tuple[str, ...]:
        """The worlds in which this sweep passes fewer tests than `best`.

        Only worlds both readings hold are compared: a reading of another
        shape (a one-world mark carried over a resume) proves no loss, and
        the summed count still guards it."""
        if best is None:
            return ()
        return tuple(sorted(w for w, n in best.passed.items()
                            if w in self.passed and self.passed[w] < n))
