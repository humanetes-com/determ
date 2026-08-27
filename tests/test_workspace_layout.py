"""Unit tests for the workspace-layout domain rule used by --restart-from-augmented.

`drop_iteration_phases` is the pure rule that decides which pipeline_state phase
entries a restore-to-augmented wipes (strip / task1+) vs keeps (task0a/0b/0c).
"""
from __future__ import annotations

from pipeline_core.domain.workspace_layout import (
    ITERATION_PHASE_PREFIXES,
    drop_iteration_phases,
)


def test_drop_iteration_phases_keeps_task0_drops_iteration():
    phases = {
        "task0a/anyhow": "done",
        "task0b/anyhow": "done",
        "task0c/anyhow": "done",
        "strip/anyhow": "done",
        "task1/anyhow": "done",
        "task2-4/anyhow": "done",
        "task2/anyhow": "done",
        "task3/anyhow": "done",
        "task4/anyhow": "done",
    }
    kept = drop_iteration_phases(phases)
    assert kept == {
        "task0a/anyhow": "done",
        "task0b/anyhow": "done",
        "task0c/anyhow": "done",
    }


def test_drop_iteration_phases_empty():
    assert drop_iteration_phases({}) == {}


def test_iteration_prefixes_are_the_iteration_keys():
    # every declared prefix is dropped; an unrelated key survives.
    phases = {f"{p}m": "x" for p in ITERATION_PHASE_PREFIXES}
    phases["task0c/m"] = "x"
    assert drop_iteration_phases(phases) == {"task0c/m": "x"}


# --- the pre-augmentation restore point -----------------------------------

from pipeline_core.domain.workspace_layout import (  # noqa: E402
    PRE_AUGMENT_TAG_PREFIX,
    TASK0C_ATTEMPTS_LOG_NAME,
    drop_pre_augment_phases,
    plan_pre_augment_tags,
    pre_augment_tag,
)

# The live tmp/anyhow state file on 2026-08-26, key for key.
_LIVE_PHASES = {
    "cfg_scan": "done", "test_cmd": "done", "task0a-context": "done", "task0a": "done",
    "task0b": "done", "task0c/stable": "done", "task0c/stable/anyhow": "done",
    "task0c/nightly": "done", "task0c/nightly/anyhow": "done",
    "strip/anyhow": "done", "task1/anyhow": "done", "task2a/anyhow": "done",
    "task2a/anyhow/stable": "done", "task2b/anyhow": "done", "task2c/anyhow": "done",
    "task2-4/anyhow": "done",
}


def test_drop_pre_augment_phases_keeps_only_what_ran_before_task0c():
    assert drop_pre_augment_phases(_LIVE_PHASES) == {
        "cfg_scan": "done", "test_cmd": "done", "task0a-context": "done",
        "task0a": "done", "task0b": "done",
    }


def test_drop_pre_augment_phases_forgets_phases_that_do_not_exist_yet():
    """Keep-list, not drop-list: a phase added between task0b and task0c
    later (a task0d, a bake key) is dropped without anyone extending a list."""
    phases = dict(_LIVE_PHASES, **{"task0d/anyhow": "done", "bake/anyhow": "done"})
    kept = drop_pre_augment_phases(phases)
    assert "task0d/anyhow" not in kept and "bake/anyhow" not in kept
    assert kept == drop_pre_augment_phases(_LIVE_PHASES)


def test_drop_pre_augment_phases_is_stricter_than_the_augmented_drop():
    """The augmented restore keeps task0c; the pre-augment restore must not."""
    assert "task0c/stable" in drop_iteration_phases(_LIVE_PHASES)
    assert "task0c/stable" not in drop_pre_augment_phases(_LIVE_PHASES)
    assert drop_pre_augment_phases({}) == {}


def test_task0c_attempts_log_name_matches_the_writer():
    from pipeline_core.application.tasks.task0c import TASK0C_ATTEMPTS_LOG
    assert TASK0C_ATTEMPTS_LOG_NAME == TASK0C_ATTEMPTS_LOG


def test_pre_augment_tag_name():
    assert pre_augment_tag("anyhow") == "task0c-pre-anyhow"
    assert pre_augment_tag("anyhow").startswith(PRE_AUGMENT_TAG_PREFIX)


def test_plan_writes_the_tag_on_a_fresh_workspace():
    plan = plan_pre_augment_tags(["anyhow"], existing_tags=[], phases={"task0b": "done"})
    assert plan.to_write == ("task0c-pre-anyhow",)
    assert plan.present == () and plan.refusal is None
    assert "tagged task0c-pre-anyhow at HEAD" in plan.summary()


def test_plan_never_repoints_an_existing_tag():
    """On a resume HEAD is a post-t0c (or post-fill) commit; `git tag -f`
    there would silently move the restore point. Present tags are reported,
    not rewritten."""
    plan = plan_pre_augment_tags(["anyhow"], existing_tags=["task0c-pre-anyhow"],
                                 phases={"task0b": "done"})
    assert plan.to_write == () and plan.present == ("task0c-pre-anyhow",)
    assert "not re-tagged" in plan.summary()


def test_plan_refuses_a_workspace_whose_task0c_already_ran():
    """The live tmp/anyhow shape: no task0c-pre-* tag, HEAD a task2a checkpoint.
    Tagging HEAD would make the restore 'return' to a stripped tree and run
    task0c against no source; the plan refuses and names the by-hand command."""
    plan = plan_pre_augment_tags(["anyhow"], existing_tags=[], phases=_LIVE_PHASES)
    assert plan.to_write == () and plan.refusal
    assert plan.summary().startswith("NOT tagged")
    assert "git tag task0c-pre-<module> <sha>" in plan.summary()
    assert "task0c/nightly" in plan.summary()


def test_plan_is_per_module():
    plan = plan_pre_augment_tags(["a", "b"], existing_tags=["task0c-pre-a"], phases={})
    assert plan.to_write == ("task0c-pre-b",) and plan.present == ("task0c-pre-a",)


def test_augmentation_has_started_on_bare_and_prefixed_task0c():
    from pipeline_core.domain.workspace_layout import augmentation_has_started
    assert augmentation_has_started(["task0c"]) is True
    assert augmentation_has_started(["task0c/stable"]) is True
    assert augmentation_has_started(["task0b", "strip/anyhow"]) is False
    assert augmentation_has_started([]) is False
