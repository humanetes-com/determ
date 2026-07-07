use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 0.
pub fn total_weighted_0(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 0.
pub fn total_biased_0(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 1.
pub fn total_weighted_1(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 1.
pub fn total_biased_1(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 2.
pub fn total_weighted_2(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 2.
pub fn total_biased_2(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 3.
pub fn total_weighted_3(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 3.
pub fn total_biased_3(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 4.
pub fn total_weighted_4(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 4.
pub fn total_biased_4(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 5.
pub fn total_weighted_5(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 5.
pub fn total_biased_5(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_0_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_0(&items, &Weighted(-3)), -9);
        assert_eq!(total_biased_0(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_1_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_1(&items, &Weighted(-2)), -8);
        assert_eq!(total_biased_1(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_2_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_2(&items, &Weighted(-1)), -5);
        assert_eq!(total_biased_2(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_3_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_3(&items, &Weighted(2)), 12);
        assert_eq!(total_biased_3(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_4_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_4(&items, &Weighted(1)), 7);
        assert_eq!(total_biased_4(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_5_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_5(&items, &Weighted(2)), 6);
        assert_eq!(total_biased_5(&items, &Biased(-2)), -7);
    }
}
