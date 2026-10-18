"""A pipeline-code exception during a cluster's fill ends the run failed.

Run `486681-a304b30a` (2026-10-10): a TypeError at the first fill of each
cluster (a keyword the fill port lost) was caught, logged as exhaustion and
checkpointed done, and the process went on into the merge, the post-merge
wave and task3 on two trees that had never been filled. A TypeError in the
pipeline's own code makes a from-scratch reading invalid whatever follows it.

Decided by the user on 2026-10-17: once an exception escapes a cluster's fill
(it escapes `_generate_one_cluster`, or the driver catches it and returns
`ClusterFillResult.crashed`), every remaining cluster is scored by its entry
sweep only — one cargo test sweep, no fill, no model call — and reports its
red list; the merge, task2c and task3 are skipped; the process exits failed
with a `HEALTH ‼` line naming the exception and its traceback. A tool or
model failure that comes back as a result is not this: it keeps the loop's
own handling.
"""
from __future__ import annotations

from dataclasses import dataclass

# The process exit code of a run that stopped on a pipeline-code crash, apart
# from 1 (the run finished with red tests) and 2 (a refused launch).
PIPELINE_CODE_CRASH_EXIT = 9


@dataclass(frozen=True, slots=True)
class ClusterScore:
    """One cluster's reading at the end of a crashed run."""

    cluster: str
    passed: int
    failed: tuple[str, ...]
    how: str  # "crashed", "scored only", "filled", "checkpoint", "not scored"


@dataclass(frozen=True, slots=True)
class PipelineCrash:
    """The first pipeline-code exception of a run's cluster fills."""

    cluster: str
    exception: str
    traceback: str

    def health_line(self) -> str:
        return (f"HEALTH ‼ PIPELINE_CODE_CRASH in cluster '{self.cluster}': "
                f"{self.exception}. The run is invalid as a reading; the "
                "remaining clusters were scored by their entry sweep only, "
                "and the merge, task2c and task3 were skipped.")


class PipelineCodeCrash(Exception):
    """Raised once every cluster is scored; the orchestrator turns it into the
    `HEALTH ‼` line and a failed exit."""

    def __init__(self, crash: PipelineCrash,
                 scores: tuple[ClusterScore, ...] = ()) -> None:
        super().__init__(crash.exception)
        self.crash = crash
        self.scores = scores

    def report(self) -> list[str]:
        lines = [self.crash.health_line()]
        for s in self.scores:
            red = ", ".join(s.failed) if s.failed else "none"
            lines.append(f"  [{s.cluster}] {s.how}: pass={s.passed} "
                         f"red={len(s.failed)} ({red})")
        lines.append("Traceback of the crash:")
        lines.append(self.crash.traceback.rstrip())
        return lines
