use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 6.
pub fn total_weighted_6(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 6.
pub fn total_biased_6(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 7.
pub fn total_weighted_7(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 7.
pub fn total_biased_7(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 8.
pub fn total_weighted_8(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 8.
pub fn total_biased_8(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 9.
pub fn total_weighted_9(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 9.
pub fn total_biased_9(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 10.
pub fn total_weighted_10(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 10.
pub fn total_biased_10(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 11.
pub fn total_weighted_11(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 11.
pub fn total_biased_11(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_6_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_6(&items, &Weighted(3)), 12);
        assert_eq!(total_biased_6(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_7_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_7(&items, &Weighted(-3)), -15);
        assert_eq!(total_biased_7(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_8_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_8(&items, &Weighted(-2)), -12);
        assert_eq!(total_biased_8(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_9_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_9(&items, &Weighted(-1)), -7);
        assert_eq!(total_biased_9(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_10_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_10(&items, &Weighted(2)), 6);
        assert_eq!(total_biased_10(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_11_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_11(&items, &Weighted(1)), 4);
        assert_eq!(total_biased_11(&items, &Biased(-1)), -1);
    }
}
