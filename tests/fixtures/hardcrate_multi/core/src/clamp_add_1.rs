/// Add then clamp to [lo, hi]. Variant 6.
pub fn clamp_add_6(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 7.
pub fn clamp_add_7(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 8.
pub fn clamp_add_8(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 9.
pub fn clamp_add_9(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 10.
pub fn clamp_add_10(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 11.
pub fn clamp_add_11(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_add_6_clamps(){
        assert_eq!(clamp_add_6(10, 5, -56, 56), 15);
        assert_eq!(clamp_add_6(1000, 2000, -56, 56), 56);
        assert_eq!(clamp_add_6(-1000, -2000, -56, 56), -56);
        assert_eq!(clamp_add_6(0, 0, -56, 56), 0);
        assert_eq!(clamp_add_6(6, -6, -56, 56), 0);
    }

    #[test]
    fn clamp_add_7_clamps(){
        assert_eq!(clamp_add_7(10, 5, -57, 57), 15);
        assert_eq!(clamp_add_7(1000, 2000, -57, 57), 57);
        assert_eq!(clamp_add_7(-1000, -2000, -57, 57), -57);
        assert_eq!(clamp_add_7(0, 0, -57, 57), 0);
        assert_eq!(clamp_add_7(7, -7, -57, 57), 0);
    }

    #[test]
    fn clamp_add_8_clamps(){
        assert_eq!(clamp_add_8(10, 5, -58, 58), 15);
        assert_eq!(clamp_add_8(1000, 2000, -58, 58), 58);
        assert_eq!(clamp_add_8(-1000, -2000, -58, 58), -58);
        assert_eq!(clamp_add_8(0, 0, -58, 58), 0);
        assert_eq!(clamp_add_8(8, -8, -58, 58), 0);
    }

    #[test]
    fn clamp_add_9_clamps(){
        assert_eq!(clamp_add_9(10, 5, -59, 59), 15);
        assert_eq!(clamp_add_9(1000, 2000, -59, 59), 59);
        assert_eq!(clamp_add_9(-1000, -2000, -59, 59), -59);
        assert_eq!(clamp_add_9(0, 0, -59, 59), 0);
        assert_eq!(clamp_add_9(9, -9, -59, 59), 0);
    }

    #[test]
    fn clamp_add_10_clamps(){
        assert_eq!(clamp_add_10(10, 5, -60, 60), 15);
        assert_eq!(clamp_add_10(1000, 2000, -60, 60), 60);
        assert_eq!(clamp_add_10(-1000, -2000, -60, 60), -60);
        assert_eq!(clamp_add_10(0, 0, -60, 60), 0);
        assert_eq!(clamp_add_10(10, -10, -60, 60), 0);
    }

    #[test]
    fn clamp_add_11_clamps(){
        assert_eq!(clamp_add_11(10, 5, -61, 61), 15);
        assert_eq!(clamp_add_11(1000, 2000, -61, 61), 61);
        assert_eq!(clamp_add_11(-1000, -2000, -61, 61), -61);
        assert_eq!(clamp_add_11(0, 0, -61, 61), 0);
        assert_eq!(clamp_add_11(11, -11, -61, 61), 0);
    }
}
