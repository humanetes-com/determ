use hcm_core::{Biased, Scorer, Weighted};

/// Total score of `items` under a `Weighted` scorer. Variant 54.
pub fn total_weighted_54(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 54.
pub fn total_biased_54(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 55.
pub fn total_weighted_55(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 55.
pub fn total_biased_55(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 56.
pub fn total_weighted_56(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 56.
pub fn total_biased_56(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 57.
pub fn total_weighted_57(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 57.
pub fn total_biased_57(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 58.
pub fn total_weighted_58(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 58.
pub fn total_biased_58(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

/// Total score of `items` under a `Weighted` scorer. Variant 59.
pub fn total_weighted_59(items: &[i32], w: &Weighted) -> i32 {
    items.iter().map(|&x| w.score(x)).sum()
}

/// Total score of `items` under a `Biased` scorer. Variant 59.
pub fn total_biased_59(items: &[i32], b: &Biased) -> i32 {
    items.iter().map(|&x| b.score(x)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn consumer_54_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_54(&items, &Weighted(2)), 14);
        assert_eq!(total_biased_54(&items, &Biased(2)), 17);
    }

    #[test]
    fn consumer_55_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 1, -4];
        assert_eq!(total_weighted_55(&items, &Weighted(3)), 9);
        assert_eq!(total_biased_55(&items, &Biased(-2)), -7);
    }

    #[test]
    fn consumer_56_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 2, -4];
        assert_eq!(total_weighted_56(&items, &Weighted(-3)), -12);
        assert_eq!(total_biased_56(&items, &Biased(-1)), -1);
    }

    #[test]
    fn consumer_57_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 3, -4];
        assert_eq!(total_weighted_57(&items, &Weighted(-2)), -10);
        assert_eq!(total_biased_57(&items, &Biased(0)), 5);
    }

    #[test]
    fn consumer_58_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 4, -4];
        assert_eq!(total_weighted_58(&items, &Weighted(-1)), -6);
        assert_eq!(total_biased_58(&items, &Biased(1)), 11);
    }

    #[test]
    fn consumer_59_totals_match() {
        let items: Vec<i32> = vec![1, 2, 3, 5, -4];
        assert_eq!(total_weighted_59(&items, &Weighted(2)), 14);
        assert_eq!(total_biased_59(&items, &Biased(2)), 17);
    }
}
