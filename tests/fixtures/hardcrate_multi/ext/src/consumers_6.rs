use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 30.
pub fn total_weighted_30(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 30.
pub fn total_biased_30(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 31.
pub fn total_weighted_31(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 31.
pub fn total_biased_31(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 32.
pub fn total_weighted_32(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 32.
pub fn total_biased_32(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 33.
pub fn total_weighted_33(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 33.
pub fn total_biased_33(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 34.
pub fn total_weighted_34(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 34.
pub fn total_biased_34(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 35.
pub fn total_weighted_35(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 35.
pub fn total_biased_35(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_30_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_30(&items, &Weighted(-1)), -3);
        assert_eq!(total_biased_30(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_31_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_31(&items, &Weighted(2)), 8);
        assert_eq!(total_biased_31(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_32_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_32(&items, &Weighted(1)), 5);
        assert_eq!(total_biased_32(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_33_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_33(&items, &Weighted(2)), 12);
        assert_eq!(total_biased_33(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_34_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_34(&items, &Weighted(3)), 21);
        assert_eq!(total_biased_34(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_35_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_35(&items, &Weighted(-3)), -9);
        assert_eq!(total_biased_35(&items, &Biased(-2)), -7);
    }
}
