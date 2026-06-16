//! TRAP T3 (MED) — `sum_csv`.
//!
//! Obvious-wrong attractor: split on ',' and parse each token, so the empty
//! string `""` becomes one empty token that fails to parse → `BadToken`. But the
//! tests pin empty/whitespace-only input to a DISTINCT `Empty` variant. The naive
//! impl conflates the two; only special-casing empty-after-trim recovers it.

/// Why [`sum_csv`] could not produce a sum.
#[derive(Debug, PartialEq, Eq)]
pub enum SumError {
    /// The input was empty or all whitespace.
    Empty,
    /// A comma-separated token was not a valid integer.
    BadToken,
}

/// Sum a comma-separated list of integers.
pub fn sum_csv(s: &str) -> Result<i32, SumError> {
    if s.trim().is_empty() {
        return Err(SumError::Empty);
    }
    let mut total = 0;
    for tok in s.split(',') {
        let n: i32 = tok.trim().parse().map_err(|_| SumError::BadToken)?;
        total += n;
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_valid_lists() {
        assert_eq!(sum_csv("1,2,3"), Ok(6));
        assert_eq!(sum_csv("10"), Ok(10));
        assert_eq!(sum_csv(" -4 , 9 "), Ok(5));
    }

    #[test]
    fn empty_is_distinct_from_bad_token() {
        assert_eq!(sum_csv(""), Err(SumError::Empty)); // NOT BadToken
        assert_eq!(sum_csv("   "), Err(SumError::Empty));
        assert_eq!(sum_csv("1,x,3"), Err(SumError::BadToken));
    }
}
