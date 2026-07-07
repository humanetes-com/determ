/// Add then clamp to [lo, hi]. Variant 78.
pub fn clamp_add_78(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 79.
pub fn clamp_add_79(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 80.
pub fn clamp_add_80(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 81.
pub fn clamp_add_81(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 82.
pub fn clamp_add_82(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 83.
pub fn clamp_add_83(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_add_78_clamps(){
        assert_eq!(clamp_add_78(10, 5, -128, 128), 15);
        assert_eq!(clamp_add_78(1000, 2000, -128, 128), 128);
        assert_eq!(clamp_add_78(-1000, -2000, -128, 128), -128);
        assert_eq!(clamp_add_78(0, 0, -128, 128), 0);
        assert_eq!(clamp_add_78(78, -78, -128, 128), 0);
    }

    #[test]
    fn clamp_add_79_clamps(){
        assert_eq!(clamp_add_79(10, 5, -129, 129), 15);
        assert_eq!(clamp_add_79(1000, 2000, -129, 129), 129);
        assert_eq!(clamp_add_79(-1000, -2000, -129, 129), -129);
        assert_eq!(clamp_add_79(0, 0, -129, 129), 0);
        assert_eq!(clamp_add_79(79, -79, -129, 129), 0);
    }

    #[test]
    fn clamp_add_80_clamps(){
        assert_eq!(clamp_add_80(10, 5, -130, 130), 15);
        assert_eq!(clamp_add_80(1000, 2000, -130, 130), 130);
        assert_eq!(clamp_add_80(-1000, -2000, -130, 130), -130);
        assert_eq!(clamp_add_80(0, 0, -130, 130), 0);
        assert_eq!(clamp_add_80(80, -80, -130, 130), 0);
    }

    #[test]
    fn clamp_add_81_clamps(){
        assert_eq!(clamp_add_81(10, 5, -131, 131), 15);
        assert_eq!(clamp_add_81(1000, 2000, -131, 131), 131);
        assert_eq!(clamp_add_81(-1000, -2000, -131, 131), -131);
        assert_eq!(clamp_add_81(0, 0, -131, 131), 0);
        assert_eq!(clamp_add_81(81, -81, -131, 131), 0);
    }

    #[test]
    fn clamp_add_82_clamps(){
        assert_eq!(clamp_add_82(10, 5, -132, 132), 15);
        assert_eq!(clamp_add_82(1000, 2000, -132, 132), 132);
        assert_eq!(clamp_add_82(-1000, -2000, -132, 132), -132);
        assert_eq!(clamp_add_82(0, 0, -132, 132), 0);
        assert_eq!(clamp_add_82(82, -82, -132, 132), 0);
    }

    #[test]
    fn clamp_add_83_clamps(){
        assert_eq!(clamp_add_83(10, 5, -133, 133), 15);
        assert_eq!(clamp_add_83(1000, 2000, -133, 133), 133);
        assert_eq!(clamp_add_83(-1000, -2000, -133, 133), -133);
        assert_eq!(clamp_add_83(0, 0, -133, 133), 0);
        assert_eq!(clamp_add_83(83, -83, -133, 133), 0);
    }
}
