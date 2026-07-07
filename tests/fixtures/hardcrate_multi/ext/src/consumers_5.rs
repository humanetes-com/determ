use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 24.
pub fn total_weighted_24(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 24.
pub fn total_biased_24(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 25.
pub fn total_weighted_25(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 25.
pub fn total_biased_25(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 26.
pub fn total_weighted_26(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 26.
pub fn total_biased_26(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 27.
pub fn total_weighted_27(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 27.
pub fn total_biased_27(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 28.
pub fn total_weighted_28(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 28.
pub fn total_biased_28(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 29.
pub fn total_weighted_29(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 29.
pub fn total_biased_29(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_24_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_24(&items, &Weighted(2)), 14);
        assert_eq!(total_biased_24(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_25_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_25(&items, &Weighted(1)), 3);
        assert_eq!(total_biased_25(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_26_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_26(&items, &Weighted(2)), 8);
        assert_eq!(total_biased_26(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_27_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_27(&items, &Weighted(3)), 15);
        assert_eq!(total_biased_27(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_28_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_28(&items, &Weighted(-3)), -18);
        assert_eq!(total_biased_28(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_29_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_29(&items, &Weighted(-2)), -14);
        assert_eq!(total_biased_29(&items, &Biased(2)), 17);
    }
}
