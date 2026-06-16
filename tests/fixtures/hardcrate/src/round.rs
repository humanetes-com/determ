//! TRAP T1 (HARD) — `divide_round`.
//!
//! Obvious-wrong attractors: integer `num / den` (truncates), or round-half-UP.
//! The tests pin round-half-to-EVEN (banker's rounding): on an exact .5 the result
//! goes to the nearest EVEN integer, so `1/2 → 0` and `5/2 → 2` (not 1 and 3).
//! The cases below make that the UNIQUE consistent rule — truncation fails the .5
//! round-ups, half-up fails `1/2` and `5/2`, half-down fails `3/2` and `7/2`.

/// Divide `num` by `den` and round the quotient to the nearest integer.
///
/// `den` is assumed non-zero and both inputs non-negative.
pub fn divide_round(num: i64, den: i64) -> i64 {
    let q = num / den;
    let r2 = (num % den) * 2;
    if r2 < den {
        q
    } else if r2 > den {
        q + 1
    } else {
        // exact half → round to even
        if q % 2 == 0 {
            q
        } else {
            q + 1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounds_half_to_even() {
        assert_eq!(divide_round(1, 2), 0); // 0.5 → 0 (even); half-up would give 1
        assert_eq!(divide_round(3, 2), 2); // 1.5 → 2 (even); truncate would give 1
        assert_eq!(divide_round(5, 2), 2); // 2.5 → 2 (even); half-up would give 3
        assert_eq!(divide_round(7, 2), 4); // 3.5 → 4 (even); truncate would give 3
        assert_eq!(divide_round(9, 2), 4); // 4.5 → 4 (even); half-up would give 5
    }

    #[test]
    fn rounds_non_halves_normally() {
        assert_eq!(divide_round(4, 2), 2); // exact
        assert_eq!(divide_round(8, 3), 3); // 2.67 → 3
        assert_eq!(divide_round(7, 3), 2); // 2.33 → 2
        assert_eq!(divide_round(0, 5), 0);
    }
}
