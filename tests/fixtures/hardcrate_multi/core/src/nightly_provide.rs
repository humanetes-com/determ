//! Real nightly-toolchain trap (not a `--cfg` stand-in): gated on
//! `hcm_nightly`, which `build.rs` only sets when it can actually compile the
//! `error_generic_member_access` API — mirrors anyhow's own `src/nightly.rs`
//! probe technique exactly (same feature, same `Request`/`provide_ref` shape),
//! so the pipeline's nightly-cluster/toolchain-selection path gets a SECOND,
//! independent crate to prove itself on, not just anyhow.

use core::error::{self, Error, Request};
use core::fmt;

#[derive(Debug)]
pub struct TaggedError {
    pub tag: u32,
}

impl fmt::Display for TaggedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "tagged error: {}", self.tag)
    }
}

impl Error for TaggedError {
    fn provide<'a>(&'a self, request: &mut Request<'a>) {
        request.provide_ref::<u32>(&self.tag);
    }
}

/// Pull the `u32` tag back out of any `dyn Error` that provided one.
pub fn extract_tag(err: &dyn Error) -> Option<u32> {
    error::request_ref::<u32>(err).copied()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_provided_tag() {
        let e = TaggedError { tag: 42 };
        assert_eq!(extract_tag(&e), Some(42));
    }

    #[derive(Debug)]
    struct Other;

    impl fmt::Display for Other {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            write!(f, "other")
        }
    }

    impl Error for Other {}

    #[test]
    fn none_for_error_that_never_provided() {
        assert_eq!(extract_tag(&Other), None);
    }
}
