/// Count set bits. Variant 42.
pub fn count_set_bits_42(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 42.
pub fn is_odd_parity_42(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 43.
pub fn count_set_bits_43(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 43.
pub fn is_odd_parity_43(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 44.
pub fn count_set_bits_44(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 44.
pub fn is_odd_parity_44(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 45.
pub fn count_set_bits_45(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 45.
pub fn is_odd_parity_45(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 46.
pub fn count_set_bits_46(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 46.
pub fn is_odd_parity_46(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 47.
pub fn count_set_bits_47(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 47.
pub fn is_odd_parity_47(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_ops_42_matches_popcount() {
        assert_eq!(count_set_bits_42(0), 0);
        assert_eq!(is_odd_parity_42(0), false);
        assert_eq!(count_set_bits_42(1), 1);
        assert_eq!(is_odd_parity_42(1), true);
        assert_eq!(count_set_bits_42(255), 8);
        assert_eq!(is_odd_parity_42(255), false);
        assert_eq!(count_set_bits_42(128), 1);
        assert_eq!(is_odd_parity_42(128), true);
        assert_eq!(count_set_bits_42(22), 3);
        assert_eq!(is_odd_parity_42(22), true);
        assert_eq!(count_set_bits_42(185), 5);
        assert_eq!(is_odd_parity_42(185), true);
    }

    #[test]
    fn bit_ops_43_matches_popcount() {
        assert_eq!(count_set_bits_43(0), 0);
        assert_eq!(is_odd_parity_43(0), false);
        assert_eq!(count_set_bits_43(1), 1);
        assert_eq!(is_odd_parity_43(1), true);
        assert_eq!(count_set_bits_43(255), 8);
        assert_eq!(is_odd_parity_43(255), false);
        assert_eq!(count_set_bits_43(128), 1);
        assert_eq!(is_odd_parity_43(128), true);
        assert_eq!(count_set_bits_43(53), 4);
        assert_eq!(is_odd_parity_43(53), false);
        assert_eq!(count_set_bits_43(238), 6);
        assert_eq!(is_odd_parity_43(238), false);
    }

    #[test]
    fn bit_ops_44_matches_popcount() {
        assert_eq!(count_set_bits_44(0), 0);
        assert_eq!(is_odd_parity_44(0), false);
        assert_eq!(count_set_bits_44(1), 1);
        assert_eq!(is_odd_parity_44(1), true);
        assert_eq!(count_set_bits_44(255), 8);
        assert_eq!(is_odd_parity_44(255), false);
        assert_eq!(count_set_bits_44(128), 1);
        assert_eq!(is_odd_parity_44(128), true);
        assert_eq!(count_set_bits_44(84), 3);
        assert_eq!(is_odd_parity_44(84), true);
        assert_eq!(count_set_bits_44(35), 3);
        assert_eq!(is_odd_parity_44(35), true);
    }

    #[test]
    fn bit_ops_45_matches_popcount() {
        assert_eq!(count_set_bits_45(0), 0);
        assert_eq!(is_odd_parity_45(0), false);
        assert_eq!(count_set_bits_45(1), 1);
        assert_eq!(is_odd_parity_45(1), true);
        assert_eq!(count_set_bits_45(255), 8);
        assert_eq!(is_odd_parity_45(255), false);
        assert_eq!(count_set_bits_45(128), 1);
        assert_eq!(is_odd_parity_45(128), true);
        assert_eq!(count_set_bits_45(115), 5);
        assert_eq!(is_odd_parity_45(115), true);
        assert_eq!(count_set_bits_45(88), 3);
        assert_eq!(is_odd_parity_45(88), true);
    }

    #[test]
    fn bit_ops_46_matches_popcount() {
        assert_eq!(count_set_bits_46(0), 0);
        assert_eq!(is_odd_parity_46(0), false);
        assert_eq!(count_set_bits_46(1), 1);
        assert_eq!(is_odd_parity_46(1), true);
        assert_eq!(count_set_bits_46(255), 8);
        assert_eq!(is_odd_parity_46(255), false);
        assert_eq!(count_set_bits_46(128), 1);
        assert_eq!(is_odd_parity_46(128), true);
        assert_eq!(count_set_bits_46(146), 3);
        assert_eq!(is_odd_parity_46(146), true);
        assert_eq!(count_set_bits_46(141), 4);
        assert_eq!(is_odd_parity_46(141), false);
    }

    #[test]
    fn bit_ops_47_matches_popcount() {
        assert_eq!(count_set_bits_47(0), 0);
        assert_eq!(is_odd_parity_47(0), false);
        assert_eq!(count_set_bits_47(1), 1);
        assert_eq!(is_odd_parity_47(1), true);
        assert_eq!(count_set_bits_47(255), 8);
        assert_eq!(is_odd_parity_47(255), false);
        assert_eq!(count_set_bits_47(128), 1);
        assert_eq!(is_odd_parity_47(128), true);
        assert_eq!(count_set_bits_47(177), 4);
        assert_eq!(is_odd_parity_47(177), false);
        assert_eq!(count_set_bits_47(194), 3);
        assert_eq!(is_odd_parity_47(194), true);
    }
}
