"""The phases view's state and the glue `monitor.py` would otherwise carry.

`monitor.py` is a god-file whose line count only shrinks (check.sh's LOC
ratchet), so the board loading, the detailed view's scroll/expand state and the
demo-mode banner live here and the file keeps one-line calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.monitor import phase_board, phase_view, run_data
from pipeline_core.monitor.render import bold, rev, yellow

__all__ = ["PhasePanel", "load_board", "demo_banner"]


def load_board(events_log: str, project_dir: str | None, running: bool) -> phase_board.Board:
    """This run's phase events over the state file's record of what earlier
    processes finished."""
    state, clusters = run_data.state_phase_statuses(project_dir)
    return phase_board.fold_board(run_data.read_phase_records(events_log), state,
                                  alive=running, clusters=clusters)


def demo_banner(interval: int) -> str:
    """Demo mode switches the view by itself; it must never be a surprise."""
    return rev(bold(yellow(
        f" [d]emo mode enabled — the view switches every {interval}s · press d to stop ")))


@dataclass
class PhasePanel:
    """Scroll and expansion of the `[e]` view. `top` is the first row shown;
    None opens on the running phase."""

    top: int | None = None
    expand_all: bool = False

    def reset(self) -> None:
        self.top = None

    def toggle_expand(self) -> None:
        self.expand_all = not self.expand_all
        self.top = None

    def scroll(self, delta: int) -> None:
        self.top = max(0, (self.top or 0) + delta)

    def window(self, board: phase_board.Board, width: int, inner: int,
               now: float) -> tuple[str, list[str]]:
        lines = phase_view.detail_lines(board, width, now, expand_all=self.expand_all)
        top = (self.top if self.top is not None
               else phase_view.detail_start(board, width, now, expand_all=self.expand_all))
        self.top = top = max(0, min(top, max(0, len(lines) - inner)))
        title = ("Pipeline phases — what each step does   [↑↓] scroll  [x] "
                 + ("collapse" if self.expand_all else "expand") + " finished  [e] close")
        return title, lines[top:top + inner]
