// Binary entry point for the skeleton extractor.
//
// CLI is clap-derive (slice 7); the flag NAMES are part of the Python↔Rust seam
// (CODE_ARCHITECTURE.md §4 — `pipeline_core.adapters.extractor` shells these exact strings),
// so they are preserved byte-for-byte from the old hand-rolled parser. anyhow stays in the
// binary; the library edge uses anyhow::Result too (thiserror migration deferred).

use anyhow::{Context as _, Result, bail};
use clap::Parser;
use std::path::PathBuf;

/// Skeleton extractor — skeletonise a Rust tree, enumerate fns/types/mods/dispatch/feature-gates,
/// or merge per-cluster trees. Subcommand selection is by mode flag (exactly one is passed by the
/// pipeline); modes are checked in the order below.
#[derive(Parser, Debug)]
#[command(name = "skeleton-extractor", about, long_about = None)]
struct Cli {
    /// Pre-strip source tree to read (required for every mode except merge).
    #[arg(long, short)]
    input: Option<PathBuf>,

    /// Output dir for skeletonisation mode.
    #[arg(long, short)]
    output: Option<PathBuf>,

    /// Print a JSON per-file report (skeletonisation mode).
    #[arg(long)]
    report: bool,

    /// Emit JSON array of FnRecord to stdout.
    #[arg(long)]
    enumerate_fns: bool,

    /// Emit JSON array of TypeRecord to stdout.
    #[arg(long)]
    enumerate_types: bool,

    /// Emit JSON array of ModRecord to stdout.
    #[arg(long)]
    enumerate_mods: bool,

    /// Emit JSON array of ConstRecord (const/static items) to stdout.
    #[arg(long)]
    enumerate_consts: bool,

    /// Emit JSON array of DispatchFamily to stdout.
    #[arg(long = "enumerate-dispatch-families")]
    enumerate_dispatch: bool,

    /// Emit JSON array of nightly-feature-gating cfg predicates to stdout.
    #[arg(long = "enumerate-feature-gate-cfgs")]
    enumerate_feature_cfgs: bool,

    /// Emit JSON array of CfgOccurrence (`#[cfg(...)]` condition leaf atoms) to stdout.
    #[arg(long = "enumerate-cfgs")]
    enumerate_cfgs: bool,

    /// Merge per-cluster trees into one cfg-gated source file.
    #[arg(long)]
    merge_clusters: bool,

    /// Cluster spec `name:path:cfg_predicate` (repeatable; merge mode).
    #[arg(long = "cluster")]
    clusters: Vec<String>,

    /// Destination path for the merged file (merge mode).
    #[arg(long)]
    merge_output: Option<PathBuf>,

    /// cfg predicate that is TRUE under the active cluster (repeatable).
    #[arg(long = "cfg-true")]
    cfg_true: Vec<String>,

    /// cfg predicate that is FALSE under the active cluster (repeatable).
    #[arg(long = "cfg-false")]
    cfg_false: Vec<String>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    if cli.merge_clusters {
        let out_path = cli
            .merge_output
            .context("--merge-output is required for --merge-clusters")?;
        if cli.clusters.is_empty() {
            bail!("--merge-clusters requires at least one --cluster name:path:cfg");
        }
        let mut files: Vec<skeleton_extractor::ClusterFile> = Vec::new();
        for spec in cli.clusters {
            // Format: name:path:cfg_predicate (cfg may contain colons inside
            // strings — split on the first two colons only).
            let mut parts = spec.splitn(3, ':');
            let name = parts.next().context("--cluster missing name")?.to_string();
            let path_str = parts.next().context("--cluster missing path")?;
            let cfg_gate = parts.next().unwrap_or("").to_string();
            let path = PathBuf::from(path_str);
            let source = std::fs::read_to_string(&path)
                .with_context(|| format!("read cluster file {}", path.display()))?;
            files.push(skeleton_extractor::ClusterFile {
                name,
                source,
                cfg_gate,
            });
        }
        let merged = skeleton_extractor::merge_cluster_files(files)?;
        if let Some(parent) = out_path.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        std::fs::write(&out_path, merged)
            .with_context(|| format!("write merged file {}", out_path.display()))?;
        return Ok(());
    }

    let input = cli.input.context("--input is required")?;
    if !input.is_dir() {
        bail!("input path is not a directory: {}", input.display());
    }

    // Enumeration modes — each emits a JSON array to stdout and returns. The
    // pipeline passes exactly one; checked in priority order.
    if cli.enumerate_fns {
        let records = skeleton_extractor::enumerate_fns_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_types {
        let records = skeleton_extractor::enumerate_types_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_mods {
        let records = skeleton_extractor::enumerate_mods_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_consts {
        let records = skeleton_extractor::enumerate_consts_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_dispatch {
        let records = skeleton_extractor::enumerate_dispatch_families_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_feature_cfgs {
        let records = skeleton_extractor::enumerate_feature_gate_cfgs_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }
    if cli.enumerate_cfgs {
        let records = skeleton_extractor::enumerate_cfgs_in_tree(&input)?;
        println!("{}", serde_json::to_string(&records)?);
        return Ok(());
    }

    // Skeletonisation mode.
    let mut cfg_set = skeleton_extractor::CfgSet::new();
    for pred in cli.cfg_true {
        cfg_set.set_true(pred);
    }
    for pred in cli.cfg_false {
        cfg_set.set_false(pred);
    }

    let output = cli
        .output
        .context("--output is required (or use --enumerate-fns)")?;
    std::fs::create_dir_all(&output)
        .with_context(|| format!("could not create output directory {}", output.display()))?;

    let reports = skeleton_extractor::extract_tree_with_cfg(&input, &output, &cfg_set)?;

    if cli.report {
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
