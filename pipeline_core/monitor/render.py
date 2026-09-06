"""Pure ANSI / pipeline-line text helpers for the monitor TUI.

These are the regex-free (R7) presentation-layer primitives the renderer reuses:
visible-width measurement (ANSI escape stripping), the color-wrapping helpers,
the two structured scrapes of pipeline-state / call-start log lines, and the
event-log line formatter. They hold no I/O and no module state, so they live in
:mod:`pipeline_core.monitor` beside the data readers (run_data / events_data)
rather than in the god-file ``monitor.py``.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from pipeline_core.monitor.fill_progress import ROUTES, FillProgress
from pipeline_core.monitor.iteration_steps import STEPS, step_index
from pipeline_core.monitor.oracle_gap import oracle_gap


def bold(s: str) -> str:      return f"\033[1m{s}\033[0m"
def dim(s: str) -> str:       return f"\033[2m{s}\033[0m"
def underline(s: str) -> str: return f"\033[4m{s}\033[0m"
def green(s: str) -> str:     return f"\033[32m{s}\033[0m"
def red(s: str) -> str:       return f"\033[31m{s}\033[0m"
def cyan(s: str) -> str:      return f"\033[36m{s}\033[0m"
def yellow(s: str) -> str:    return f"\033[33m{s}\033[0m"
def rev(s: str) -> str:       return f"\033[7m{s}\033[0m"   # reverse video (highlight)
def bg_white(s: str) -> str:  return f"\033[30;107m{s}\033[0m"  # black on bright white


def event_format(ev: dict[str, Any]) -> str:
    """Render a single event-log entry as one human-readable colored line."""
    ts = ev.get("ts", "")
    # Time portion only: HH:MM:SS.mmm
    if "T" in ts:
        time_part = ts.split("T", 1)[1].split("+")[0].split(".")
        ts_s = time_part[0] + ("." + time_part[1][:3] if len(time_part) > 1 else "")
    else:
        ts_s = ts[:12]

    verb = ev.get("event", "")
    if verb.endswith(".done") or verb.endswith(".pass") or verb.endswith(".success"):
        verb_col = green
    elif verb.endswith(".fail") or verb.endswith(".error"):
        verb_col = red
    elif verb.endswith(".start") or verb.endswith(".request"):
        verb_col = yellow
    elif verb.startswith("guard.") or verb.startswith("llm."):
        verb_col = cyan
    else:
        verb_col = bold

    fields = []
    for k in ("task", "cluster", "module", "file", "attempt"):
        v = ev.get(k)
        if v not in (None, "", "-"):
            fields.append(f"{k}={v}")
    if ev.get("duration_ms") is not None:
        fields.append(f"{ev['duration_ms']}ms")

    # Short data summary: prefer a couple of high-signal fields.
    data = ev.get("data") or {}
    data_bits = []
    for k in ("pct", "passed", "ok", "covered", "coverable", "pass_count", "fail_count",
              "returncode", "reason", "model"):
        if k in data:
            v = data[k]
            if isinstance(v, str) and len(v) > 30:
                v = v[:27] + "..."
            data_bits.append(f"{k}={v}")
    # Compact prompt content-addressed summary when present.
    for k in ("prompt", "response"):
        sub = data.get(k)
        if isinstance(sub, dict) and "char_count" in sub:
            data_bits.append(f"{k}={sub['char_count']}c")

    head = f"{dim(ts_s)} {verb_col(bold(verb)):<22}"
    tail = "  ".join(fields)
    extra = "  ".join(data_bits)
    return f"{head}  {dim(tail) if tail else ''}{('  ' + dim(extra)) if extra else ''}".rstrip()


def ansi_seq_len(s: str, i: int) -> int:
    r"""Length of the CSI escape sequence at ``s[i]`` (``\033[`` params final-letter),
    or 0 if none (regex-free, R7; mirrors ``\033\[[0-9;]*[a-zA-Z]``)."""
    if not s.startswith("\033[", i):
        return 0
    j = i + 2
    while j < len(s) and (s[j].isdigit() or s[j] == ";"):
        j += 1
    c = s[j] if j < len(s) else ""
    if ("a" <= c <= "z") or ("A" <= c <= "Z"):
        return j + 1 - i
    return 0


def plain(s: str) -> str:
    """``s`` with every ANSI CSI escape removed — i.e. its visible characters."""
    out: list[str] = []
    i = 0
    while i < len(s):
        seq = ansi_seq_len(s, i)
        if seq:
            i += seq
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def state_done_key(line: str) -> str | None:
    r"""Cluster key from a ``[<key>[/...]] done`` pipeline_state line, else None
    (regex-free, R7; mirrors ``\[([^\]/]+)(?:/[^\]]*)?\]\s+done\b``)."""
    if not line.startswith("["):
        return None
    rest = line[1:]
    e = 0
    while e < len(rest) and rest[e] not in "]/":
        e += 1
    if e == 0:
        return None                       # [^\]/]+ requires ≥1 char
    key = rest[:e]
    k = e
    if k < len(rest) and rest[k] == "/":  # optional /[^\]]*
        k += 1
        while k < len(rest) and rest[k] != "]":
            k += 1
    if k >= len(rest) or rest[k] != "]":
        return None
    tail = rest[k + 1:]
    stripped = tail.lstrip()
    if stripped == tail:                  # \s+ requires ≥1 whitespace
        return None
    nxt = stripped[4:5]
    if stripped.startswith("done") and not (nxt.isalnum() or nxt == "_"):  # done\b
        return key
    return None


def call_start_value(content: str) -> str | None:
    r"""The float-ish token from the first well-formed ``[CALL_START: <num>]``
    marker, else None (regex-free, R7; mirrors ``\[CALL_START: ([\d.]+)\]``)."""
    marker = "[CALL_START: "
    start = 0
    while (i := content.find(marker, start)) >= 0:
        j = i + len(marker)
        k = j
        while k < len(content) and (content[k].isdigit() or content[k] == "."):
            k += 1
        if k > j and k < len(content) and content[k] == "]":
            return content[j:k]
        start = i + 1
    return None


def driver_counter(state: dict[str, Any]) -> str:
    """The live outer loop's own counter.

    Under the TDD driver the per-fn engine runs with max_passes=1 and emits no
    pass.start at all once the frontier holds only behavioural re-fills, so
    `pass=n/N` renders as `?/?` for hours on a perfectly healthy run. The tdd
    iteration (+ its stall budget) is the counter that actually advances there.
    """
    if state.get("driver") == "tdd":
        stall = state.get("tdd_stall")
        maxs = state.get("tdd_max_stalls")
        budget = (f"  stall={stall}/{maxs}"
                  if stall is not None and maxs is not None else "")
        # Never substitute a plausible number for a missing one — inventing
        # "iter=1" is the same defect as the "?/?" this replaced: a counter
        # asserting a value the events never reported.
        return f"iter={state.get('tdd_iter') or '?'}{budget}"
    pass_n = state.get("pass_n") or "?"
    maxp = state.get("max_passes") or "?"
    return f"pass={pass_n}/{maxp}"


def progress_bar(pct: float, width: int = 18, label: str = "") -> str:
    """Filled/empty block bar, optionally with a label overlaid from the left."""
    filled = round(pct / 100 * width)
    s = list("█" * filled + "░" * (width - filled))
    if label and len(label) <= width:
        for i, ch in enumerate(label):
            s[i] = ch
    return "".join(s)


def _iso_ts_seconds(ts: str) -> float | None:
    """Epoch seconds from an events.jsonl ISO timestamp, else None."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fmt_dur(seconds: float) -> str:
    """Compact duration: 45s / 12m / 3.2h."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


PHASE_SEQ: tuple[str, ...] = (
    "task0a", "task0b", "task0c", "strip", "task1",
    "task2a", "task2b", "task2c", "task3", "task4",
)


def phase_bar_rows(done_keys: set[str], cur: str) -> list[str]:
    """The whole-run phase strip: ✓done / ▶current / ·pending across t0a…t4,
    plus the `current:` line's detail beneath it.

    Pure — the pipeline_state.md read is `run_data.phase_progress`. The strip
    one level down, inside t2a, is :func:`iteration_step_row`; the two share
    this vocabulary deliberately, because they answer the same question at two
    scales and an operator should not have to learn it twice.
    """
    done = set(done_keys)
    # The combined reconstruct marker means the whole loop finished.
    if "task2-4" in done:
        done |= {"task2a", "task2b", "task2c", "task3", "task4"}
    stage = cur.split("/", 1)[0].strip() if cur else ""
    if stage in ("task2", "task2-4"):
        stage = "task2c"
    cells: list[str] = []
    for p in PHASE_SEQ:
        short = p.replace("task", "t")
        if p == stage and p not in done:
            cells.append(cyan("▶" + short))
        elif p in done:
            cells.append(green("✓" + short))
        else:
            cells.append(dim("·" + short))
    rows = ["  phase    " + " ".join(cells)]
    detail = cur.split("/", 2)[-1].strip() if cur.count("/") >= 1 else ""
    if detail and detail != cur:
        rows.append("           " + dim(detail))
    return rows


def iteration_step_row(state: dict[str, Any], now: float) -> str | None:
    """Where inside the TDD iteration the loop is, as a strip.

    The same vocabulary as the `phase` strip at the top of the panel — ✓ done
    this iteration, ▶ current, · not reached — one level down: that strip
    stops at `t2a`, and t2a is a multi-hour loop of five steps
    (:mod:`pipeline_core.monitor.iteration_steps`). Without this row the panel
    could distinguish "the fill has been running for 40 minutes" from "cargo
    test has been hung for 40 minutes" only by reading the log by eye.

    The trailing reading is time-in-step, and it is the point of the row: a
    `sweep` that has been current for 20 minutes, or a `grade` that never
    advances, is a finding, where the same 20 minutes under `fill` is a
    healthy long generation. It is taken from `tdd_step_age_s` when the caller
    has a better clock for the current step than the event stream does (the
    sweep publishes no events, so its age is the pipeline log's own staleness)
    and from the transition stamp otherwise.

    Returns None until a step has been observed — never a guessed `sweep`,
    which would put a hang on screen that nothing measured.
    """
    step = state.get("tdd_step")
    if not isinstance(step, str) or step_index(step) < 0:
        return None
    cur = step_index(step)
    cells: list[str] = []
    for i, s in enumerate(STEPS):
        if i == cur:
            cells.append(cyan("▶" + s.id))
        elif i < cur:
            cells.append(green("✓" + s.id))
        else:
            cells.append(dim("·" + s.id))
    age = state.get("tdd_step_age_s")
    if not isinstance(age, (int, float)):
        since = _iso_ts_seconds(state.get("tdd_step_since") or "")
        age = max(0.0, now - since) if since is not None else None
    tail = dim(f"  {_fmt_dur(float(age))} in {step}") if age is not None else ""
    return "           step     " + "  ".join(cells) + tail


def in_flight_row(state: dict[str, Any], covered: set[str]) -> str | None:
    """The compact in-flight list — the FALLBACK for units the fill fold
    cannot place.

    In-flight fills are the parallel set (up to `PIPELINE_PARALLELISM` at
    once), longest-running first (the unit eating the iteration): ↻ a
    behavioural re-fill, ⊞ an impl block, ▤ a file, plain a per-fn, each with
    its elapsed time.

    Two suppressions, and both are the same rule — never print one unit twice.
    `covered` is the threads the worker gauges already carry with a live token
    count; listing both printed every in-flight fill twice on four consecutive
    rows. And once :func:`fill_rows` has listed the units with their fix-loop
    floor and stall, this row has strictly less to say than the rows above it,
    so it steps aside entirely. What is left for it is the case it was written
    for: a unit whose thread has no live stream file (an older record with no
    thread, or a call that has not opened its log yet).

    Returns `in-flight idle` when the events show nothing in flight at all —
    an empty row and a quiet loop are different states.
    """
    flight_all = state.get("in_flight") or []
    fill = state.get("fill")
    flight = ([] if (isinstance(fill, FillProgress) and fill.live)
              else [u for u in flight_all if u.get("thread") not in covered])
    if not flight:
        return f"           in-flight {dim('idle')}" if not flight_all else None
    bits = []
    for u in flight[:4]:
        age = u.get("age_s")
        shown = ("?" if age is None else f"{age}s" if age < 60 else f"{age // 60}m")
        kind = u.get("kind")
        tag = ("↻" if u.get("refill") else "⊞" if kind == "impl"
               else "▤" if kind == "file" else "")
        bits.append(f"{tag}{(u.get('label') or '?').splitlines()[0][:30]}({shown})")
    more = f"  +{len(flight) - 4}" if len(flight) > 4 else ""
    return f"           in-flight {cyan('  '.join(bits))}{dim(more)}"


def fill_rows(state: dict[str, Any], now: float) -> list[str]:
    """The fill block of the Phase C panel: the route strip plus one row per
    unit in flight.

    One level below :func:`iteration_step_row`, and it exists because that
    strip's answer stopped being useful the moment it became accurate: on run
    `1383168-eb151d09` (2026-08-29) iter 1 the sweep took 0.7 s and
    grade/aim/frontier finished inside two more, so `▶fill` stood alone for
    1 h 43 m. The two rows below split that span by route
    (:func:`fill_route_row`) and then by the body each route is writing
    (:func:`fill_unit_rows`).

    Composed here rather than in ``monitor.py`` so the panel gains a call
    instead of a block — the LOC ratchet's rule, and the reason every other
    multi-row reading on this panel already has an entry point of its own.
    """
    fill = state.get("fill")
    if not isinstance(fill, FillProgress) or not fill:
        return []
    row = fill_route_row(fill)
    return ([row] if row else []) + fill_unit_rows(fill, now)


def fill_route_row(fill: FillProgress) -> str | None:
    """One level below the `step` strip: WHERE INSIDE the fill the loop is.

    Same vocabulary as the `phase` and `step` strips above it, because it is
    the same question at a third scale and the operator should not have to
    learn it three times. It exists because the step strip's answer stopped
    being useful the moment it was accurate: on run `1383168-eb151d09`
    (2026-08-29) iter 1 the four steps before the fill finished in under two
    seconds and `▶fill` then stood alone for 1 h 43 m.

    The three routes fill units of different sizes — a whole file, one impl
    block, one fn (:mod:`pipeline_core.monitor.fill_progress`) — and a unit
    that fails one route falls through to the next, so the counters stay
    SEPARATE and are never summed into a single fill bar: on that iteration
    `context` and `construct_from_boxed` were attempted by all three, and a
    summed bar would have counted the iteration's two hardest units three
    times each.

    A route draws a bar only against the unit count IT published — the
    file/impl routes on `phase_c.fill.route_plan`, the per-fn route on
    `phase_c.pass.start`. Records written before those emits carry no plan,
    and such a route shows `finished/started` with a `~` and no bar rather
    than borrowing the frontier or the dispatch cap as a denominator. The
    batch is named at the end of the row for the same reason it is not the
    denominator: it counts frontier UNITS the sweep dispatched, which is not
    the count of units any one route walks.
    """
    if not fill.routes:
        return None
    cells: list[str] = []
    for r in fill.routes:
        denom = r.denominator
        # Without a plan the only honest fraction is "closed of opened", and
        # it is labelled `seen` so a route that has closed the one unit it
        # opened cannot be read as a route that has closed its whole plan.
        count = (f"{r.finished}/{denom}" if denom is not None
                 else f"{r.finished}/{r.started} seen")
        if r.id == fill.current:
            body = f"▶{r.id} {count}"
            if denom:
                body += f" [{progress_bar(r.finished / denom * 100, 10)}]"
            cells.append(cyan(body))
        elif r.finished >= r.started > 0:
            cells.append(green(f"✓{r.id} {count}"))
        else:
            cells.append(dim(f"·{r.id} {count}"))
    for unseen in ROUTES:
        if not any(x.id == unseen for x in fill.routes):
            cells.append(dim("·" + unseen))
    tail = dim(f"  batch {fill.batch}") if fill.batch else ""
    return "           fill     " + "  ".join(cells) + tail


def fill_unit_rows(fill: FillProgress, now: float, limit: int = 4) -> list[str]:
    """One row per unit currently being filled — the answer to "which body".

    The label alone was already on screen (the worker gauges carry it beside
    their token bar), and the label alone is not the reading: a unit that has
    been in flight for 40 minutes is either converging or re-buying the same
    wrong answer, and only the fix loop's own gauge separates those. So each
    row carries what `loop.iter` publishes per failed attempt — the attempt
    count, the FLOOR (best metric the unit has reached) and the STALL (attempts
    since that floor last improved).

    Read the pair, not the elapsed time. A falling floor is the model closing
    on the body and the minutes are being spent well; a stall climbing at a
    fixed floor is the shape that ends in a revert, and it is visible here
    attempts before the route logs it. `attempt N` with an empty floor is a
    unit whose first answer has not come back yet — never a zero passed off as
    a measurement.

    The file route is the only one with an attempt CEILING
    (`PHASE_C_FILE_MAX_ATTEMPTS`, carried on `file.fill.revert`), so it is the
    only one that renders `2/3`. The impl and per-fn loops stop on a floor
    stall whose bound is adjusted per module before use and is not in the
    stream, so their attempt is a count and is printed as one.
    """
    rows: list[str] = []
    units = sorted(fill.live, key=lambda u: _iso_ts_seconds(u.since_iso) or now)
    for u in units[:limit]:
        tag = ("↻" if u.refill else "⊞" if u.route == "impl"
               else "▤" if u.route == "file" else " ")
        # Pad BEFORE colouring: an f-string width counts the ANSI escape bytes,
        # so `f"{cyan(x):<26}"` pads a coloured label to nothing and every
        # column right of it stops lining up (the same trap
        # `monitor/worker_streams.py` names).
        seg = [f"{tag} " + cyan(f"{u.label.splitlines()[0][:26]:<26}")]
        since = _iso_ts_seconds(u.since_iso)
        if since is not None:
            seg.append(dim(f"{_fmt_dur(max(0.0, now - since)):>5}"))
        if u.members > 1:
            seg.append(dim(f"{u.members} member(s)"))
        if u.attempt:
            att = (f"attempt {u.attempt}/{u.max_attempts}" if u.max_attempts
                   else f"attempt {u.attempt}")
            seg.append(att)
        if u.floor:
            seg.append(dim(f"floor {u.floor}"))
        if u.stalled:
            seg.append(yellow(f"stall {u.stalled}"))
        elif u.improving:
            seg.append(green("floor fell"))
        rows.append("           unit     " + "  ".join(seg))
    if len(units) > limit:
        rows.append("           " + dim(f"         +{len(units) - limit} more in flight"))
    return rows


def score_row(state: dict[str, Any]) -> str | None:
    """The target, as a bar: tests passing out of the oracle the sweep counted.

    The panel had every component of this number (`pass=9 fail=94` on the tdd
    row, `oracle_size` nowhere) and no picture of the distance left, so the one
    quantity the whole run exists to move was the only one with no gauge.

    Two denominators, and the difference is the finding, not a detail. The bar
    is drawn against `oracle_size` — `pass + fail`, what the sweep counted —
    while `oracle_static` is what the cluster's test inventory declares.
    Folding the difference into the denominator would make the score look worse
    than the work, so it is named beside the bar instead — split by cause
    (:mod:`pipeline_core.monitor.oracle_gap`), never as one lump.

    It used to render as `N unreachable (crashed binaries)`, which named the
    wrong tests: a crashed binary's lost tests are COUNTED, as failing
    (`ORACLE SHRUNK — counting 13 as failing`), and the gap is the verdicts a
    crashed binary printed before aborting — invisible to counters summed from
    libtest trailers — plus the tests that never ran at all. On cluster
    `stable`, 2026-08-29, that mislabel hid five passing tests inside a "12
    unreachable".

    The delta is against the previous sweep of this cluster, and it is signed:
    a re-stub by the crash/layout retrigger is a deliberate, expected drop, and
    a drop that reads as a drop is the point.
    """
    passed = state.get("tdd_pass")
    oracle = state.get("tdd_oracle")
    if passed is None or not oracle:
        return None
    pct = min(100.0, passed / oracle * 100)
    seg = [f"{passed}/{oracle} passing ({pct:.0f}%)"]
    hist = state.get("tdd_pass_hist") or []
    if len(hist) >= 2:
        delta = hist[-1] - hist[-2]
        seg.append(green(f"↑{delta}") if delta > 0
                   else red(f"↓{-delta}") if delta < 0 else dim("="))
    failing = state.get("tdd_fail")
    if failing is not None:
        seg.append(red(f"{failing} failing") if failing else green("0 failing"))
    return f"           score    [{progress_bar(pct, 24)}]  {'  '.join(seg)}"


def oracle_row(state: dict[str, Any]) -> str | None:
    """What the cluster DECLARES, and what of it the sweep did not count.

    Its own row rather than a third segment of the score row: the honest
    sentence is too long to survive `box_lines`' truncation there (the score
    row is ~80 visible columns before it), and the segment it replaces was
    cut mid-phrase on any terminal narrower than ~115 columns — which is how
    a wrong four-word label went unread for weeks.

    Absent when the sweep counted everything the inventory declares, so the
    row appears exactly when the measurement is short and says by how much
    and why (:mod:`pipeline_core.monitor.oracle_gap`).
    """
    gap = oracle_gap(state)
    if gap is None:
        return None
    static = state.get("tdd_oracle_static")
    return (f"           oracle   {dim(f'{static} declared')}  "
            f"{yellow(gap.rendered)}")


def iteration_progress_rows(state: dict[str, Any], now: float) -> list[str]:
    """Progress INSIDE the current TDD iteration — where the hours go.

    The between-iteration measures already live on neighbouring rows (the
    tdd row's pass=/fail= with its history chain); repeating the pass count
    here duplicated them, and anything keyed to the sweep only moves every
    few hours. This row shows what observably moves within an iteration:

    - the pace bar — time elapsed in this iteration against the median
      sweep-to-sweep gap of this run. It advances on every refresh. A pace
      reference, deliberately not an ETA: iterations legitimately overrun
      the median (the bar caps full while the numbers keep counting), and
      the score an iteration ends on is a step function over coupled
      components — a single sweep has moved it 13→83.
    - fills — completed fill units this iteration (per-fn fills, impl-block
      members, whole-file fns; re-fills and retries included): the counter
      that ticks every few minutes on a healthy run, with the age of the
      newest fill beside it. A growing age while the pipeline is RUNNING is
      the finest-grain stall signal the events carry.
    - the frontier the iteration is working through, as the sweep line
      printed it. Fills routinely exceed it (file-first fills touch
      non-frontier fns), so the two are labeled apart, never slash-joined —
      the row this replaced rendered `frontier: 65/22` and invited reading
      "65 out of 22".
    - the sweep bar — fill units completed against the batch the sweep
      opened with, the in-iteration progress that keeps moving after the
      `bodies` bar retires. The batch is the pass.start stub snapshot when
      there is one and the sweep's own frontier otherwise; which of the two
      is on screen is named in the row. Overflow past the denominator is
      labeled, not folded in.

    A second `bodies` row — filled fns out of the cluster's FILLABLE count
    (cfg-gated fns excluded from both sides), straight from the pass.start
    snapshot — shows while any fillable fn is still hollow, and disappears
    once every one has a body: the saturated bar it replaced sat at 100%
    for hours while the loop rewrote wrong bodies, teaching nothing.
    """
    rows: list[str] = []
    stamps = [t for t in (_iso_ts_seconds(s)
                          for s in state.get("tdd_iter_ts") or [])
              if t is not None]
    done = state.get("pass_fills_done", 0) or 0
    failed = state.get("pass_fills_failed", 0) or 0
    if stamps:
        elapsed = max(0.0, now - stamps[-1])
        deltas = [b - a for a, b in zip(stamps, stamps[1:], strict=False)
                  if b > a]
        if deltas:
            med = median(deltas)
            pace = min(100.0, elapsed / med * 100) if med > 0 else 0.0
            head = (f"[{progress_bar(pace, 24)}]  "
                    f"{_fmt_dur(elapsed)} of ~{_fmt_dur(med)} typical")
        else:
            head = f"{_fmt_dur(elapsed)} elapsed (first iter, no typical yet)"
        bits = [f"fills {done + failed}"]
        last_fill = _iso_ts_seconds(state.get("last_fill_ts_iso") or "")
        if last_fill is not None:
            bits.append(f"last {_fmt_dur(max(0.0, now - last_fill))} ago")
        if state.get("tdd_frontier"):
            bits.append(f"frontier {state['tdd_frontier']} fn(s)")
        rows.append(f"           iter     {head}   "
                    f"{dim(' · '.join(bits))}".rstrip())

    # The sweep row — completed fill units against the frontier batch the
    # sweep opened with. This is the counter the pipeline log prints as
    # `N/34`, and it is the only within-iteration progress signal that
    # survives once every fn has a body (the `bodies` bar below hides itself
    # then) — without it the panel showed no forward motion for the whole
    # 9-hour iter 3 of run 1159463-b1e352e5. Fill units from the file/impl
    # passes count members, so the numerator can pass the denominator.
    #
    # PAST the denominator there is no bar. A bar pinned at 100% is not a
    # progress reading, it is a decoration that outlives its own measurement:
    # on run 669347-cf33eae3 (2026-08-15) the row read `69/69 frontier fn(s)
    # … +10 beyond frontier` for the whole of a 46-minute iteration, which is
    # what "the monitor shows nothing but 69/69" means. Once the units exceed
    # the batch the honest reading is throughput — units landed, units in
    # flight, and the rate they are landing at — so that is what replaces it,
    # and the batch it has passed is named rather than divided into.
    #
    # The batch has two sources and needs both, because pass.start's stub
    # snapshot is not always emitted. The per-fn engine emits it only when it
    # has something to fill (`if not stubs and not _refill_recs: break`,
    # application/reconstruct/phase_c_engine.py), so a tree whose every fn
    # already has a body publishes no snapshot at all: on run
    # 500301-6a66bf86 (2026-08-15) iter 1 opened a 61-fn frontier of pure
    # behavioural re-fills and dispatched all of it through the file-first
    # route — nine file units, every one logging `0 stub(s), N re-fill(s)` —
    # so `pass_stubs_total` stayed 0 and 35 landed fills drew no bar of any
    # kind, on the same iteration this row exists to measure. The frontier
    # (phase_c.tdd.iter's `frontier_size`) is the honest batch there: it IS
    # the set the sweep opened with. The two denominators count different
    # things, so the row names which one it is rather than letting a reader
    # assume stubs.
    queue_total = state.get("pass_stubs_total", 0) or 0
    _frontier = state.get("tdd_frontier")
    frontier = _frontier if isinstance(_frontier, int) else 0
    denom, denom_unit = ((queue_total, "unit(s)") if queue_total > 0
                         else (frontier, "frontier fn(s)"))
    units = done + failed
    verdict = (green(f"{done} ✓") + " "
               + (red(f"{failed} ✗") if failed else dim("0 ✗")))
    in_flight = len(state.get("in_flight") or [])
    if denom > 0 and units < denom:
        spct = units / denom * 100
        rows.append(f"           sweep    [{progress_bar(spct, 24)}]  "
                    f"{units}/{denom} {denom_unit}  {verdict}"
                    + (f"  {dim(f'{in_flight} in flight')}" if in_flight else ""))
    elif units > 0 or in_flight:
        seg = [f"{units} unit(s)", verdict]
        if in_flight:
            seg.append(cyan(f"{in_flight} in flight"))
        if stamps:
            mins = max(0.0, now - stamps[-1]) / 60
            if mins >= 1 and units:
                seg.append(dim(f"{units / mins:.1f}/min"))
        if denom > 0:
            seg.append(dim(f"past this iter's {denom} {denom_unit}"))
        rows.append(f"           sweep    {'  '.join(seg)}")

    # `remaining` — the same two counts above, restated as "how much is left"
    # instead of "how much is done", so a reader does not have to subtract by
    # hand: 20 - 9 for the per-fn route, 20 - 17 for the sweep. Both come from
    # counters this function already trusts (`r.finished`/`r.denominator`,
    # `units`/`denom` from the sweep block just above), so nothing new is
    # measured or guessed here.
    #
    # Deliberately excludes `tdd_frontier`: that count is a different,
    # whole-cluster pool (the `iter` row's `frontier N fn(s)`, unaffected by
    # this sweep's dispatch), and `frontier - batch` would assume a
    # relationship between two denominators this module has never verified —
    # exactly the folding the sweep bar's own comment above warns against.
    fill = state.get("fill")
    if isinstance(fill, FillProgress) and fill:
        route_bits: list[str] = []
        for r in fill.routes:
            rdenom = r.denominator
            if rdenom is None:
                continue
            left = rdenom - r.finished
            if left > 0:
                route_bits.append(f"{r.id} {left}")
        rem_seg: list[str] = []
        if route_bits:
            rem_seg.append("fill " + ", ".join(route_bits))
        if denom > 0:
            sweep_left = denom - units
            if sweep_left > 0:
                rem_seg.append(f"sweep {sweep_left} {denom_unit}")
        if rem_seg:
            rows.append(f"           remaining {'  ·  '.join(rem_seg)}")

    total = state.get("total_fns_in_cluster", 0) or 0
    filled_start = state.get("filled_count_at_pass_start", 0) or 0
    hollow_start = state.get("all_stubs_remaining_at_pass_start", 0) or 0
    queue = state.get("pass_stubs_total", 0) or 0
    # The pass.start snapshot alone — never `filled_start + done`. `done`
    # counts fill UNITS, and a behavioral re-fill of an already-filled body is
    # a unit too: on run 3368-753757e6 iter 3 (2026-08-15, 197 filled of 209
    # fillable, 12 hollow) the re-fill sweep pushed done to 130, so
    # filled+done capped at the 223 total, "hollow" read 0, and the bar hid
    # itself exactly while the user was asking where the fill bar went. The
    # denominator is the FILLABLE count: the cluster total also counts fns
    # cfg-gated to other clusters (14 for anyhow/stable), which no amount of
    # filling here ever closes, so a bar over the total can never honestly
    # complete. The snapshot refreshes on each pass.start, which is at most
    # once per tdd iter and on a fully-bodied tree is never (see the sweep
    # bar above) — so it can be one iteration stale, deliberately: a slow
    # whole-cluster reading from last iter beats no reading. A drop in it is
    # the crash/layout retrigger's deliberate re-stub.
    fillable = filled_start + hollow_start
    if fillable > 0:
        if hollow_start > 0:
            bpct = filled_start / fillable * 100
            gated = max(0, total - fillable)
            line = (f"           bodies   [{progress_bar(bpct, 24)}]  "
                    f"{filled_start}/{fillable} fillable ({int(bpct)}%) "
                    f"· {hollow_start} hollow")
            if gated:
                line += f" · {gated} cfg-gated elsewhere"
            rows.append(line)
    elif total > 0 and queue > 0:
        # Events predate the filled_count snapshot: infer from the queue. The
        # inference undercounts when the TDD driver narrows the queue to a
        # per-iter frontier, hence the marker.
        cum = max(0, total - queue + done)
        bpct = cum / total * 100
        rows.append(f"           bodies   [{progress_bar(bpct, 24)}]  "
                    f"~{cum}/{total} fns ({int(bpct)}%, inferred)")
    return rows


def loop_hierarchy_rows(
    *,
    clusters: list[str],
    done_set: set[str],
    cur_cl: str,
    prog: dict[str, Any],
    phase_c: dict[str, Any],
) -> list[str]:
    """The cluster → attempt → file → retry rows.

        cluster  ✓stable  ▶nightly  ○...       [██░░░░░░] 1/3
          └ attempt                            [████░░░░] 2/10
              └ file  context.rs               [████████] 4/8
                   └ retry                     [██░░░░░░] 1/2   (only when active)

    Levels 1-3 are fed ONLY by `task0c.*` events, and a RESUMED run skips
    task0c entirely — the clusters are baked and the finished ones are
    checkpointed. Rendering them then gives `attempt ?/? · file ? 0/0` for the
    whole run: honest and useless. The counter that DOES move under the TDD
    driver is its own iteration, and that is what takes their place.
    """
    rows: list[str] = []
    n_clusters = len(clusters)
    n_done_cl = len(done_set)
    cl_i = next((i for i, n in enumerate(clusters) if n == cur_cl), n_done_cl)
    cl_pct = (cl_i / n_clusters * 100) if n_clusters else 0
    cl_parts: list[str] = []
    for name in clusters:
        ab = name[:10]
        if name in done_set:
            cl_parts.append(green(f"✓{ab}"))
        elif name == cur_cl:
            cl_parts.append(yellow(bold(f"▶{ab}")))
        else:
            cl_parts.append(dim(f"○{ab}"))
    cl_bar = f"[{progress_bar(cl_pct, 10)}] {cl_i}/{n_clusters}" if n_clusters else ""
    rows.append(f"  cluster  {'  '.join(cl_parts)}  {dim(cl_bar)}")

    if not prog.get("has_task0c", True):
        bits = [f"iter {phase_c.get('tdd_iter') or 0}"]
        if phase_c.get("tdd_pass") is not None:
            bits.append(f"pass {phase_c['tdd_pass']}")
        stall, max_stall = phase_c.get("tdd_stall"), phase_c.get("tdd_max_stalls")
        if stall is not None and max_stall:
            bits.append(f"stall {stall}/{max_stall}")
        rows.append(f"    └ tdd  {dim('(no task0c this run)')}  {'  '.join(bits)}")
        return rows

    try:
        att_i, att_t = int(prog["attempt"]), int(prog["max_attempt"])
    except (ValueError, TypeError, KeyError):
        att_i = att_t = 0
    att_pct = (att_i / att_t * 100) if att_t else 0
    rows.append(f"    └ attempt  [{progress_bar(att_pct, 14)}] "
                f"{prog['attempt']}/{prog['max_attempt']}")

    _cl_files = prog["cluster_files"]
    all_f = (_cl_files if (cur_cl and cur_cl not in ("stable", "") and _cl_files)
             else prog["all_files"])
    this_p = prog["files_this_pass"]
    ft = max(len(all_f), len(this_p))
    fi = len(this_p)
    cf = prog["current_file"].replace("src/", "") if prog["current_file"] != "?" else "?"
    file_pct = (fi / ft * 100) if ft else 0
    rows.append(f"        └ file  {cyan(cf):<18}  [{progress_bar(file_pct, 12)}] {fi}/{ft}")

    ir, ir_max = prog.get("inner_retry", "?"), prog.get("inner_retry_max", "?")
    if ir != "?" and ir_max != "?":
        try:
            r_pct = int(ir) / int(ir_max) * 100
        except (ValueError, TypeError, ZeroDivisionError):
            r_pct = 0
        rows.append(f"             └ retry  [{progress_bar(r_pct, 10)}] {ir}/{ir_max}")
    return rows
