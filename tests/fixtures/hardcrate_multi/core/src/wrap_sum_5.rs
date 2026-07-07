/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 96.
pub fn wrap_sum_96(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 97.
pub fn wrap_sum_97(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 98.
pub fn wrap_sum_98(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 99.
pub fn wrap_sum_99(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 100.
pub fn wrap_sum_100(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 101.
pub fn wrap_sum_101(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_sum_96_wraps_on_overflow() {
        assert_eq!(wrap_sum_96(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_96(&[200, 146]), 90);
        assert_eq!(wrap_sum_96(&[255, 1, 6]), 6);
        assert_eq!(wrap_sum_96(&[5, 6, 7, 8, 9]), 35);
    }

    #[test]
    fn wrap_sum_97_wraps_on_overflow() {
        assert_eq!(wrap_sum_97(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_97(&[200, 147]), 91);
        assert_eq!(wrap_sum_97(&[255, 1, 7]), 7);
        assert_eq!(wrap_sum_97(&[6, 7, 8, 9, 10]), 40);
    }

    #[test]
    fn wrap_sum_98_wraps_on_overflow() {
        assert_eq!(wrap_sum_98(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_98(&[200, 148]), 92);
        assert_eq!(wrap_sum_98(&[255, 1, 8]), 8);
        assert_eq!(wrap_sum_98(&[0, 1, 2, 3, 4]), 10);
    }

    #[test]
    fn wrap_sum_99_wraps_on_overflow() {
        assert_eq!(wrap_sum_99(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_99(&[200, 149]), 93);
        assert_eq!(wrap_sum_99(&[255, 1, 9]), 9);
        assert_eq!(wrap_sum_99(&[1, 2, 3, 4, 5]), 15);
    }

    #[test]
    fn wrap_sum_100_wraps_on_overflow() {
        assert_eq!(wrap_sum_100(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_100(&[200, 100]), 44);
        assert_eq!(wrap_sum_100(&[255, 1, 0]), 0);
        assert_eq!(wrap_sum_100(&[2, 3, 4, 5, 6]), 20);
    }

    #[test]
    fn wrap_sum_101_wraps_on_overflow() {
        assert_eq!(wrap_sum_101(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_101(&[200, 101]), 45);
        assert_eq!(wrap_sum_101(&[255, 1, 1]), 1);
        assert_eq!(wrap_sum_101(&[3, 4, 5, 6, 7]), 25);
    }
}
