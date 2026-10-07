"""t0d's reach round (pure): a probe aimed at each function no example executes.

After t0d's exercises run, the set of functions no harvested or probed example
enters is a computed fact (site-level hits, `behaviour_coverage.sites_for_hit_lines`).
For each one the world's MIR names a public route (`domain.fn_routes`), the
round asks for ONE program along it — build the value with the route's public
call, then render it the way the dump says reaches the target — and runs it on
the ORIGINAL crate twice like every capture. Whether it reached the target is
then read from its own coverage, never from the reply:

- `reached`: the probe's site-level hits name the target. The capture is
  emitted as a test with the others, so t0c's first measurement already
  executes the function and t0c never spends a pass on it.
- `probe_missed`: the probe ran and its hits do not name the target. t0c's
  prompt quotes that measured negative beside the route.
- `probe_failed`: no capture (it did not compile, did not run, printed
  nothing, did not reproduce). Recorded with the reason and the program that
  was asked, or t0c would receive neither an emitted test nor the negative.

The ask names only public items: the target's own name never reaches the
model, so a probe stays black-box like every other behaviour t0d records.
A target with no route is not asked: a public-API program cannot be aimed at
it, and saying so is the route's job.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pipeline_core.domain.fn_reachability import idents
from pipeline_core.domain.fn_routes import FnRoutes, PublicRoute, WorldFacts
from pipeline_core.domain.rust_idents import container_identity

REACHED = "reached"
PROBE_MISSED = "probe_missed"
PROBE_FAILED = "probe_failed"
_OUTCOMES = (REACHED, PROBE_MISSED, PROBE_FAILED)


@dataclass(frozen=True, slots=True)
class ReachTarget:
    label: str
    route: PublicRoute
    observe: str
    # What to do with the built value, in public terms.
    action: str


@dataclass(frozen=True, slots=True)
class ReachOutcome:
    label: str
    outcome: str
    behaviour_id: str = ""
    given: str = ""
    when: str = ""
    observe: str = ""
    reason: str = ""

    def _program(self) -> str:
        """The asked program, in the words the measurement has (`""` without one)."""
        given = " ".join(self.given.split())
        when = " ".join(self.when.split())
        if not given and not when:
            return ""
        render = _RENDER.get(self.observe, self.observe)
        return f"a program that built `{given[:200]}` and rendered `{when}` with {render}"

    def prompt_line(self) -> str:
        """The measured fact, for t0c's NEVER EXECUTED block ("" when reached).

        A miss says this program ran and its hits omit the function. A failure
        quotes the same program with the reason. Neither states a cause."""
        program = self._program()
        if self.outcome == PROBE_MISSED:
            return (f"// Measured on the original crate: {program} ran and did "
                    "NOT enter this function")
        if self.outcome == PROBE_FAILED:
            reason = " ".join(self.reason.split())[:240]
            if program:
                tail = f": {reason}" if reason else ""
                return (f"// Measured on the original crate: {program} did not capture{tail}")
            return ("// A program along this route did not capture on the original crate: "
                    + reason)
        return ""


_RENDER = {"display": "`{}`", "debug": "`{:?}`", "alt_debug": "`{:#?}`"}


def _container(label: str) -> str:
    return label.split(" [", 1)[1][:-1] if label.endswith("]") and " [" in label else ""


def _trait_of(container: str) -> tuple[str, str]:
    """`(trait last ident, full trait path text)` of an impl container."""
    path = container_identity(container).trait if container.startswith("impl") else ()
    if not path:
        return "", ""
    toks = idents(path[-1])
    return (toks[0] if toks else ""), "::".join(path)


def target_for(label: str, routes: FnRoutes) -> ReachTarget | None:
    if not routes.routes:
        return None
    route = routes.routes[0]
    trait, _ = _trait_of(_container(label))
    name = label.split("::")[-1].split(" [", 1)[0]
    if trait == "Debug":
        alt = any(g.flag == "alternate" for g in routes.gated)
        observe = "alt_debug" if alt else "debug"
        action = "render the value with `{:#?}`" if alt else "render the value with `{:?}`"
    elif trait == "Display":
        observe, action = "display", "render the value with `{}`"
    elif trait:
        observe = "debug"
        action = (f"call the trait method `{trait}::{name}` on the value and render what it "
                  "returns with `{:?}`")
    else:
        observe, action = "debug", "render the public call's result with `{:?}`"
    return ReachTarget(label, route, observe, action)


def reach_targets(facts: WorldFacts | None, executed: Iterable[str]) -> tuple[ReachTarget, ...]:
    """Every routed function of the world that no example's site-level hits
    name, in label order."""
    if facts is None:
        return ()
    done = frozenset(executed)
    out: list[ReachTarget] = []
    for label in sorted(facts.routes):
        if label in done:
            continue
        t = target_for(label, facts.routes[label])
        if t is not None:
            out.append(t)
    return tuple(out)


def side_hint(route: PublicRoute) -> str:
    return {"Err": " — the value is the `Err` of its result: take it with `.unwrap_err()`",
            "Ok": " — the value is the `Ok` of its result: take it with `.unwrap()`",
            "Some": " — the value is the `Some` of its result: take it with `.unwrap()`"}.get(route.side, "")


def outcomes_to_json(cluster: str, outcomes: Iterable[ReachOutcome]) -> str:
    return json.dumps({"cfg_cluster": cluster, "outcomes": [
        {"label": o.label, "outcome": o.outcome, "behaviour_id": o.behaviour_id,
         "given": o.given, "when": o.when, "observe": o.observe, "reason": o.reason}
        for o in outcomes]}, indent=2, sort_keys=True) + "\n"


def outcomes_from_json(text: str) -> dict[str, ReachOutcome]:
    """`label -> outcome`; {} for a text this reader does not understand."""
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    rows = raw.get("outcomes") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, ReachOutcome] = {}
    for r in rows:
        if not isinstance(r, Mapping) or r.get("outcome") not in _OUTCOMES:
            continue
        o = ReachOutcome(*(str(r.get(k, "")) for k in
                           ("label", "outcome", "behaviour_id", "given", "when", "observe", "reason")))
        out[o.label] = o
    return out
