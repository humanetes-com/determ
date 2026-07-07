/// Add then clamp to [lo, hi]. Variant 30.
pub fn clamp_add_30(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 31.
pub fn clamp_add_31(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 32.
pub fn clamp_add_32(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 33.
pub fn clamp_add_33(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 34.
pub fn clamp_add_34(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 35.
pub fn clamp_add_35(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_add_30_clamps(){
        assert_eq!(clamp_add_30(10, 5, -80, 80), 15);
        assert_eq!(clamp_add_30(1000, 2000, -80, 80), 80);
        assert_eq!(clamp_add_30(-1000, -2000, -80, 80), -80);
        assert_eq!(clamp_add_30(0, 0, -80, 80), 0);
        assert_eq!(clamp_add_30(30, -30, -80, 80), 0);
    }

    #[test]
    fn clamp_add_31_clamps(){
        assert_eq!(clamp_add_31(10, 5, -81, 81), 15);
        assert_eq!(clamp_add_31(1000, 2000, -81, 81), 81);
        assert_eq!(clamp_add_31(-1000, -2000, -81, 81), -81);
        assert_eq!(clamp_add_31(0, 0, -81, 81), 0);
        assert_eq!(clamp_add_31(31, -31, -81, 81), 0);
    }

    #[test]
    fn clamp_add_32_clamps(){
        assert_eq!(clamp_add_32(10, 5, -82, 82), 15);
        assert_eq!(clamp_add_32(1000, 2000, -82, 82), 82);
        assert_eq!(clamp_add_32(-1000, -2000, -82, 82), -82);
        assert_eq!(clamp_add_32(0, 0, -82, 82), 0);
        assert_eq!(clamp_add_32(32, -32, -82, 82), 0);
    }

    #[test]
    fn clamp_add_33_clamps(){
        assert_eq!(clamp_add_33(10, 5, -83, 83), 15);
        assert_eq!(clamp_add_33(1000, 2000, -83, 83), 83);
        assert_eq!(clamp_add_33(-1000, -2000, -83, 83), -83);
        assert_eq!(clamp_add_33(0, 0, -83, 83), 0);
        assert_eq!(clamp_add_33(33, -33, -83, 83), 0);
    }

    #[test]
    fn clamp_add_34_clamps(){
        assert_eq!(clamp_add_34(10, 5, -84, 84), 15);
        assert_eq!(clamp_add_34(1000, 2000, -84, 84), 84);
        assert_eq!(clamp_add_34(-1000, -2000, -84, 84), -84);
        assert_eq!(clamp_add_34(0, 0, -84, 84), 0);
        assert_eq!(clamp_add_34(34, -34, -84, 84), 0);
    }

    #[test]
    fn clamp_add_35_clamps(){
        assert_eq!(clamp_add_35(10, 5, -85, 85), 15);
        assert_eq!(clamp_add_35(1000, 2000, -85, 85), 85);
        assert_eq!(clamp_add_35(-1000, -2000, -85, 85), -85);
        assert_eq!(clamp_add_35(0, 0, -85, 85), 0);
        assert_eq!(clamp_add_35(35, -35, -85, 85), 0);
    }
}
