//! TRAP T4 (MED) — `wrapping_total`, gated to the `--cfg extra_cap` cluster so the
//! SECOND cluster also has a recovery (cluster-aware refill).
//!
//! Obvious-wrong attractor: `xs.iter().map(|&x| x as u32).sum()` or `u8` `sum()` —
//! either changes the type or (in debug) overflow-PANICS. The tests pin wrapping
//! `u8` addition: 200 + 100 wraps to 44 (mod 256). Only `wrapping_add` recovers it.

/// Sum the bytes with wrapping (modular) `u8` arithmetic.
pub fn wrapping_total(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_on_overflow() {
        assert_eq!(wrapping_total(&[1, 2, 3]), 6);
        assert_eq!(wrapping_total(&[200, 100]), 44); // 300 mod 256
        assert_eq!(wrapping_total(&[255, 1]), 0);
        assert_eq!(wrapping_total(&[]), 0);
    }
}
