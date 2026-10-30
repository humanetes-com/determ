"""What a when observes is decided by the call at the root of its value.

Run `15370-6a4cfd45` (2026-10-30): `identity_type_of` found a downcast
anywhere in the when, so `format!("{:?}", err.downcast_mut::<String>())` was
stored as a type identity, its replay asserted a String as a bool, the emitted
behaviour file did not compile and every capture in it was withdrawn (24 in
stable, 26 in nightly; run `2129106-ead996ab` lost 52 the same way). The
option class and the identity call name read tokens anywhere in the same way.

Each test carries its negative control: the pre-fix token scan, inlined here,
returns the wrong reading on the same when.
"""
from __future__ import annotations

from pipeline_core.domain.behaviour import BehaviourClass, OutcomeKind, classify_behaviour
from pipeline_core.domain.behaviour_probe import (
    _IDENTITY_CALLS,
    CapturedProbe,
    captured_spec,
    fills_non_format_class,
    identity_type_of,
    observe_kind,
    observes_presence,
    render_replay_test,
    root_call,
    value_bearing_reason,
)
from pipeline_core.domain.behaviour_select import identity_call_name

FORMAT_OF_DOWNCAST = 'format!("{:?}", err.downcast_mut::<String>())'


def _pre_fix_identity(when: str) -> str | None:
    """The token scan this change replaced: the first identity call anywhere."""
    for call in _IDENTITY_CALLS:
        at = when.find(call)
        if at < 0:
            continue
        start, depth, i = at + len(call), 1, at + len(call)
        while i < len(when):
            if when[i] == "<":
                depth += 1
            elif when[i] == ">":
                depth -= 1
                if depth == 0:
                    return when[start:i].strip() or None
            i += 1
    return None


def _spec(when: str, stdout: str):  # type: ignore[no-untyped-def]
    return captured_spec(
        cfg_cluster="stable",
        probe=CapturedProbe(name="p", given="let err = make();", when=when, observe="display"),
        n=0, stdout=stdout, default_cluster="stable")


def test_a_downcast_inside_a_format_is_not_an_identity() -> None:
    assert identity_type_of(FORMAT_OF_DOWNCAST) is None
    assert observe_kind("display", FORMAT_OF_DOWNCAST) is OutcomeKind.DISPLAY_STRING
    # Negative control: the token scan called it an identity of String.
    assert _pre_fix_identity(FORMAT_OF_DOWNCAST) == "String"


def test_the_replay_of_that_capture_asserts_a_string_not_a_bool() -> None:
    """The emitted assertion is the one that compiled-or-not on the record."""
    body = render_replay_test(_spec(FORMAT_OF_DOWNCAST, "None\n"), fn_name="t")
    assert "assert!(!(" not in body
    assert "format!" in body and '"None"' in body


def test_the_identity_forms_stay_identities() -> None:
    cases = {
        'Error::msg("x").is::<&str>()': "&str",
        "err.downcast::<i32>().is_ok()": "i32",
        "e.downcast_ref::<Vec<u8>>().is_some()": "Vec<u8>",
        "(&e as &dyn StdError).is::<io::Error>()": "io::Error",
        "e.chain().next().unwrap().is::<std::io::Error>()": "std::io::Error",
        '(|| -> Result<()> { bail!("x") })().unwrap_err().is::<io::Error>()': "io::Error",
        "e.downcast_ref::<Box<dyn Fn() -> i32>>().is_some()": "Box<dyn Fn() -> i32>",
    }
    for when, ty in cases.items():
        assert identity_type_of(when) == ty, when


def test_an_operator_at_the_root_is_not_a_call_root() -> None:
    """A comparison that ends in a call, a negation and a reference are their
    operator's value. The negated identity was an identity with the printed
    bool inverted under the token scan."""
    for when in ("e.downcast_ref::<X>().map(|x| x.code) == Some(3)",
                 "!e.is::<io::Error>()", "x == y.is_some()"):
        assert root_call(when) is None, when
        assert identity_type_of(when) is None, when
        assert not observes_presence(when), when
    assert _pre_fix_identity("!e.is::<io::Error>()") == "io::Error"
    # The comparison is now refused as a comparison, not admitted as an identity.
    assert value_bearing_reason("e.downcast_ref::<X>().map(|x| x.code) == Some(3)")
    assert _pre_fix_identity("e.downcast_ref::<X>().map(|x| x.code) == Some(3)") == "X"


def test_a_combinator_over_an_identity_observes_its_own_value() -> None:
    when = "e.chain().any(|c| c.is::<io::Error>())"
    assert identity_type_of(when) is None
    assert _pre_fix_identity(when) == "io::Error"


def test_presence_is_read_at_the_root() -> None:
    assert observes_presence("e.source().is_none()")
    assert observes_presence("v.first().is_some()")
    assert not observes_presence('format!("{}", v.first().is_some())')
    probe = CapturedProbe(name="p", given="", when='format!("{}", v.first().is_some())',
                          observe="display")
    assert not fills_non_format_class(probe)
    spec = _spec("e.source().is_none()", "true\n")
    assert classify_behaviour(spec, default_cluster="stable") is BehaviourClass.OPTION_SEMANTICS
    nested = _spec('format!("{}", v.first().is_some())', "true\n")
    assert classify_behaviour(nested, default_cluster="stable") is not BehaviourClass.OPTION_SEMANTICS


def test_the_identity_call_name_is_the_root_call() -> None:
    assert identity_call_name("e.downcast_ref::<T>().is_some()") == "downcast_ref"
    assert identity_call_name("Counter::new(1).peek::<u8>()") == "peek"
    assert identity_call_name("Wrap::<u8>::new().is::<T>()") == "is"
    assert identity_call_name("c.peek()") == "peek"
    assert identity_call_name("value") == ""
