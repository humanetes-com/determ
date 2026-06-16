//! A dependency-free crate that is HARD ON PURPOSE — the L3-RECOVERY fixture.
//!
//! Several fns have an OBVIOUS implementation that compiles and runs but returns
//! the WRONG value; only the tests catch it. The pipeline strips every body to
//! `unimplemented!()` and reconstructs from signatures + tests; the naive fill
//! fails t3 → task4 (genesis) → regen, across ≥3 iterations. That failure-recovery
//! loop is what the microcrate (happy path) never exercises.
//!
//! Doc comments are deliberately NEUTRAL — they name WHAT a fn computes, never the
//! exact rule (no "round half to even", no "first wins"). The TESTS are the only
//! oracle, and each trap's cases UNIQUELY pin the rule (a careful human reading the
//! tests gets it right), so non-recovery is a pipeline gap, never an ambiguity.

mod merge;
mod round;
mod textsum;
mod counter;
#[cfg(any(extra_cap, feature = "extra_cap"))]
mod cap;

pub use counter::Counter;
pub use merge::merge_first_wins;
pub use round::divide_round;
pub use textsum::{sum_csv, SumError};
#[cfg(any(extra_cap, feature = "extra_cap"))]
pub use cap::wrapping_total;

/// Sum two integers. (Baseline — fills first try; lets the pass count climb as the
/// traps recover, giving the ratchet/plateau audits a rising series.)
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Error from [`checked_div`].
#[derive(Debug, PartialEq, Eq)]
pub enum MathError {
    DivByZero,
}

impl core::fmt::Display for MathError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            MathError::DivByZero => f.write_str("division by zero"),
        }
    }
}

/// Integer division that reports division by zero instead of panicking. (Baseline.)
pub fn checked_div(a: i32, b: i32) -> Result<i32, MathError> {
    if b == 0 {
        return Err(MathError::DivByZero);
    }
    Ok(a / b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_works() {
        assert_eq!(add(2, 3), 5);
        assert_eq!(add(-1, 1), 0);
    }

    #[test]
    fn div_by_zero_is_error() {
        assert_eq!(checked_div(6, 2), Ok(3));
        assert_eq!(checked_div(1, 0), Err(MathError::DivByZero));
    }

    #[test]
    fn display_formats_error() {
        assert_eq!(MathError::DivByZero.to_string(), "division by zero");
    }
}
