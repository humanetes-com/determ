/// Count set bits. Variant 66.
pub fn count_set_bits_66(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 66.
pub fn is_odd_parity_66(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 67.
pub fn count_set_bits_67(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 67.
pub fn is_odd_parity_67(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 68.
pub fn count_set_bits_68(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 68.
pub fn is_odd_parity_68(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 69.
pub fn count_set_bits_69(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 69.
pub fn is_odd_parity_69(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 70.
pub fn count_set_bits_70(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 70.
pub fn is_odd_parity_70(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 71.
pub fn count_set_bits_71(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 71.
pub fn is_odd_parity_71(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_ops_66_matches_popcount() {
        assert_eq!(count_set_bits_66(0), 0);
        assert_eq!(is_odd_parity_66(0), false);
        assert_eq!(count_set_bits_66(1), 1);
        assert_eq!(is_odd_parity_66(1), true);
        assert_eq!(count_set_bits_66(255), 8);
        assert_eq!(is_odd_parity_66(255), false);
        assert_eq!(count_set_bits_66(128), 1);
        assert_eq!(is_odd_parity_66(128), true);
        assert_eq!(count_set_bits_66(254), 7);
        assert_eq!(is_odd_parity_66(254), true);
        assert_eq!(count_set_bits_66(177), 4);
        assert_eq!(is_odd_parity_66(177), false);
    }

    #[test]
    fn bit_ops_67_matches_popcount() {
        assert_eq!(count_set_bits_67(0), 0);
        assert_eq!(is_odd_parity_67(0), false);
        assert_eq!(count_set_bits_67(1), 1);
        assert_eq!(is_odd_parity_67(1), true);
        assert_eq!(count_set_bits_67(255), 8);
        assert_eq!(is_odd_parity_67(255), false);
        assert_eq!(count_set_bits_67(128), 1);
        assert_eq!(is_odd_parity_67(128), true);
        assert_eq!(count_set_bits_67(29), 4);
        assert_eq!(is_odd_parity_67(29), false);
        assert_eq!(count_set_bits_67(230), 5);
        assert_eq!(is_odd_parity_67(230), true);
    }

    #[test]
    fn bit_ops_68_matches_popcount() {
        assert_eq!(count_set_bits_68(0), 0);
        assert_eq!(is_odd_parity_68(0), false);
        assert_eq!(count_set_bits_68(1), 1);
        assert_eq!(is_odd_parity_68(1), true);
        assert_eq!(count_set_bits_68(255), 8);
        assert_eq!(is_odd_parity_68(255), false);
        assert_eq!(count_set_bits_68(128), 1);
        assert_eq!(is_odd_parity_68(128), true);
        assert_eq!(count_set_bits_68(60), 4);
        assert_eq!(is_odd_parity_68(60), false);
        assert_eq!(count_set_bits_68(27), 4);
        assert_eq!(is_odd_parity_68(27), false);
    }

    #[test]
    fn bit_ops_69_matches_popcount() {
        assert_eq!(count_set_bits_69(0), 0);
        assert_eq!(is_odd_parity_69(0), false);
        assert_eq!(count_set_bits_69(1), 1);
        assert_eq!(is_odd_parity_69(1), true);
        assert_eq!(count_set_bits_69(255), 8);
        assert_eq!(is_odd_parity_69(255), false);
        assert_eq!(count_set_bits_69(128), 1);
        assert_eq!(is_odd_parity_69(128), true);
        assert_eq!(count_set_bits_69(91), 5);
        assert_eq!(is_odd_parity_69(91), true);
        assert_eq!(count_set_bits_69(80), 2);
        assert_eq!(is_odd_parity_69(80), false);
    }

    #[test]
    fn bit_ops_70_matches_popcount() {
        assert_eq!(count_set_bits_70(0), 0);
        assert_eq!(is_odd_parity_70(0), false);
        assert_eq!(count_set_bits_70(1), 1);
        assert_eq!(is_odd_parity_70(1), true);
        assert_eq!(count_set_bits_70(255), 8);
        assert_eq!(is_odd_parity_70(255), false);
        assert_eq!(count_set_bits_70(128), 1);
        assert_eq!(is_odd_parity_70(128), true);
        assert_eq!(count_set_bits_70(122), 5);
        assert_eq!(is_odd_parity_70(122), true);
        assert_eq!(count_set_bits_70(133), 3);
        assert_eq!(is_odd_parity_70(133), true);
    }

    #[test]
    fn bit_ops_71_matches_popcount() {
        assert_eq!(count_set_bits_71(0), 0);
        assert_eq!(is_odd_parity_71(0), false);
        assert_eq!(count_set_bits_71(1), 1);
        assert_eq!(is_odd_parity_71(1), true);
        assert_eq!(count_set_bits_71(255), 8);
        assert_eq!(is_odd_parity_71(255), false);
        assert_eq!(count_set_bits_71(128), 1);
        assert_eq!(is_odd_parity_71(128), true);
        assert_eq!(count_set_bits_71(153), 4);
        assert_eq!(is_odd_parity_71(153), false);
        assert_eq!(count_set_bits_71(186), 5);
        assert_eq!(is_odd_parity_71(186), true);
    }
}
