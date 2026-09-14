use std::{
    ffi::OsString,
    os::unix::{fs::PermissionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::Command,
};

pub fn candidates(current: &Path, path: &std::ffi::OsStr) -> Vec<PathBuf> {
    let current = current.canonicalize().unwrap_or_else(|_| current.into());
    let mut found = vec![];
    for dir in std::env::split_paths(path) {
        let candidate = dir.join("runtop");
        if let Ok(real) = candidate.canonicalize() {
            if real == current || found.contains(&real) {
                continue;
            }
            if real
                .metadata()
                .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
            {
                found.push(real);
            }
        }
    }
    found
}
pub fn handoff() -> anyhow::Result<()> {
    let argv: Vec<OsString> = std::env::args_os().collect();
    let invoked = argv.first().map(Path::new).and_then(Path::file_name);
    if invoked == Some(std::ffi::OsStr::new("rt"))
        || argv.iter().any(|s| s == "--lite")
        || std::env::var("RUNTOP_EDITION").as_deref() == Ok("lite")
    {
        return Ok(());
    }
    let full = std::env::var("RUNTOP_EDITION").as_deref() == Ok("full");
    if std::env::var_os("RUNTOP_HANDOFF").is_some() {
        anyhow::ensure!(!full, "full edition is not available on PATH");
        return Ok(());
    }
    let current = std::env::current_exe()?;
    for candidate in candidates(&current, &std::env::var_os("PATH").unwrap_or_default()) {
        // A guarded probe skips other lite copies and recognizes the full entry point.
        // It also avoids handing full-only arguments to an unrelated executable.
        if is_full(&candidate) {
            let _error = Command::new(candidate)
                .args(&argv[1..])
                .env("RUNTOP_HANDOFF", "1")
                .exec();
        }
    }
    anyhow::ensure!(
        !full,
        "full edition is not installed; install the full package and retry"
    );
    Ok(())
}

fn is_full(candidate: &Path) -> bool {
    use std::{
        io::Read,
        os::{fd::OwnedFd, unix::net::UnixStream},
        process::Stdio,
        time::{Duration, Instant},
    };
    let Ok((mut reader, writer)) = UnixStream::pair() else {
        return false;
    };
    if reader.set_nonblocking(true).is_err() {
        return false;
    }
    let Ok(mut child) = Command::new(candidate)
        .arg("--edition")
        .env("RUNTOP_HANDOFF", "1")
        .env_remove("RUNTOP_EDITION")
        .stdin(Stdio::null())
        .stdout(Stdio::from(OwnedFd::from(writer)))
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let started = Instant::now();
    let mut bytes = Vec::with_capacity(16);
    let mut buf = [0; 16];
    loop {
        while let Ok(n) = reader.read(&mut buf) {
            if n == 0 {
                break;
            }
            bytes.extend_from_slice(&buf[..n]);
            if bytes.len() > 16 {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                while let Ok(n) = reader.read(&mut buf) {
                    if n == 0 {
                        break;
                    }
                    bytes.extend_from_slice(&buf[..n]);
                    if bytes.len() > 16 {
                        return false;
                    }
                }
                return status.success() && bytes == b"full\n";
            }
            Ok(None) if started.elapsed() < Duration::from_secs(1) => {
                std::thread::sleep(Duration::from_millis(1))
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}
