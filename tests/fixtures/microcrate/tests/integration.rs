//! Integration tests — exercise the public API from outside the crate, mirroring
//! the pipeline's `tests/` reconstruction target (frozen; never rewritten).
use microcrate::{add, checked_div, parse_pair, Counter, MathError};

#[test]
fn parse_pair_roundtrips() {
    assert_eq!(parse_pair("3,4"), Some((3, 4)));
    assert_eq!(parse_pair(" -2 , 7 "), Some((-2, 7)));
    assert_eq!(parse_pair("nope"), None);
    assert_eq!(parse_pair("1,"), None);
}

#[test]
fn add_and_div_compose() {
    let s = add(10, 2);
    assert_eq!(checked_div(s, 3), Ok(4));
    assert_eq!(checked_div(s, 0), Err(MathError::DivByZero));
}

#[test]
fn counter_from_default() {
    let mut c = Counter::default();
    assert_eq!(c.bump(), 1);
}

#[cfg(any(extra_cap, feature = "extra_cap"))]
#[test]
fn capability_composes() {
    use microcrate::{capacity_hint, clamp_add, clamp_add_report};
    assert_eq!(clamp_add(capacity_hint(), 1), capacity_hint());
    assert!(clamp_add_report(capacity_hint(), 1).clamped);
}
