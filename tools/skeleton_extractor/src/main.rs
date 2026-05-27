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
    let mut enumerate_fns = false;
    let mut enumerate_types = false;
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
            "--enumerate-fns" => {
                // No --output needed; emits a JSON array of FnRecord to stdout.
                enumerate_fns = true;
            }
            "--enumerate-types" => {
                // No --output needed; emits JSON array of TypeRecord to stdout.
                // (Mutually exclusive with --enumerate-fns; the later flag wins.)
                enumerate_fns = false;
                enumerate_types = true;
            }
            "--cfg-true" => {
                cfg_set.set_true(args.next().context("--cfg-true requires a predicate")?);
            }
            "--cfg-false" => {
                cfg_set.set_false(args.next().context("--cfg-false requires a predicate")?);
            }
            "-h" | "--help" => {
                eprintln!(
                    "Usage:\n\
                     \n\
                     skeleton-extractor --input <dir> --output <dir> [--cfg-true PRED]... [--cfg-false PRED]... [--report]\n\
                       Walks <input> for .rs files, writes skeletonised versions under <output>.\n\
                     \n\
                     skeleton-extractor --enumerate-fns --input <dir>\n\
                       Emits JSON to stdout: array of {{file, fn_name, sig_text, body_text,\n\
                       sig_byte_start, body_byte_start, body_byte_end, cfg_predicates, is_pub,\n\
                       container, has_unimplemented_body}}. One entry per fn item (free fn,\n\
                       impl method, trait method default). Used by the pipeline as a robust\n\
                       replacement for regex-based fn discovery."
                );
                return Ok(());
            }
            other => {
                bail!("unrecognised argument: {other}");
            }
        }
    }

    let input = input.context("--input is required")?;

    if !input.is_dir() {
        bail!("input path is not a directory: {}", input.display());
    }

    if enumerate_fns {
        let records = skeleton_extractor::enumerate_fns_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if enumerate_types {
        let records = skeleton_extractor::enumerate_types_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }

    let output = output.context("--output is required (or use --enumerate-fns)")?;
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
