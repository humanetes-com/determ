use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 12.
pub fn total_weighted_12(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 12.
pub fn total_biased_12(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 13.
pub fn total_weighted_13(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 13.
pub fn total_biased_13(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 14.
pub fn total_weighted_14(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 14.
pub fn total_biased_14(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 15.
pub fn total_weighted_15(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 15.
pub fn total_biased_15(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 16.
pub fn total_weighted_16(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 16.
pub fn total_biased_16(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 17.
pub fn total_weighted_17(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 17.
pub fn total_biased_17(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_12_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_12(&items, &Weighted(2)), 10);
        assert_eq!(total_biased_12(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_13_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_13(&items, &Weighted(3)), 18);
        assert_eq!(total_biased_13(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_14_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_14(&items, &Weighted(-3)), -21);
        assert_eq!(total_biased_14(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_15_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_15(&items, &Weighted(-2)), -6);
        assert_eq!(total_biased_15(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_16_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_16(&items, &Weighted(-1)), -4);
        assert_eq!(total_biased_16(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_17_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_17(&items, &Weighted(2)), 10);
        assert_eq!(total_biased_17(&items, &Biased(0)), 5);
    }
}
