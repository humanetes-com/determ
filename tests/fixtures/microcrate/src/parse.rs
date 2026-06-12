//! Fallible parsing — the Option/`?`-chain reconstruction path.

/// Parse `"<i32>,<i32>"` into a pair; `None` on any malformed input.
pub fn parse_pair(s: &str) -> Option<(i32, i32)> {
    let (l, r) = s.split_once(',')?;
    let l = l.trim().parse().ok()?;
    let r = r.trim().parse().ok()?;
    Some((l, r))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_trimmed_pairs() {
        assert_eq!(parse_pair("3,4"), Some((3, 4)));
        assert_eq!(parse_pair(" -2 , 7 "), Some((-2, 7)));
    }

    #[test]
    fn rejects_malformed() {
        assert_eq!(parse_pair("nope"), None);
        assert_eq!(parse_pair("1,"), None);
        assert_eq!(parse_pair(",2"), None);
    }
}
