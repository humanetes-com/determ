//! A tiny, dependency-free crate used as the L3 reconstruction fixture.
//!
//! Shape chosen to exercise the pipeline meaningfully while staying fast:
//!   - a free function returning a plain value (`add`)
//!   - a fallible parser returning `Option` (`parse_pair`)
//!   - a small stateful type with methods (`Counter`) — impl-block fill path
//!   - `Result` with a custom error enum — trait-impl + match arms
//! Bodies are real here; the pipeline strips them to `unimplemented!()` and
//! reconstructs from the signatures + the tests below.

/// Sum two integers.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Parse `"<i32>,<i32>"` into a pair; `None` on any malformed input.
pub fn parse_pair(s: &str) -> Option<(i32, i32)> {
    let (l, r) = s.split_once(',')?;
    let l = l.trim().parse().ok()?;
    let r = r.trim().parse().ok()?;
    Some((l, r))
}

/// A monotonically increasing counter.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counter {
    value: u32,
}

impl Counter {
    /// A counter starting at `start`.
    pub fn new(start: u32) -> Self {
        Counter { value: start }
    }

    /// Current value.
    pub fn get(&self) -> u32 {
        self.value
    }

    /// Increment and return the new value.
    pub fn bump(&mut self) -> u32 {
        self.value += 1;
        self.value
    }
}

/// Error from [`checked_div`].
#[derive(Debug, PartialEq, Eq)]
pub enum MathError {
    DivByZero,
}

/// Integer division that reports division by zero instead of panicking.
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
    fn counter_bumps() {
        let mut c = Counter::new(0);
        assert_eq!(c.get(), 0);
        assert_eq!(c.bump(), 1);
        assert_eq!(c.bump(), 2);
        assert_eq!(c.get(), 2);
    }

    #[test]
    fn div_by_zero_is_error() {
        assert_eq!(checked_div(6, 2), Ok(3));
        assert_eq!(checked_div(1, 0), Err(MathError::DivByZero));
    }
}
