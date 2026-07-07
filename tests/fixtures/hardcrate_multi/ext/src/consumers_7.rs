use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 36.
pub fn total_weighted_36(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 36.
pub fn total_biased_36(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 37.
pub fn total_weighted_37(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 37.
pub fn total_biased_37(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 38.
pub fn total_weighted_38(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 38.
pub fn total_biased_38(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 39.
pub fn total_weighted_39(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 39.
pub fn total_biased_39(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 40.
pub fn total_weighted_40(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 40.
pub fn total_biased_40(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 41.
pub fn total_weighted_41(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 41.
pub fn total_biased_41(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_36_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_36(&items, &Weighted(-2)), -8);
        assert_eq!(total_biased_36(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_37_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_37(&items, &Weighted(-1)), -5);
        assert_eq!(total_biased_37(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_38_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_38(&items, &Weighted(2)), 12);
        assert_eq!(total_biased_38(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_39_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_39(&items, &Weighted(1)), 7);
        assert_eq!(total_biased_39(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_40_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_40(&items, &Weighted(2)), 6);
        assert_eq!(total_biased_40(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_41_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_41(&items, &Weighted(3)), 12);
        assert_eq!(total_biased_41(&items, &Biased(-1)), -1);
    }
}
