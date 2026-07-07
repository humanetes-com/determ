/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 48.
pub fn wrap_sum_48(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 49.
pub fn wrap_sum_49(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 50.
pub fn wrap_sum_50(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 51.
pub fn wrap_sum_51(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 52.
pub fn wrap_sum_52(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 53.
pub fn wrap_sum_53(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_sum_48_wraps_on_overflow() {
        assert_eq!(wrap_sum_48(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_48(&[200, 148]), 92);
        assert_eq!(wrap_sum_48(&[255, 1, 8]), 8);
        assert_eq!(wrap_sum_48(&[6, 7, 8, 9, 10]), 40);
    }

    #[test]
    fn wrap_sum_49_wraps_on_overflow() {
        assert_eq!(wrap_sum_49(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_49(&[200, 149]), 93);
        assert_eq!(wrap_sum_49(&[255, 1, 9]), 9);
        assert_eq!(wrap_sum_49(&[0, 1, 2, 3, 4]), 10);
    }

    #[test]
    fn wrap_sum_50_wraps_on_overflow() {
        assert_eq!(wrap_sum_50(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_50(&[200, 100]), 44);
        assert_eq!(wrap_sum_50(&[255, 1, 0]), 0);
        assert_eq!(wrap_sum_50(&[1, 2, 3, 4, 5]), 15);
    }

    #[test]
    fn wrap_sum_51_wraps_on_overflow() {
        assert_eq!(wrap_sum_51(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_51(&[200, 101]), 45);
        assert_eq!(wrap_sum_51(&[255, 1, 1]), 1);
        assert_eq!(wrap_sum_51(&[2, 3, 4, 5, 6]), 20);
    }

    #[test]
    fn wrap_sum_52_wraps_on_overflow() {
        assert_eq!(wrap_sum_52(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_52(&[200, 102]), 46);
        assert_eq!(wrap_sum_52(&[255, 1, 2]), 2);
        assert_eq!(wrap_sum_52(&[3, 4, 5, 6, 7]), 25);
    }

    #[test]
    fn wrap_sum_53_wraps_on_overflow() {
        assert_eq!(wrap_sum_53(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_53(&[200, 103]), 47);
        assert_eq!(wrap_sum_53(&[255, 1, 3]), 3);
        assert_eq!(wrap_sum_53(&[4, 5, 6, 7, 8]), 30);
    }
}
