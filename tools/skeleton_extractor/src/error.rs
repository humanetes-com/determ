//! Typed library error (CODE_ARCHITECTURE.md §3: thiserror at the lib edge, anyhow only in
//! the binary). Every `pub fn` in the crate returns `Result<T> = Result<T, ExtractError>`, so
//! a *library* consumer gets a matchable error with the file path attached — not an opaque
//! `anyhow::Error`. The binary (`main.rs`) keeps `anyhow`; `?` converts `ExtractError` into
//! `anyhow::Error` for free because it implements `std::error::Error`.

use std::path::Path;

/// Errors the skeleton extractor can return across its public API.
#[derive(thiserror::Error, Debug)]
pub enum ExtractError {
    #[error("reading {path}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("writing {path}")]
    Write {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("parsing {path} as Rust source")]
    Parse {
        path: String,
        #[source]
        source: syn::Error,
    },
}

impl ExtractError {
    pub(crate) fn read(path: &Path, source: std::io::Error) -> Self {
        Self::Read {
            path: path.display().to_string(),
            source,
        }
    }
    pub(crate) fn write(path: &Path, source: std::io::Error) -> Self {
        Self::Write {
            path: path.display().to_string(),
            source,
        }
    }
    pub(crate) fn parse(path: &Path, source: syn::Error) -> Self {
        Self::Parse {
            path: path.display().to_string(),
            source,
        }
    }
    /// Parse error keyed by a non-path label (e.g. a cluster name).
    pub(crate) fn parse_named(label: impl Into<String>, source: syn::Error) -> Self {
        Self::Parse {
            path: label.into(),
            source,
        }
    }
}

/// Crate-wide result alias — the library edge type.
pub type Result<T> = std::result::Result<T, ExtractError>;
