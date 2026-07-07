use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 42.
pub fn total_weighted_42(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 42.
pub fn total_biased_42(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 43.
pub fn total_weighted_43(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 43.
pub fn total_biased_43(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 44.
pub fn total_weighted_44(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 44.
pub fn total_biased_44(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 45.
pub fn total_weighted_45(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 45.
pub fn total_biased_45(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 46.
pub fn total_weighted_46(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 46.
pub fn total_biased_46(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 47.
pub fn total_weighted_47(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 47.
pub fn total_biased_47(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_42_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_42(&items, &Weighted(-3)), -15);
        assert_eq!(total_biased_42(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_43_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_43(&items, &Weighted(-2)), -12);
        assert_eq!(total_biased_43(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_44_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_44(&items, &Weighted(-1)), -7);
        assert_eq!(total_biased_44(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_45_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_45(&items, &Weighted(2)), 6);
        assert_eq!(total_biased_45(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_46_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_46(&items, &Weighted(1)), 4);
        assert_eq!(total_biased_46(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_47_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_47(&items, &Weighted(2)), 10);
        assert_eq!(total_biased_47(&items, &Biased(0)), 5);
    }
}
