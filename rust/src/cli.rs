use crate::{
    backend::{Backend, discover},
    model::{Document, Snapshot, human},
};
use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use std::{path::PathBuf, time::Duration};

#[derive(Parser, Clone, Debug)]
#[command(
    name = "runtop",
    about = "A compact terminal workspace for containers and their machines",
    disable_version_flag = true
)]
pub struct Args {
    /// Print the package version and edition.
    #[arg(long, global = true)]
    pub version: bool,
    /// Print the selected edition.
    #[arg(long, global = true)]
    pub edition: bool,
    /// Keep the lite launcher in this edition.
    #[arg(long, global = true)]
    pub lite: bool,
    /// Use bundled synthetic data without contacting engines.
    #[arg(long, global = true)]
    pub demo: bool,
    /// Read a saved snapshot instead of live engines.
    #[arg(long, global = true)]
    pub snapshot: Option<PathBuf>,
    /// Select a target by key or name.
    #[arg(long, short = 't', global = true)]
    pub target: Option<String>,
    /// Skip context discovery in live mode.
    #[arg(long, global = true)]
    pub no_contexts: bool,
    /// Disable all local mutations in this session.
    #[arg(long, global = true)]
    pub read_only: bool,
    /// Disable terminal mouse capture.
    #[arg(long)]
    pub no_mouse: bool,
    /// Diagnose live discovery and connectivity.
    #[arg(long)]
    pub doctor: bool,
    /// Print one target as normalized snapshot JSON and exit.
    #[arg(long, value_name = "KEY")]
    pub dump: Option<String>,
    /// Script TUI keys, including wait:N delays; requires a terminal.
    #[arg(long)]
    pub keys: Option<String>,
    /// Exit the workspace after this many seconds.
    #[arg(long,value_parser=positive_seconds)]
    pub quit_after: Option<f64>,
    #[command(subcommand)]
    pub command: Option<Sub>,
}
fn positive_seconds(s: &str) -> std::result::Result<f64, String> {
    let n = s.parse::<f64>().map_err(|e| e.to_string())?;
    if n.is_finite() && n > 0.0 {
        Ok(n)
    } else {
        Err("must be a positive finite duration".into())
    }
}
#[derive(Subcommand, Clone, Debug)]
pub enum Sub {
    /// Read the bundled offline manual; --json is intended for agents.
    Docs {
        #[arg(default_value = "all")]
        topic: String,
        #[arg(long)]
        list: bool,
        #[arg(long)]
        json: bool,
    },
    /// Print a container or image listing.
    Ps {
        /// Include non-running containers.
        #[arg(short = 'a', long)]
        all: bool,
        /// Include remote targets in the listing.
        #[arg(long)]
        contexts: bool,
        /// Print the image table; JSON retains the snapshot schema.
        #[arg(long)]
        images: bool,
        /// Request local CPU and memory samples.
        #[arg(long)]
        stats: bool,
        /// Write snapshot JSON to standard output.
        #[arg(long)]
        json: bool,
    },
    /// Diagnose discovery and connectivity.
    Doctor,
    /// Archive commands are available in the full edition.
    Logs {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
}
impl Args {
    pub fn fixture(&self) -> Result<Option<Document>> {
        if let Some(path) = &self.snapshot {
            Ok(Some(Document::parse(
                &std::fs::read_to_string(path).context("cannot read snapshot")?,
            )?))
        } else if self.demo {
            Ok(Some(Document::parse(include_str!(
                "../../spec/fixtures/snapshots/demo.json"
            ))?))
        } else {
            Ok(None)
        }
    }
}
pub async fn run(args: Args) -> Result<i32> {
    if args.version {
        println!("runtop {} (lite)", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    if args.edition {
        println!("lite");
        return Ok(0);
    }
    if let Some(Sub::Docs { topic, list, json }) = &args.command {
        return Ok(crate::manual::run(topic, *list, *json));
    }
    if matches!(args.command, Some(Sub::Logs { .. })) {
        bail!("archive commands require the full edition");
    }
    if args.doctor || matches!(args.command, Some(Sub::Doctor)) {
        return doctor(&args).await;
    }
    if args.dump.is_some() || matches!(args.command, Some(Sub::Ps { .. })) {
        return listing(&args).await;
    }
    crate::ui::run(args).await?;
    Ok(0)
}
async fn listing(args: &Args) -> Result<i32> {
    let (all, contexts, images, with_stats, json) = match args.command {
        Some(Sub::Ps {
            all,
            contexts,
            images,
            stats,
            json,
        }) => (all, contexts, images, stats, json),
        _ => (true, true, false, true, true),
    };
    let fixture = args.fixture()?;
    let (mut targets, errors) = if let Some(d) = &fixture {
        (d.targets.clone(), vec![])
    } else {
        discover(!args.no_contexts).await
    };
    for e in &errors {
        eprintln!("{e}");
    }
    let selected = args.dump.as_ref().or(args.target.as_ref());
    if let Some(key) = selected {
        targets.retain(|t| &t.key == key || &t.name == key);
        if targets.is_empty() {
            eprintln!("unknown target: {key}");
            return Ok(2);
        }
    } else if !contexts {
        targets.retain(|t| !t.remote());
    }
    if targets.is_empty() {
        if json {
            println!("{}", serde_json::to_string(&Document::new(vec![]))?);
        }
        return Ok(1);
    }
    let backend = Backend::default();
    let mut snapshots = vec![];
    let mut failed = !errors.is_empty();
    for t in targets {
        let mut s = if fixture.is_some() {
            t
        } else {
            if with_stats && !t.remote() {
                let first = backend.containers(&t).await;
                backend.fill_stats(&first).await;
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            backend.fetch(&t, with_stats).await
        };
        if s.state == "unreachable" || s.images_error.is_some() {
            failed = true;
            eprintln!(
                "{}: {}",
                s.key,
                s.error
                    .as_deref()
                    .or(s.images_error.as_deref())
                    .unwrap_or("unavailable")
            );
        }
        if !all && args.dump.is_none() {
            s.containers.retain(|c| c.state == "running");
        }
        snapshots.push(s);
    }
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&Document::new(snapshots))?
        );
    } else {
        if images {
            println!("TARGET\tIMAGE\tID\tSIZE");
        } else {
            println!(
                "TARGET\tNAME\tID\tIMAGE\tSTATUS\tPORTS{}",
                if with_stats { "\tCPU\tMEM" } else { "" }
            );
        }
        for s in snapshots {
            if images {
                for i in s.images {
                    println!(
                        "{}\t{}\t{}\t{}",
                        s.key,
                        i.reference,
                        i.id,
                        human(i.size_bytes)
                    );
                }
            } else {
                for c in s.containers {
                    let metrics = if with_stats {
                        format!(
                            "\t{}\t{}",
                            c.stats
                                .as_ref()
                                .and_then(|s| s.cpu_percent)
                                .map(|n| format!("{n:.1}%"))
                                .unwrap_or("—".into()),
                            c.stats
                                .as_ref()
                                .map(|s| human(s.mem_bytes))
                                .unwrap_or("—".into())
                        )
                    } else {
                        String::new()
                    };
                    println!(
                        "{}\t{}\t{}\t{}\t{}\t{}{}",
                        s.key,
                        c.name,
                        c.id,
                        c.image,
                        c.status,
                        c.ports.join(","),
                        metrics
                    );
                }
            }
        }
    }
    Ok(if failed { 3 } else { 0 })
}
async fn doctor(args: &Args) -> Result<i32> {
    let (targets, errors) = discover(!args.no_contexts).await;
    let backend = Backend::default();
    let mut failed = !errors.is_empty();
    for e in errors {
        println!("discovery: {e}");
    }
    if targets.is_empty() {
        println!("No targets. Configure a local socket or an existing machine.");
        return Ok(1);
    }
    for t in targets {
        let s = backend.containers(&t).await;
        println!(
            "{}: {}{}",
            s.key,
            s.state,
            s.error
                .as_ref()
                .map(|e| format!(" — {e}"))
                .unwrap_or_default()
        );
        failed |= s.state == "unreachable";
    }
    Ok(i32::from(failed))
}
pub fn selected_index(targets: &[Snapshot], preferred: Option<&str>) -> usize {
    preferred
        .and_then(|p| targets.iter().position(|t| t.key == p || t.name == p))
        .unwrap_or(0)
}
