// Binary entry point for the skeleton extractor.
//
// Usage:
//   skeleton-extractor --input <pre-strip dir> --output <skeleton dir> [--report]
//
// Walks the input directory for .rs files, emits skeletonised copies
// under the output directory mirroring the relative path. Optionally
// prints a JSON report of per-file stats.

use anyhow::{bail, Context as _, Result};
use std::path::PathBuf;

fn main() -> Result<()> {
    let mut input: Option<PathBuf> = None;
    let mut output: Option<PathBuf> = None;
    let mut report_json = false;
    let mut cfg_set = skeleton_extractor::CfgSet::new();

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--input" | "-i" => {
                input = Some(PathBuf::from(args.next().context("--input requires a value")?));
            }
            "--output" | "-o" => {
                output = Some(PathBuf::from(args.next().context("--output requires a value")?));
            }
            "--report" => {
                report_json = true;
            }
            "--cfg-true" => {
                cfg_set.set_true(args.next().context("--cfg-true requires a predicate")?);
            }
            "--cfg-false" => {
                cfg_set.set_false(args.next().context("--cfg-false requires a predicate")?);
            }
            "-h" | "--help" => {
                eprintln!(
                    "Usage: skeleton-extractor --input <dir> --output <dir> [--cfg-true PRED]... [--cfg-false PRED]... [--report]\n\
                     \n\
                     Walks <input> for .rs files, writes skeletonised versions under <output>.\n\
                     --cfg-true / --cfg-false (repeatable) set cluster cfg predicates:\n\
                       --cfg-true 'feature = \"std\"'\n\
                       --cfg-false nightly\n\
                     Items whose `#[cfg(...)]` evaluates to false are dropped.\n\
                     With --report, prints a JSON array of per-file stats to stdout."
                );
                return Ok(());
            }
            other => {
                bail!("unrecognised argument: {other}");
            }
        }
    }

    let input = input.context("--input is required")?;
    let output = output.context("--output is required")?;

    if !input.is_dir() {
        bail!("input path is not a directory: {}", input.display());
    }
    std::fs::create_dir_all(&output)
        .with_context(|| format!("could not create output directory {}", output.display()))?;

    let reports = skeleton_extractor::extract_tree_with_cfg(&input, &output, &cfg_set)?;

    if report_json {
        let json = serde_json::to_string_pretty(&reports)?;
        println!("{json}");
    } else {
        eprintln!(
            "skeleton-extractor: {} files processed, {} bytes in, {} bytes out",
            reports.len(),
            reports.iter().map(|r| r.bytes_in).sum::<usize>(),
            reports.iter().map(|r| r.bytes_out).sum::<usize>(),
        );
    }
    Ok(())
}
