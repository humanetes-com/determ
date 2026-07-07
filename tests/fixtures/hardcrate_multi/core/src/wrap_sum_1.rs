/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 0.
pub fn wrap_sum_0(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 1.
pub fn wrap_sum_1(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 2.
pub fn wrap_sum_2(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 3.
pub fn wrap_sum_3(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 4.
pub fn wrap_sum_4(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant 5.
pub fn wrap_sum_5(xs: &[u8]) -> u8 {
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_sum_0_wraps_on_overflow() {
        assert_eq!(wrap_sum_0(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_0(&[200, 100]), 44);
        assert_eq!(wrap_sum_0(&[255, 1, 0]), 0);
        assert_eq!(wrap_sum_0(&[0, 1, 2, 3, 4]), 10);
    }

    #[test]
    fn wrap_sum_1_wraps_on_overflow() {
        assert_eq!(wrap_sum_1(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_1(&[200, 101]), 45);
        assert_eq!(wrap_sum_1(&[255, 1, 1]), 1);
        assert_eq!(wrap_sum_1(&[1, 2, 3, 4, 5]), 15);
    }

    #[test]
    fn wrap_sum_2_wraps_on_overflow() {
        assert_eq!(wrap_sum_2(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_2(&[200, 102]), 46);
        assert_eq!(wrap_sum_2(&[255, 1, 2]), 2);
        assert_eq!(wrap_sum_2(&[2, 3, 4, 5, 6]), 20);
    }

    #[test]
    fn wrap_sum_3_wraps_on_overflow() {
        assert_eq!(wrap_sum_3(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_3(&[200, 103]), 47);
        assert_eq!(wrap_sum_3(&[255, 1, 3]), 3);
        assert_eq!(wrap_sum_3(&[3, 4, 5, 6, 7]), 25);
    }

    #[test]
    fn wrap_sum_4_wraps_on_overflow() {
        assert_eq!(wrap_sum_4(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_4(&[200, 104]), 48);
        assert_eq!(wrap_sum_4(&[255, 1, 4]), 4);
        assert_eq!(wrap_sum_4(&[4, 5, 6, 7, 8]), 30);
    }

    #[test]
    fn wrap_sum_5_wraps_on_overflow() {
        assert_eq!(wrap_sum_5(&[1, 2, 3]), 6);
        assert_eq!(wrap_sum_5(&[200, 105]), 49);
        assert_eq!(wrap_sum_5(&[255, 1, 5]), 5);
        assert_eq!(wrap_sum_5(&[5, 6, 7, 8, 9]), 35);
    }
}
