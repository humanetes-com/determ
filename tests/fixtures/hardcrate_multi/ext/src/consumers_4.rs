use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 18.
pub fn total_weighted_18(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 18.
pub fn total_biased_18(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 19.
pub fn total_weighted_19(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 19.
pub fn total_biased_19(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 20.
pub fn total_weighted_20(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 20.
pub fn total_biased_20(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 21.
pub fn total_weighted_21(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 21.
pub fn total_biased_21(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 22.
pub fn total_weighted_22(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 22.
pub fn total_biased_22(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 23.
pub fn total_weighted_23(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 23.
pub fn total_biased_23(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_18_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_18(&items, &Weighted(1)), 6);
        assert_eq!(total_biased_18(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_19_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_19(&items, &Weighted(2)), 14);
        assert_eq!(total_biased_19(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_20_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_20(&items, &Weighted(3)), 9);
        assert_eq!(total_biased_20(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_21_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_21(&items, &Weighted(-3)), -12);
        assert_eq!(total_biased_21(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_22_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_22(&items, &Weighted(-2)), -10);
        assert_eq!(total_biased_22(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_23_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_23(&items, &Weighted(-1)), -6);
        assert_eq!(total_biased_23(&items, &Biased(1)), 11);
    }
}
