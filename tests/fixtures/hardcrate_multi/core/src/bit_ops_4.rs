/// Count set bits. Variant 90.
pub fn count_set_bits_90(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 90.
pub fn is_odd_parity_90(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 91.
pub fn count_set_bits_91(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 91.
pub fn is_odd_parity_91(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 92.
pub fn count_set_bits_92(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 92.
pub fn is_odd_parity_92(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 93.
pub fn count_set_bits_93(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 93.
pub fn is_odd_parity_93(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 94.
pub fn count_set_bits_94(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 94.
pub fn is_odd_parity_94(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

/// Count set bits. Variant 95.
pub fn count_set_bits_95(x: u8) -> u32 {
    x.count_ones()
}

/// True if `x` has an odd number of set bits. Variant 95.
pub fn is_odd_parity_95(x: u8) -> bool {
    x.count_ones() % 2 == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_ops_90_matches_popcount() {
        assert_eq!(count_set_bits_90(0), 0);
        assert_eq!(is_odd_parity_90(0), false);
        assert_eq!(count_set_bits_90(1), 1);
        assert_eq!(is_odd_parity_90(1), true);
        assert_eq!(count_set_bits_90(255), 8);
        assert_eq!(is_odd_parity_90(255), false);
        assert_eq!(count_set_bits_90(128), 1);
        assert_eq!(is_odd_parity_90(128), true);
        assert_eq!(count_set_bits_90(230), 5);
        assert_eq!(is_odd_parity_90(230), true);
        assert_eq!(count_set_bits_90(169), 4);
        assert_eq!(is_odd_parity_90(169), false);
    }

    #[test]
    fn bit_ops_91_matches_popcount() {
        assert_eq!(count_set_bits_91(0), 0);
        assert_eq!(is_odd_parity_91(0), false);
        assert_eq!(count_set_bits_91(1), 1);
        assert_eq!(is_odd_parity_91(1), true);
        assert_eq!(count_set_bits_91(255), 8);
        assert_eq!(is_odd_parity_91(255), false);
        assert_eq!(count_set_bits_91(128), 1);
        assert_eq!(is_odd_parity_91(128), true);
        assert_eq!(count_set_bits_91(5), 2);
        assert_eq!(is_odd_parity_91(5), false);
        assert_eq!(count_set_bits_91(222), 6);
        assert_eq!(is_odd_parity_91(222), false);
    }

    #[test]
    fn bit_ops_92_matches_popcount() {
        assert_eq!(count_set_bits_92(0), 0);
        assert_eq!(is_odd_parity_92(0), false);
        assert_eq!(count_set_bits_92(1), 1);
        assert_eq!(is_odd_parity_92(1), true);
        assert_eq!(count_set_bits_92(255), 8);
        assert_eq!(is_odd_parity_92(255), false);
        assert_eq!(count_set_bits_92(128), 1);
        assert_eq!(is_odd_parity_92(128), true);
        assert_eq!(count_set_bits_92(36), 2);
        assert_eq!(is_odd_parity_92(36), false);
        assert_eq!(count_set_bits_92(19), 3);
        assert_eq!(is_odd_parity_92(19), true);
    }

    #[test]
    fn bit_ops_93_matches_popcount() {
        assert_eq!(count_set_bits_93(0), 0);
        assert_eq!(is_odd_parity_93(0), false);
        assert_eq!(count_set_bits_93(1), 1);
        assert_eq!(is_odd_parity_93(1), true);
        assert_eq!(count_set_bits_93(255), 8);
        assert_eq!(is_odd_parity_93(255), false);
        assert_eq!(count_set_bits_93(128), 1);
        assert_eq!(is_odd_parity_93(128), true);
        assert_eq!(count_set_bits_93(67), 3);
        assert_eq!(is_odd_parity_93(67), true);
        assert_eq!(count_set_bits_93(72), 2);
        assert_eq!(is_odd_parity_93(72), false);
    }

    #[test]
    fn bit_ops_94_matches_popcount() {
        assert_eq!(count_set_bits_94(0), 0);
        assert_eq!(is_odd_parity_94(0), false);
        assert_eq!(count_set_bits_94(1), 1);
        assert_eq!(is_odd_parity_94(1), true);
        assert_eq!(count_set_bits_94(255), 8);
        assert_eq!(is_odd_parity_94(255), false);
        assert_eq!(count_set_bits_94(128), 1);
        assert_eq!(is_odd_parity_94(128), true);
        assert_eq!(count_set_bits_94(98), 3);
        assert_eq!(is_odd_parity_94(98), true);
        assert_eq!(count_set_bits_94(125), 6);
        assert_eq!(is_odd_parity_94(125), false);
    }

    #[test]
    fn bit_ops_95_matches_popcount() {
        assert_eq!(count_set_bits_95(0), 0);
        assert_eq!(is_odd_parity_95(0), false);
        assert_eq!(count_set_bits_95(1), 1);
        assert_eq!(is_odd_parity_95(1), true);
        assert_eq!(count_set_bits_95(255), 8);
        assert_eq!(is_odd_parity_95(255), false);
        assert_eq!(count_set_bits_95(128), 1);
        assert_eq!(is_odd_parity_95(128), true);
        assert_eq!(count_set_bits_95(129), 2);
        assert_eq!(is_odd_parity_95(129), false);
        assert_eq!(count_set_bits_95(178), 4);
        assert_eq!(is_odd_parity_95(178), false);
    }
}
