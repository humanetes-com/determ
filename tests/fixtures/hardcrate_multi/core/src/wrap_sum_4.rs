/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 72.
pub fn wrap_sum_72(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 73.
pub fn wrap_sum_73(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 74.
pub fn wrap_sum_74(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 75.
pub fn wrap_sum_75(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 76.
pub fn wrap_sum_76(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 77.
pub fn wrap_sum_77(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_sum_72_wraps_on_overflow() {
        assert_eq!(wrap_sum_72(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_72(&[200, 122]), 66);
        assert_eq!(wrap_sum_72(&[255, 1, 2]), 2);
        assert_eq!(wrap_sum_72(&[2, 3, 4, 5, 6]), 20);
    }

    #[test]
    fn wrap_sum_73_wraps_on_overflow() {
        assert_eq!(wrap_sum_73(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_73(&[200, 123]), 67);
        assert_eq!(wrap_sum_73(&[255, 1, 3]), 3);
        assert_eq!(wrap_sum_73(&[3, 4, 5, 6, 7]), 25);
    }

    #[test]
    fn wrap_sum_74_wraps_on_overflow() {
        assert_eq!(wrap_sum_74(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_74(&[200, 124]), 68);
        assert_eq!(wrap_sum_74(&[255, 1, 4]), 4);
        assert_eq!(wrap_sum_74(&[4, 5, 6, 7, 8]), 30);
    }

    #[test]
    fn wrap_sum_75_wraps_on_overflow() {
        assert_eq!(wrap_sum_75(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_75(&[200, 125]), 69);
        assert_eq!(wrap_sum_75(&[255, 1, 5]), 5);
        assert_eq!(wrap_sum_75(&[5, 6, 7, 8, 9]), 35);
    }

    #[test]
    fn wrap_sum_76_wraps_on_overflow() {
        assert_eq!(wrap_sum_76(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_76(&[200, 126]), 70);
        assert_eq!(wrap_sum_76(&[255, 1, 6]), 6);
        assert_eq!(wrap_sum_76(&[6, 7, 8, 9, 10]), 40);
    }

    #[test]
    fn wrap_sum_77_wraps_on_overflow() {
        assert_eq!(wrap_sum_77(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_77(&[200, 127]), 71);
        assert_eq!(wrap_sum_77(&[255, 1, 7]), 7);
        assert_eq!(wrap_sum_77(&[0, 1, 2, 3, 4]), 10);
    }
}
