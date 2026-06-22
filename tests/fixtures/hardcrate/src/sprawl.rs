//! TRAP T7 (level 3) — the AUGMENTATION FAN-OUT reproducer.
//!
//! Unlike the other traps (which probe the recovery LOOP), this one probes the
//! TASK-0c → TASK-1 pipeline at scale. It is a WIDE surface: many small, branchy,
//! independent pub fns, each pinned on ONE path only (the integration test exercises
//! a single input per fn; the unit tests are deliberately thin). So after strip +
//! reconstruct, a large fraction of lines across MANY functions is left uncovered →
//! TASK 0c augmentation must fill them. With split-and-merge that uncovered set
//! partitions across many functions, so the module accumulates MANY
//! `sprawl.gen_tests_<N>.rs` siblings — the exact condition that made a live anyhow
//! run fan TASK 1 out to ~140 one-call-per-file extractions and look "blocked".
//!
//! Regression value:
//!   - Part A (TASK 1 sibling coalescing): TASK 1's "across N file(s)" must report
//!     the sibling group as ONE logical unit, not one per `sprawl.gen_tests_*`.
//!   - Part B (one sibling per split-and-merge attempt): the `sprawl.gen_tests_*.rs`
//!     count must stay ~= number of augmentation attempts, NOT attempts × split_n.
//!
//! Doc comments stay NEUTRAL (name WHAT, never the exact rule) — the tests are the
//! only oracle, per the fixture convention.

/// Bucket an integer into one of four ranges.
#[derive(Debug, PartialEq, Eq)]
pub enum Category {
    Neg,
    Zero,
    Small,
    Large,
}

/// Restrict a signed value to the unsigned-byte range.
pub fn clamp_u8(x: i32) -> u8 {
    if x < 0 {
        0
    } else if x > 255 {
        255
    } else {
        x as u8
    }
}

/// The sign of a value as -1, 0 or 1.
pub fn sign(x: i32) -> i8 {
    if x < 0 {
        -1
    } else if x > 0 {
        1
    } else {
        0
    }
}

/// The absolute difference of two unsigned values.
pub fn abs_diff(a: u32, b: u32) -> u32 {
    if a >= b {
        a - b
    } else {
        b - a
    }
}

/// The byte at `n`, if present.
pub fn nth_byte(buf: &[u8], n: usize) -> Option<u8> {
    if n < buf.len() {
        Some(buf[n])
    } else {
        None
    }
}

/// Whether a byte is an ASCII uppercase letter.
pub fn is_ascii_upper(c: u8) -> bool {
    c >= b'A' && c <= b'Z'
}

/// Lowercase an ASCII letter, leaving everything else unchanged.
pub fn to_lower(c: u8) -> u8 {
    if is_ascii_upper(c) {
        c + 32
    } else {
        c
    }
}

/// The number of set bits in a byte.
pub fn count_set_bits(x: u8) -> u32 {
    let mut n = x;
    let mut count = 0;
    while n != 0 {
        count += (n & 1) as u32;
        n >>= 1;
    }
    count
}

/// Whether a byte has an odd number of set bits.
pub fn parity(x: u8) -> bool {
    count_set_bits(x) % 2 == 1
}

/// Subtract, saturating at zero.
pub fn saturating_sub_u8(a: u8, b: u8) -> u8 {
    if a > b {
        a - b
    } else {
        0
    }
}

/// The least of three values.
pub fn min3(a: i32, b: i32, c: i32) -> i32 {
    let m = if a < b { a } else { b };
    if m < c {
        m
    } else {
        c
    }
}

/// The greatest of three values.
pub fn max3(a: i32, b: i32, c: i32) -> i32 {
    let m = if a > b { a } else { b };
    if m > c {
        m
    } else {
        c
    }
}

/// The sum of the decimal digits of a value.
pub fn digit_sum(mut n: u32) -> u32 {
    let mut total = 0;
    while n > 0 {
        total += n % 10;
        n /= 10;
    }
    total
}

/// Swap the high and low nibble of a byte.
pub fn reverse_nibbles(x: u8) -> u8 {
    (x << 4) | (x >> 4)
}

/// Bucket a value: negative, zero, 1..=9, or 10+.
pub fn classify(n: i32) -> Category {
    if n < 0 {
        Category::Neg
    } else if n == 0 {
        Category::Zero
    } else if n < 10 {
        Category::Small
    } else {
        Category::Large
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Deliberately THIN: one happy-path assert each for two fns only. Everything
    // else is left to the integration pin (one input per fn) → most branches
    // uncovered → TASK 0c must augment broadly. Do NOT "complete" these.
    #[test]
    fn clamp_in_range() {
        assert_eq!(clamp_u8(100), 100);
    }

    #[test]
    fn digit_sum_basic() {
        assert_eq!(digit_sum(123), 6);
    }
}
