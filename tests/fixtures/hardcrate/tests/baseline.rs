//! Baseline integration — always present (complexity 0). These exercise the easy
//! fns that fill first try, so the pass count has a floor to climb from.
use hardcrate::{add, checked_div, Counter, MathError};

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
    assert_eq!(c.bump(), 2);
}
