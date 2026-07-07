/// Count set bits. Variant 18.
pub fn count_set_bits_18(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 18.
pub fn is_odd_parity_18(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 19.
pub fn count_set_bits_19(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 19.
pub fn is_odd_parity_19(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 20.
pub fn count_set_bits_20(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 20.
pub fn is_odd_parity_20(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 21.
pub fn count_set_bits_21(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 21.
pub fn is_odd_parity_21(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 22.
pub fn count_set_bits_22(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 22.
pub fn is_odd_parity_22(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 23.
pub fn count_set_bits_23(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 23.
pub fn is_odd_parity_23(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_ops_18_matches_popcount() {
        assert_eq!(count_set_bits_18(0), 0);
        assert_eq!(is_odd_parity_18(0), false);
        assert_eq!(count_set_bits_18(1), 1);
        assert_eq!(is_odd_parity_18(1), true);
        assert_eq!(count_set_bits_18(255), 8);
        assert_eq!(is_odd_parity_18(255), false);
        assert_eq!(count_set_bits_18(128), 1);
        assert_eq!(is_odd_parity_18(128), true);
        assert_eq!(count_set_bits_18(46), 4);
        assert_eq!(is_odd_parity_18(46), false);
        assert_eq!(count_set_bits_18(193), 3);
        assert_eq!(is_odd_parity_18(193), true);
    }

    #[test]
    fn bit_ops_19_matches_popcount() {
        assert_eq!(count_set_bits_19(0), 0);
        assert_eq!(is_odd_parity_19(0), false);
        assert_eq!(count_set_bits_19(1), 1);
        assert_eq!(is_odd_parity_19(1), true);
        assert_eq!(count_set_bits_19(255), 8);
        assert_eq!(is_odd_parity_19(255), false);
        assert_eq!(count_set_bits_19(128), 1);
        assert_eq!(is_odd_parity_19(128), true);
        assert_eq!(count_set_bits_19(77), 4);
        assert_eq!(is_odd_parity_19(77), false);
        assert_eq!(count_set_bits_19(246), 6);
        assert_eq!(is_odd_parity_19(246), false);
    }

    #[test]
    fn bit_ops_20_matches_popcount() {
        assert_eq!(count_set_bits_20(0), 0);
        assert_eq!(is_odd_parity_20(0), false);
        assert_eq!(count_set_bits_20(1), 1);
        assert_eq!(is_odd_parity_20(1), true);
        assert_eq!(count_set_bits_20(255), 8);
        assert_eq!(is_odd_parity_20(255), false);
        assert_eq!(count_set_bits_20(128), 1);
        assert_eq!(is_odd_parity_20(128), true);
        assert_eq!(count_set_bits_20(108), 4);
        assert_eq!(is_odd_parity_20(108), false);
        assert_eq!(count_set_bits_20(43), 4);
        assert_eq!(is_odd_parity_20(43), false);
    }

    #[test]
    fn bit_ops_21_matches_popcount() {
        assert_eq!(count_set_bits_21(0), 0);
        assert_eq!(is_odd_parity_21(0), false);
        assert_eq!(count_set_bits_21(1), 1);
        assert_eq!(is_odd_parity_21(1), true);
        assert_eq!(count_set_bits_21(255), 8);
        assert_eq!(is_odd_parity_21(255), false);
        assert_eq!(count_set_bits_21(128), 1);
        assert_eq!(is_odd_parity_21(128), true);
        assert_eq!(count_set_bits_21(139), 4);
        assert_eq!(is_odd_parity_21(139), false);
        assert_eq!(count_set_bits_21(96), 2);
        assert_eq!(is_odd_parity_21(96), false);
    }

    #[test]
    fn bit_ops_22_matches_popcount() {
        assert_eq!(count_set_bits_22(0), 0);
        assert_eq!(is_odd_parity_22(0), false);
        assert_eq!(count_set_bits_22(1), 1);
        assert_eq!(is_odd_parity_22(1), true);
        assert_eq!(count_set_bits_22(255), 8);
        assert_eq!(is_odd_parity_22(255), false);
        assert_eq!(count_set_bits_22(128), 1);
        assert_eq!(is_odd_parity_22(128), true);
        assert_eq!(count_set_bits_22(170), 4);
        assert_eq!(is_odd_parity_22(170), false);
        assert_eq!(count_set_bits_22(149), 4);
        assert_eq!(is_odd_parity_22(149), false);
    }

    #[test]
    fn bit_ops_23_matches_popcount() {
        assert_eq!(count_set_bits_23(0), 0);
        assert_eq!(is_odd_parity_23(0), false);
        assert_eq!(count_set_bits_23(1), 1);
        assert_eq!(is_odd_parity_23(1), true);
        assert_eq!(count_set_bits_23(255), 8);
        assert_eq!(is_odd_parity_23(255), false);
        assert_eq!(count_set_bits_23(128), 1);
        assert_eq!(is_odd_parity_23(128), true);
        assert_eq!(count_set_bits_23(201), 4);
        assert_eq!(is_odd_parity_23(201), false);
        assert_eq!(count_set_bits_23(202), 4);
        assert_eq!(is_odd_parity_23(202), false);
    }
}
