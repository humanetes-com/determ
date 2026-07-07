use std::env;
use std::path::Path;
use std::process::{Command, Stdio};

// Mirrors anyhow's real build.rs probe technique exactly (see
// `git show master:build.rs` / `src/nightly.rs` in tmp/anyhow): actually try to
// COMPILE a tiny snippet using the unstable `error_generic_member_access` API
// under the current toolchain, rather than guessing from a version string.
// Success -> emit a custom cfg; `src/lib.rs` gates `feature(...)` on that same
// cfg, and `mod nightly_provide;` is gated on it too.
fn main() {
    println!("cargo:rustc-check-cfg=cfg(hcm_nightly)");
    println!("cargo:rerun-if-changed=build.rs");
    if probe_supports_provide_api() {
        println!("cargo:rustc-cfg=hcm_nightly");
    }
}

const PROBE_SRC: &str = r#"
#![feature(error_generic_member_access)]
use core::error::{Error, Request};
use core::fmt;
#[derive(Debug)]
struct X;
impl fmt::Display for X {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "X") }
}
impl Error for X {
    fn provide<'a>(&'a self, request: &mut Request<'a>) {
        request.provide_ref::<u32>(&1u32);
    }
}
"#;

fn probe_supports_provide_api() -> bool {
    let rustc = env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
    let out_dir = match env::var_os("OUT_DIR") {
        Some(d) => d,
        None => return false,
    };
    let probe_dir = Path::new(&out_dir).join("hcm_probe");
    if std::fs::create_dir_all(&probe_dir).is_err() {
        return false;
    }
    let probe_src = probe_dir.join("probe.rs");
    if std::fs::write(&probe_src, PROBE_SRC).is_err() {
        return false;
    }

    let status = Command::new(rustc)
        .arg("--edition=2021")
        .arg("--crate-type=lib")
        .arg("--cap-lints=allow")
        .arg("--emit=metadata")
        .arg("--out-dir")
        .arg(&probe_dir)
        .arg(&probe_src)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    let _ = std::fs::remove_dir_all(&probe_dir);
    matches!(status, Ok(s) if s.success())
}
