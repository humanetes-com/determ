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
