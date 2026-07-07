use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 48.
pub fn total_weighted_48(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 48.
pub fn total_biased_48(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 49.
pub fn total_weighted_49(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 49.
pub fn total_biased_49(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 50.
pub fn total_weighted_50(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 50.
pub fn total_biased_50(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 51.
pub fn total_weighted_51(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 51.
pub fn total_biased_51(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 52.
pub fn total_weighted_52(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 52.
pub fn total_biased_52(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 53.
pub fn total_weighted_53(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 53.
pub fn total_biased_53(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_48_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_48(&items, &Weighted(3)), 18);
        assert_eq!(total_biased_48(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_49_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_49(&items, &Weighted(-3)), -21);
        assert_eq!(total_biased_49(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_50_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_50(&items, &Weighted(-2)), -6);
        assert_eq!(total_biased_50(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_51_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_51(&items, &Weighted(-1)), -4);
        assert_eq!(total_biased_51(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_52_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_52(&items, &Weighted(2)), 10);
        assert_eq!(total_biased_52(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_53_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_53(&items, &Weighted(1)), 6);
        assert_eq!(total_biased_53(&items, &Biased(1)), 11);
    }
}
