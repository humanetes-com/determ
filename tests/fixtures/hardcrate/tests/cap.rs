//! Integration pin for TRAP T4 (cap.rs) — the extra_cap cluster. complexity 1.
//! Whole file gated: under the default cluster this binary has no active tests.
#![cfg(any(extra_cap, feature = "extra_cap"))]
use hardcrate::wrapping_total;

#[test]
fn wrapping_total_wraps() {
    assert_eq!(wrapping_total(&[200, 100]), 44);
    assert_eq!(wrapping_total(&[255, 1]), 0);
}
