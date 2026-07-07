/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 24.
pub fn wrap_sum_24(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 25.
pub fn wrap_sum_25(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 26.
pub fn wrap_sum_26(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 27.
pub fn wrap_sum_27(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 28.
pub fn wrap_sum_28(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 29.
pub fn wrap_sum_29(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_sum_24_wraps_on_overflow() {
        assert_eq!(wrap_sum_24(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_24(&[200, 124]), 68);
        assert_eq!(wrap_sum_24(&[255, 1, 4]), 4);
        assert_eq!(wrap_sum_24(&[3, 4, 5, 6, 7]), 25);
    }

    #[test]
    fn wrap_sum_25_wraps_on_overflow() {
        assert_eq!(wrap_sum_25(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_25(&[200, 125]), 69);
        assert_eq!(wrap_sum_25(&[255, 1, 5]), 5);
        assert_eq!(wrap_sum_25(&[4, 5, 6, 7, 8]), 30);
    }

    #[test]
    fn wrap_sum_26_wraps_on_overflow() {
        assert_eq!(wrap_sum_26(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_26(&[200, 126]), 70);
        assert_eq!(wrap_sum_26(&[255, 1, 6]), 6);
        assert_eq!(wrap_sum_26(&[5, 6, 7, 8, 9]), 35);
    }

    #[test]
    fn wrap_sum_27_wraps_on_overflow() {
        assert_eq!(wrap_sum_27(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_27(&[200, 127]), 71);
        assert_eq!(wrap_sum_27(&[255, 1, 7]), 7);
        assert_eq!(wrap_sum_27(&[6, 7, 8, 9, 10]), 40);
    }

    #[test]
    fn wrap_sum_28_wraps_on_overflow() {
        assert_eq!(wrap_sum_28(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_28(&[200, 128]), 72);
        assert_eq!(wrap_sum_28(&[255, 1, 8]), 8);
        assert_eq!(wrap_sum_28(&[0, 1, 2, 3, 4]), 10);
    }

    #[test]
    fn wrap_sum_29_wraps_on_overflow() {
        assert_eq!(wrap_sum_29(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_29(&[200, 129]), 73);
        assert_eq!(wrap_sum_29(&[255, 1, 9]), 9);
        assert_eq!(wrap_sum_29(&[1, 2, 3, 4, 5]), 15);
    }
}
