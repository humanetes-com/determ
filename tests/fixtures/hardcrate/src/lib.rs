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
mod wire;
mod framing;
mod sprawl;
mod label;
mod unsafe_ptr;
mod guarded;
mod link_a;
mod link_b;
#[cfg(any(extra_cap, feature = "extra_cap"))]
mod cap;

pub use counter::Counter;
pub use merge::merge_first_wins;
pub use round::divide_round;
pub use textsum::{sum_csv, SumError};
pub use wire::{decode_id, encode_id};
pub use framing::{deframe, frame};
pub use sprawl::{abs_diff, clamp_u8, classify, count_set_bits, digit_sum, is_ascii_upper, max3, min3, nth_byte, parity, reverse_nibbles, saturating_sub_u8, sign, to_lower, Category};
pub use label::{Describe, Reason, Tagged};
pub use unsafe_ptr::{copy_via_raw, ptr_distance, read_at, write_at};
pub use guarded::{seal, verify, Guarded};
pub use link_a::stamp;
pub use link_b::unstamp;
#[cfg(any(extra_cap, feature = "extra_cap"))]
pub use cap::wrapping_total;

/// Capability buffer limit — larger when the `extra_cap` capability is compiled in.
/// The dual `#[cfg]`/`#[cfg(not)]` form is what makes `extra_cap` a 2-state TOGGLE,
/// so the cluster scan identifies it as a second compiler cluster (the single-form
/// gating in the first cut left it undetected → 1 cluster → the test-count bug).
#[cfg(any(extra_cap, feature = "extra_cap"))]
pub fn cap_limit() -> usize {
    255
}

/// Capability buffer limit — default build.
#[cfg(not(any(extra_cap, feature = "extra_cap")))]
pub fn cap_limit() -> usize {
    64
}

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

    #[cfg(not(any(extra_cap, feature = "extra_cap")))]
    #[test]
    fn default_cap_limit() {
        assert_eq!(cap_limit(), 64);
    }

    #[cfg(any(extra_cap, feature = "extra_cap"))]
    #[test]
    fn extra_cap_limit() {
        assert_eq!(cap_limit(), 255);
    }
}
