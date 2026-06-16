//! Integration tests — exercise the public API from outside the crate (the frozen
//! reconstruction oracle; never rewritten). These re-pin each trap's rule across
//! the public boundary, so a per-fn fill that satisfies a single unit case but
//! breaks the contract is still caught here.
use hardcrate::{add, checked_div, divide_round, merge_first_wins, sum_csv, Counter, MathError, SumError};

#[test]
fn add_and_div_compose() {
    let s = add(10, 2);
    assert_eq!(checked_div(s, 3), Ok(4));
    assert_eq!(checked_div(s, 0), Err(MathError::DivByZero));
}

#[test]
fn divide_round_is_half_to_even() {
    assert_eq!(divide_round(1, 2), 0);
    assert_eq!(divide_round(5, 2), 2);
    assert_eq!(divide_round(7, 2), 4);
    assert_eq!(divide_round(8, 3), 3);
}

#[test]
fn merge_keeps_first_on_conflict() {
    let out = merge_first_wins(&[(2, 20), (1, 10)], &[(2, 99), (3, 30)]);
    assert_eq!(out, vec![(1, 10), (2, 20), (3, 30)]);
}

#[test]
fn sum_csv_distinguishes_empty() {
    assert_eq!(sum_csv("4,5,6"), Ok(15));
    assert_eq!(sum_csv(""), Err(SumError::Empty));
    assert_eq!(sum_csv("1,bad"), Err(SumError::BadToken));
}

#[test]
fn counter_from_default() {
    let mut c = Counter::default();
    assert_eq!(c.bump(), 1);
    assert_eq!(c.bump(), 2);
}

#[cfg(any(extra_cap, feature = "extra_cap"))]
#[test]
fn wrapping_total_wraps() {
    use hardcrate::wrapping_total;
    assert_eq!(wrapping_total(&[200, 100]), 44);
    assert_eq!(wrapping_total(&[255, 1]), 0);
}
