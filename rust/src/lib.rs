pub mod backend;
pub mod cli;
pub mod handoff;
pub mod logs;
pub mod manual;
pub mod model;
pub mod navigation;
pub mod preferences;
pub mod ui;
use clap::Parser;
pub fn main_entry() -> i32 {
    if let Err(e) = handoff::handoff() {
        eprintln!("runtop: {e}");
        return 1;
    }
    let args = cli::Args::parse();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("async runtime");
    match runtime.block_on(cli::run(args)) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("runtop: {e:#}");
            1
        }
    }
}
