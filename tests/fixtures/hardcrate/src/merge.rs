//! TRAP T2 (MED) — `merge_first_wins`.
//!
//! Obvious-wrong attractor: the library habit is LAST-wins (HashMap::extend,
//! `b` overrides `a`). Here `a` wins on a key conflict (first-wins), and the
//! output is sorted by key ascending. Last-wins fails the conflict case; an
//! unsorted concat fails the ordering case.

/// Merge two key→value lists into one, sorted by key ascending.
///
/// Each input has unique keys. (Conflict resolution is pinned by the tests.)
pub fn merge_first_wins(a: &[(u8, u8)], b: &[(u8, u8)]) -> Vec<(u8, u8)> {
    let mut out: Vec<(u8, u8)> = a.to_vec();
    for &(k, v) in b {
        if !out.iter().any(|&(ek, _)| ek == k) {
            out.push((k, v));
        }
    }
    out.sort_by_key(|&(k, _)| k);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_wins_on_conflict() {
        // key 2 is in both: a's value (20) wins, not b's (99).
        let out = merge_first_wins(&[(2, 20), (1, 10)], &[(2, 99), (3, 30)]);
        assert_eq!(out, vec![(1, 10), (2, 20), (3, 30)]);
    }

    #[test]
    fn single_conflict_keeps_a() {
        assert_eq!(merge_first_wins(&[(5, 1)], &[(5, 2)]), vec![(5, 1)]);
    }

    #[test]
    fn handles_empty_sides() {
        assert_eq!(merge_first_wins(&[], &[(1, 1)]), vec![(1, 1)]);
        assert_eq!(merge_first_wins(&[(1, 1)], &[]), vec![(1, 1)]);
        assert_eq!(merge_first_wins(&[], &[]), Vec::<(u8, u8)>::new());
    }
}
