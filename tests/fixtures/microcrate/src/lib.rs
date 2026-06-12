//! A tiny, dependency-free crate used as the L3 reconstruction fixture.
//!
//! Shape mirrors the real targets (anyhow) while staying minutes-fast:
//!   - multi-file: `counter`, `parse`, and a capability-gated `cap` module
//!   - TWO compiler clusters: default, and `--cfg extra_cap` (dual-form
//!     predicates like anyhow's `error_generic_member_access`) — exercises
//!     cluster scan, per-cluster task0c, and cross-cluster gen-test gating
//!   - a fn whose value DIFFERS per cluster (`capacity_hint`) — the trap that
//!     catches ungated cross-cluster test leakage
//!   - trait impls (`Display`), a stateful type, a fallible parser
//! Bodies are real here; the pipeline strips them to `unimplemented!()` and
//! reconstructs from the signatures + the tests.

mod counter;
mod parse;
#[cfg(any(extra_cap, feature = "extra_cap"))]
mod cap;

pub use counter::Counter;
pub use parse::parse_pair;
#[cfg(any(extra_cap, feature = "extra_cap"))]
pub use cap::{clamp_add, clamp_add_report, CapReport};

/// Sum two integers.
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

/// Integer division that reports division by zero instead of panicking.
pub fn checked_div(a: i32, b: i32) -> Result<i32, MathError> {
    if b == 0 {
        return Err(MathError::DivByZero);
    }
    Ok(a / b)
}

/// Buffer length hint — larger when the `extra_cap` capability is compiled in.
#[cfg(any(extra_cap, feature = "extra_cap"))]
pub fn capacity_hint() -> usize {
    64
}

/// Buffer length hint — default build.
#[cfg(not(any(extra_cap, feature = "extra_cap")))]
pub fn capacity_hint() -> usize {
    16
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
    fn default_capacity() {
        assert_eq!(capacity_hint(), 16);
    }

    #[cfg(any(extra_cap, feature = "extra_cap"))]
    #[test]
    fn extra_capacity() {
        assert_eq!(capacity_hint(), 64);
    }
}
