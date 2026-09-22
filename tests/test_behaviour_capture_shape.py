"""The constrained capture shape: one source, two roles, a typed Then.

WHY THESE EXIST. Measured 2026-08-27 on this crate and model: the previous
prompt ("a self-contained `fn main()` … PRINTS the observable") produced 0 of 6
mechanically-assertable captures — 3 recorded on disk plus 3 re-run as the
experiment's control — every one a multi-`println!` blob stored as
NORMALIZED_STDOUT and classified OTHER. `behaviours_pinned_by` joins on an exact
`then.value`, so no generated test could ever pin one, and `MUST_CLASS_ORDER`
ranks OTHER last so t1.5's cap discarded them. The constrained shape produced
7 of 7 across two variants. Each test below pins one link of the chain that
turns that into a real check, and the blob shape is the negative control.
"""

from __future__ import annotations

import pytest

from pipeline_core.application.emit_behaviour_tests import (
    render_behaviour_test_file,
    replay_fn_name,
    replayable,
)
from pipeline_core.domain.behaviour import BehaviourClass, OutcomeKind
from pipeline_core.domain.behaviour_probe import (
    CapturedProbe,
    captured_spec,
    class_targets,
    identity_type_of,
    normalize_stdout,
    parse_behaviour_specs,
    perturbed_env,
    printed_value,
    probe_spec,
    program_for,
    render_probe_program,
    render_replay_test,
    value_bearing_reason,
)
from pipeline_core.domain.behaviour_select import behaviour_class_name
from pipeline_core.domain.language_driver import using_driver
from pipeline_core.prompts.language_catalog import catalog_driver


def _probe(**kw: str) -> CapturedProbe:
    base = {"name": "b", "given": '    let e = anyhow!("x");', "when": "e.to_string()", "observe": "display"}
    base.update(kw)
    return CapturedProbe(**base)  # type: ignore[arg-type]


# --- the value-bearing rule -------------------------------------------------


def test_a_comparison_when_is_refused_because_it_discards_the_value() -> None:
    """Experiment variant B produced `format!("{}", b) == "world"`, whose
    captured Then is `true` — it embeds the expected value in the expression and
    then observes a boolean carrying no information."""
    why = value_bearing_reason('format!("{}", b) == "world"')
    assert why is not None and "compares" in why
    for expr in ("a != b", "n >= 3", 'x.contains("y")'):
        assert value_bearing_reason(expr) is not None


def test_identity_predicates_are_the_one_admissible_boolean() -> None:
    """For an identity the boolean IS the observable, and the type is what the
    pin must name — the same carve-out `weak_assertion_reason` makes for
    generated tests."""
    assert value_bearing_reason("err.is::<io::Error>()") is None
    assert value_bearing_reason("e.downcast_ref::<String>().is_some()") is None
    assert identity_type_of("err.is::<io::Error>()") == "io::Error"
    assert identity_type_of("e.downcast_ref::<Vec<u8>>().is_some()") == "Vec<u8>"
    assert identity_type_of("e.to_string()") is None


def test_a_when_that_prints_or_asserts_or_spans_lines_is_refused() -> None:
    assert value_bearing_reason('println!("{}", x)') is not None
    assert value_bearing_reason("assert_eq!(a, b)") is not None
    assert value_bearing_reason("let a = 1;\nа") is not None
    assert value_bearing_reason("   ") is not None


# --- one source, two roles --------------------------------------------------


def test_capture_and_replay_render_from_the_same_three_parts() -> None:
    spec = captured_spec(cfg_cluster="stable", probe=_probe(), n=0, stdout="x")
    prog = program_for(spec)
    assert prog.startswith("fn main() {") and 'println!("{}", e.to_string());' in prog
    test = render_replay_test(spec, fn_name="t")
    assert test.startswith("#[test]")
    assert 'assert_eq!(format!("{}", e.to_string()), "x");' in test
    # Both carry the setup verbatim — the whole point of one source.
    assert 'let e = anyhow!("x");' in prog
    assert 'let e = anyhow!("x");' in test


def test_the_observe_kind_reaches_the_then_so_the_class_is_not_OTHER() -> None:
    """Without observe -> then.kind every capture stays NORMALIZED_STDOUT, which
    is in no format or identity set, so `classified()` returns OTHER and the
    capture is discarded by t1.5's cap for ranking last."""
    disp = captured_spec(cfg_cluster="s", probe=_probe(observe="display"), n=0, stdout="v")
    dbg = captured_spec(cfg_cluster="s", probe=_probe(observe="debug"), n=1, stdout="v")
    alt = captured_spec(cfg_cluster="s", probe=_probe(observe="alt_debug"), n=2, stdout="v")
    assert disp.then is not None and disp.then.kind is OutcomeKind.DISPLAY_STRING
    assert dbg.then is not None and dbg.then.kind is OutcomeKind.DEBUG_STRING
    assert alt.then is not None and alt.then.kind is OutcomeKind.ALT_DEBUG_STRING
    for s in (disp, dbg, alt):
        assert s.klass is not BehaviourClass.OTHER
        assert behaviour_class_name(s) == "EXACT_FORMAT"


def test_an_identity_capture_stores_the_TYPE_and_the_observed_polarity() -> None:
    """`DOWNCAST_OK` carries a bool but not WHICH type — that is why
    `TYPE_IDENTITY` replaced it. The capture must keep the type."""
    p = _probe(when="e.is::<io::Error>()")
    yes = captured_spec(cfg_cluster="s", probe=p, n=0, stdout="true")
    no = captured_spec(cfg_cluster="s", probe=p, n=1, stdout="false")
    assert yes.then is not None and yes.then.kind is OutcomeKind.TYPE_IDENTITY
    assert yes.then.value == "io::Error" and yes.then.ok is True
    assert no.then is not None and no.then.ok is False
    assert behaviour_class_name(yes) == "IDENTITY"
    assert "assert!(e.is::<io::Error>());" in render_replay_test(yes, fn_name="t")
    assert "assert!(!(e.is::<io::Error>()));" in render_replay_test(no, fn_name="t")


def test_a_legacy_blob_capture_still_runs_but_can_never_replay() -> None:
    """NEGATIVE CONTROL — the shape on disk today. `program_for` returns the
    stored program so the exercises pass keeps working, and `render_replay_test`
    returns "" because there is no single expression to assert on."""
    blob = probe_spec(
        cfg_cluster="s",
        unit="Error::backtrace",
        n=0,
        program='fn main() { println!("a"); println!("b"); }',
        stdout="a\nb",
    )
    assert blob.klass is BehaviourClass.OTHER
    assert program_for(blob) == blob.given
    assert render_replay_test(blob, fn_name="t") == ""
    assert replayable([blob]) == []


# --- the parser -------------------------------------------------------------

_REPLY = """
<BEHAVIOUR name="chain_order" class="EXACT_FORMAT">
<GIVEN>
    let e = anyhow!("inner");
</GIVEN>
<WHEN>e.to_string()</WHEN>
<OBSERVE>display</OBSERVE>
</BEHAVIOUR>
<BEHAVIOUR name="no_when">
<GIVEN>let x = 1;</GIVEN>
</BEHAVIOUR>
"""


def test_the_parser_takes_the_three_parts_and_drops_a_block_with_no_when() -> None:
    got = parse_behaviour_specs(_REPLY)
    assert [p.name for p in got] == ["chain_order"]
    assert got[0].when == "e.to_string()" and got[0].observe == "display"
    assert 'let e = anyhow!("inner");' in got[0].given


def test_the_models_class_is_not_read_back() -> None:
    """Measured 0 of 3 self-assigned classes correct when the model chose them
    against 4 of 4 when supplied; `classified()` re-derives the class anyway."""
    assert not hasattr(parse_behaviour_specs(_REPLY)[0], "klass")
    spec = captured_spec(cfg_cluster="s", probe=parse_behaviour_specs(_REPLY)[0], n=0, stdout="inner")
    assert spec.klass is BehaviourClass.EXACT_FORMAT  # derived, not the tag


# --- the class-driven selector ----------------------------------------------


def test_the_selector_asks_for_the_scarcest_classes_first() -> None:
    specs = [
        captured_spec(cfg_cluster="s", probe=_probe(), n=i, stdout="v") for i in range(5)
    ]  # 5 EXACT_FORMAT, nothing else
    asks = class_targets(specs, behaviour_class_name, max_targets=3)
    names = [c for c, _ in asks]
    assert "EXACT_FORMAT" not in names  # the one class that is well covered
    assert names[0] in {"IDENTITY", "TRAIT_OBJECT_IDENTITY", "OPTION_SEMANTICS", "BOUNDED_RESOURCE"}
    assert all(d for _c, d in asks)  # every ask carries its phrasing
    assert class_targets(specs, behaviour_class_name, max_targets=0) == ()


def test_display_probes_do_not_cover_exact_format_for_the_probe_ask() -> None:
    """NEGATIVE CONTROL of ranking by harvest class: five Display probes make
    `behaviour_class_name` treat EXACT_FORMAT as covered. The probe ask must
    still request it — Display does not pin a render that moves with the env."""
    from pipeline_core.domain.behaviour_probe import probe_coverage_class

    displays = [captured_spec(cfg_cluster="s", probe=_probe(observe="display"), n=i, stdout="v") for i in range(5)]
    names = [c for c, _ in class_targets(displays, probe_coverage_class, max_targets=3)]
    assert "EXACT_FORMAT" in names


def test_debug_probes_cover_exact_format_for_the_probe_ask() -> None:
    from pipeline_core.domain.behaviour_probe import probe_coverage_class

    dbgs = [captured_spec(cfg_cluster="s", probe=_probe(observe="debug"), n=i, stdout="v") for i in range(5)]
    with using_driver(catalog_driver()):
        names = [c for c, _ in class_targets(dbgs, probe_coverage_class, max_targets=3)]
    assert "EXACT_FORMAT" not in names


def test_the_recorded_stable_suite_asks_exact_format_under_probe_coverage() -> None:
    """Plan fixture, computed on the recorded suite (not a hand list of five
    units). Today's `class_targets` + `behaviour_class_name` omits
    EXACT_FORMAT; `probe_coverage_class` puts it in the first four."""
    import json
    from pathlib import Path

    from pipeline_core.domain.behaviour import BehaviourSuite
    from pipeline_core.domain.behaviour_probe import probe_coverage_class

    path = Path(__file__).parent / "fixtures" / "behaviours" / "behaviours_stable_2026-08-27.json"
    suite = BehaviourSuite.from_dict(json.loads(path.read_text(encoding="utf-8")))
    today = [c for c, _ in class_targets(suite.specs, behaviour_class_name, max_targets=4)]
    assert today == [
        "TRAIT_OBJECT_IDENTITY",
        "OPTION_SEMANTICS",
        "BOUNDED_RESOURCE",
        "IDENTITY",
    ]
    asked = [c for c, _ in class_targets(suite.specs, probe_coverage_class, max_targets=4)]
    assert "EXACT_FORMAT" in asked


def test_class_prompts_off_restores_display_or_debug() -> None:
    """BEHAVIOUR_PIN_ENV_DEBUG=0 is the pre-2026-09-02 ask. On is Debug-only.

    Catalog-driver pin. Generic class prompts ignore the switch and do
    not name a render.
    """
    from pipeline_core.domain.behaviour_probe import catalog_class_prompts

    on = catalog_class_prompts(pin_env_debug=True)
    off = catalog_class_prompts(pin_env_debug=False)
    assert "never Display" in on["EXACT_FORMAT"]
    assert "Display, Debug, or the alternate" in off["EXACT_FORMAT"]
    assert "never Display" not in off["EXACT_FORMAT"]
    assert on["IDENTITY"] == off["IDENTITY"]


# --- the env guard ----------------------------------------------------------


def test_a_legacy_capture_env_key_is_ignored_on_load() -> None:
    """The field was write-only: nothing downstream read it, and the
    perturbation run is the guard. A record written while it existed must
    still load; the key must not come back out."""
    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="v")
    d = spec.to_dict()
    assert "capture_env" not in d
    d["capture_env"] = "RUST_BACKTRACE=0"
    from pipeline_core.domain.behaviour import BehaviourSpec

    loaded = BehaviourSpec.from_dict(d)
    assert loaded.to_dict() == spec.to_dict()
    assert "capture_env" not in loaded.to_dict()


def test_required_env_round_trips_and_is_omitted_when_empty() -> None:
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.behaviour import BehaviourSpec

    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="v")
    assert "required_env" not in spec.to_dict()
    pinned = dc_replace(spec, required_env=(("RUST_BACKTRACE", "1"),))
    d = pinned.to_dict()
    assert d["required_env"] == {"RUST_BACKTRACE": "1"}
    loaded = BehaviourSpec.from_dict(d)
    assert loaded.required_env == (("RUST_BACKTRACE", "1"),)
    assert BehaviourSpec.from_dict(spec.to_dict()).required_env == ()


def test_render_replay_writes_set_var_before_given() -> None:
    from dataclasses import replace as dc_replace

    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="v")
    pinned = dc_replace(spec, required_env=(("RUST_BACKTRACE", "1"), ("RUST_LIB_BACKTRACE", "1")))
    body = render_replay_test(pinned, fn_name="t")
    assert body.index("set_var") < body.index('let e = anyhow!("x")')
    assert 'set_var("RUST_BACKTRACE", "1")' in body
    # NEGATIVE CONTROL: no required_env, no set_var — today's renderer.
    plain = render_replay_test(spec, fn_name="t")
    assert "set_var" not in plain


# --- the emitter ------------------------------------------------------------


def test_the_emitted_file_carries_the_origin_marker_and_stable_fn_names() -> None:
    specs = [captured_spec(cfg_cluster="stable", probe=_probe(name="a"), n=0, stdout="x")]
    body = render_behaviour_test_file(specs, crate="anyhow")
    assert "determ-pipeline generated integration test" in body
    assert "use anyhow::*;" in body
    assert f"fn {replay_fn_name(specs[0])}()" in body
    # Stable across renders: a churning emitter makes verdict vectors
    # incomparable between sweeps.
    assert render_behaviour_test_file(specs, crate="anyhow") == body


# --- the value the replay has to match --------------------------------------


def test_a_capture_stores_exactly_what_format_would_produce() -> None:
    """`normalize_stdout` strips per-line trailing whitespace and drops trailing
    blank lines. That is right for a free-form program's blob and wrong for a
    single rendering: the replay asserts `format!(…) == <stored value>`, so a
    character the store drops and `format!` still produces makes the emitted
    test fail against the ORIGINAL crate. Measured 2026-08-27 on a pristine
    copy: a value of `"a \nb"` printed as `'a \nb\n'`, was stored as `'a\nb'`,
    and the emitted `#[test]` failed on the tree it was captured from."""
    raw = "a \nb\n"  # exactly what `println!("{}", s)` writes
    assert printed_value(raw) == "a \nb"
    assert normalize_stdout(raw) == "a\nb"  # NEGATIVE CONTROL: the old store
    assert printed_value(raw) != normalize_stdout(raw)

    spec = captured_spec(cfg_cluster="s", probe=_probe(when="s.clone()"), n=0, stdout=raw)
    assert spec.then is not None and spec.then.value == "a \nb"
    # The replay asserts the value the capture printed, character for character
    # — the trailing space on the first line included.
    assert r'assert_eq!(format!("{}", s.clone()), "a \nb");' in render_replay_test(spec, fn_name="t")


def test_a_value_that_ends_in_its_own_newline_survives_the_round_trip() -> None:
    """`println!` appends exactly one newline, so removing exactly one is the
    inverse. A Display that itself ends in a newline used to lose it."""
    assert printed_value("x\n\n") == "x\n"
    assert normalize_stdout("x\n\n") == "x"  # NEGATIVE CONTROL


# --- the capture program's imports ------------------------------------------


def test_the_capture_program_imports_the_crate_like_the_replay_does() -> None:
    """The three probes this shape replaced each opened with
    `use anyhow::{anyhow, Error};` OUTSIDE `fn main()` — the one line a
    setup-statements `given` has nowhere to put. Measured against a pristine
    copy, a `given` of `let err = anyhow!("boom");` with no import fails with
    `cannot find macro `anyhow` in this scope`; the replay half has had this
    line since it was written (`as_integration_test`)."""
    prog = render_probe_program('let e = anyhow!("x");', "e.to_string()", crate="anyhow")
    assert "use anyhow::*;" in prog.split("fn main()")[0]
    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="x")
    assert "use anyhow::*;" in program_for(spec, crate="anyhow")
    # NEGATIVE CONTROL: no crate named, no import — the shipped behaviour.
    assert "use " not in render_probe_program("let e = 1;", "e", crate="")


# --- the comparison check, with the turbofish carved out --------------------


def test_a_bare_comparison_is_refused_and_a_turbofish_is_not() -> None:
    """`<` and `>` were omitted from the token list to protect the turbofish,
    which admitted `v.len() < 3` and `n > 0` — the exact shape the rule exists
    to refuse. Deleting `::<…>` spans first makes the brackets unambiguous."""
    for expr in ("v.len() < 3", "n > 0", "a.len() > b.len()"):
        why = value_bearing_reason(expr)
        assert why is not None and "compares" in why, expr
    for expr in ("s.parse::<i32>().unwrap()", "e.is::<Vec<u8>>()", "e.downcast_ref::<HashMap<String, u8>>().is_some()"):
        assert value_bearing_reason(expr) is None, expr


def test_a_conjunction_of_two_observables_is_refused() -> None:
    """One printed boolean cannot say which half held, and the capture would
    store it as the identity of the FIRST type alone."""
    why = value_bearing_reason("a.is::<X>() && b.is::<Y>()")
    assert why is not None and "&&" in why
    assert value_bearing_reason("a.is::<X>() || b.is::<Y>()") is not None


# --- the environment axis ---------------------------------------------------


def test_the_perturbation_flips_the_observable_env_keys() -> None:
    """The flip is the guard. Storing the capture env on the spec said what
    the value was seen under and never asked whether it depends on it."""
    assert perturbed_env({}) == {"RUST_BACKTRACE": "1", "RUST_LIB_BACKTRACE": "1"}
    assert perturbed_env({"RUST_BACKTRACE": "1"})["RUST_BACKTRACE"] == "0"
    assert perturbed_env({"RUST_BACKTRACE": "full"})["RUST_BACKTRACE"] == "0"


def test_a_display_observe_is_rejected_for_an_exact_format_ask() -> None:
    from pipeline_core.domain.behaviour_probe import (
        CapturedProbe,
        exact_format_ask_rejects,
        fills_non_format_class,
    )

    display = CapturedProbe(name="x", given='let e = anyhow!("x");', when="e", observe="display")
    debug = CapturedProbe(name="x", given='let e = anyhow!("x");', when="e", observe="debug")
    ident = CapturedProbe(name="x", given="let e = Error::new(E);", when="e.is::<E>()", observe="display")
    with using_driver(catalog_driver()):
        assert exact_format_ask_rejects(display) is not None
        assert exact_format_ask_rejects(debug) is None
        assert fills_non_format_class(ident) and exact_format_ask_rejects(ident) is None


def test_a_twin_is_narrowed_to_what_the_capture_and_the_replay_share() -> None:
    """The twin's Then is cut back to the text the probe binary's render and
    the test binary's render share; the rest belongs to the rendering
    process. `None` when they share nothing, and when the capture already
    agrees (nothing to narrow).

    Run `330242-0be2c3e1` (2026-09-03): the twin carried the probe's WHOLE
    render and both clusters' only such capture came back `replay disagreed
    with the capture`. Two runs of the same probe cannot find this — they
    agree on exactly the part that differs between binaries — which is why
    the pair is capture-vs-replay.
    """
    from pipeline_core.domain.behaviour_probe import (
        agreed_prefix,
        narrow_then_to_agreement,
        observed_values,
        split_env_worlds,
    )

    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="head")
    env = {"SOME_KNOB": "1"}
    worlds = split_env_worlds(spec, "head + tail\nframe 0x41\n", env)
    assert worlds is not None
    _orig, twin = worlds
    assert twin.then is not None and twin.then.value == "head + tail\nframe 0x41"
    assert not twin.then.prefix          # exact until a replay disagrees

    cut = narrow_then_to_agreement(twin, "head + tail\nframe 0x77\n")
    assert cut is not None and cut.then is not None
    assert cut.then.value == "head + tail\nframe 0x" and cut.then.prefix

    # Nothing shared, and nothing to cut, are both None — the caller withdraws
    # rather than pinning the empty string or re-rendering forever.
    assert narrow_then_to_agreement(twin, "unrelated") is None
    assert narrow_then_to_agreement(twin, "head + tail\nframe 0x41") is None
    assert agreed_prefix(["abc", "abd"]) == "ab"
    assert agreed_prefix(["abc"]) == "" and agreed_prefix([]) == ""

    # The observed value comes out of the failing run's own marker.
    assert observed_values(
        "thread 'behaviour_x' panicked:\n"
        "determ-observed[behaviour_x]<<<head + tail\nframe 0x77\n>>>\n"
    ) == {"behaviour_x": "head + tail\nframe 0x77\n"}
    assert observed_values("test behaviour_x ... FAILED") == {}


def test_a_prefix_then_replays_as_starts_with_and_a_longer_render() -> None:
    from dataclasses import replace as dc_replace

    from pipeline_core.domain.behaviour import ObservableOutcome, OutcomeKind

    spec = captured_spec(cfg_cluster="s", probe=_probe(), n=0, stdout="v")
    assert spec.then is not None
    pinned = dc_replace(spec, then=dc_replace(spec.then, value="head", prefix=True))
    body = render_replay_test(pinned, fn_name="t")
    assert "starts_with" in body and "assert_eq!" not in body
    assert "actual.len() > " in body
    # NEGATIVE CONTROL: the same spec without the flag is an equality assert.
    assert "assert_eq!" in render_replay_test(spec, fn_name="t")

    # A prefix is a claim about rendered text, and pins nothing when empty.
    for bad in (OutcomeKind.TYPE_IDENTITY, OutcomeKind.DOWNCAST_OK):
        with pytest.raises(ValueError):
            ObservableOutcome(bad, value="T", prefix=True)
    with pytest.raises(ValueError):
        ObservableOutcome(OutcomeKind.DEBUG_STRING, value="", prefix=True)

    # Round trip, and a legacy record without the key loads as exact.
    d = pinned.then.to_dict()
    assert d["prefix"] is True
    assert ObservableOutcome.from_dict(d).prefix
    assert "prefix" not in spec.then.to_dict()
    assert not ObservableOutcome.from_dict(spec.then.to_dict()).prefix
