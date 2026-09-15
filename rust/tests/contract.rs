use runtop::{
    backend::{self, Backend},
    logs::Decoder,
    model::*,
    ui::App,
};
use serde_json::{Value, json};
use std::{fs, path::PathBuf};
fn fixture(name: &str) -> Value {
    serde_json::from_slice(&fs::read(root().join("spec/fixtures").join(name)).unwrap()).unwrap()
}
fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}
fn normalized_containers(mut cs: Vec<Container>) -> Value {
    cs.sort_by(|a, b| (&a.project, &a.name).cmp(&(&b.project, &b.name)));
    let mut v = serde_json::to_value(cs).unwrap();
    for c in v.as_array_mut().unwrap() {
        c.as_object_mut().unwrap().remove("stats");
    }
    v
}
#[test]
fn local_container_contract() {
    assert_eq!(
        normalized_containers(
            array(&fixture("engine/containers_json.json"))
                .iter()
                .map(normalize_container)
                .collect()
        ),
        fixture("expected/containers.json")
    );
}
#[test]
fn local_image_contract() {
    let mut images: Vec<_> = array(&fixture("engine/images_json.json"))
        .iter()
        .map(normalize_image)
        .collect();
    images.sort_by_key(|i| std::cmp::Reverse(i.size_bytes));
    assert_eq!(
        serde_json::to_value(images).unwrap(),
        fixture("expected/images.json")
    );
}
#[test]
fn remote_container_contract() {
    let raw = fs::read(root().join("spec/fixtures/cli/docker_ps.jsonl")).unwrap();
    assert_eq!(
        normalized_containers(
            backend::json_lines(&raw)
                .unwrap()
                .iter()
                .map(normalize_remote_container)
                .collect()
        ),
        fixture("expected/remote_containers.json")
    );
}
#[test]
fn remote_image_contract() {
    let raw = fs::read(root().join("spec/fixtures/cli/docker_images.jsonl")).unwrap();
    let mut images: Vec<_> = backend::json_lines(&raw)
        .unwrap()
        .iter()
        .map(normalize_remote_image)
        .collect();
    images.sort_by_key(|i| std::cmp::Reverse(i.size_bytes));
    assert_eq!(
        serde_json::to_value(images).unwrap(),
        fixture("expected/remote_images.json")
    );
}
#[test]
fn parser_contract() {
    let exp = fixture("expected/parsers.json");
    for (input, expected) in exp["remote_ports"].as_object().unwrap() {
        assert_eq!(json!(remote_ports(input)), *expected);
    }
    for (input, expected) in exp["docker_size"].as_object().unwrap() {
        assert_eq!(json!(parse_size(input)), *expected);
    }
}
#[test]
fn stats_contract() {
    let a = fixture("engine/stats_oneshot_1.json");
    let b = fixture("engine/stats_oneshot_2.json");
    let c = fixture("engine/stats_streamfalse.json");
    let exp = fixture("expected/stats.json");
    assert_eq!(json!(stats(&a, None)), exp["oneshot_first"]);
    assert_eq!(
        json!(stats(&b, Some(&a["cpu_stats"]))),
        exp["oneshot_second_vs_first"]
    );
    assert_eq!(json!(stats(&c, None)), exp["stream_false"]);
    assert_eq!(stats(&c, Some(&a["cpu_stats"])), stats(&c, None));
}
#[test]
fn stats_counter_reset_and_cache() {
    let prev = json!({"cpu_usage":{"total_usage":100},"system_cpu_usage":0});
    let s = json!({"cpu_stats":{"cpu_usage":{"total_usage":200,"percpu_usage":[]},"system_cpu_usage":1000,"online_cpus":4},"memory_stats":{"usage":10,"stats":{"inactive_file":30},"limit":9}});
    let got = stats(&s, Some(&prev));
    assert_eq!(got.cpu_percent, Some(40.0));
    assert_eq!(got.mem_bytes, 0);
    assert_eq!(
        stats(
            &s,
            Some(&json!({"cpu_usage":{"total_usage":300},"system_cpu_usage":0}))
        )
        .cpu_percent,
        None
    );
}
#[test]
fn image_fallbacks() {
    let mut v = json!({"Id":"sha256:abcdef","Containers":-1,"RepoTags":["a:1@sha256:dead"]});
    assert_eq!(normalize_image(&v).reference, "a:1");
    assert_eq!(normalize_image(&v).containers, None);
    v["RepoTags"] = json!([]);
    v["RepoDigests"] = json!(["repo@sha256:1234567890129999"]);
    let i = normalize_image(&v);
    assert_eq!(i.reference, "repo@123456789012");
    assert!(i.dangling);
}
#[test]
fn logs_arbitrary_chunks() {
    let exp = fixture("expected/logs.json");
    for size in [1, 3, 7, 13, 4096] {
        for (tty, file, key) in [
            (false, "logs_mux.bin", "mux"),
            (true, "logs_tty.bin", "tty"),
        ] {
            let bytes = fs::read(root().join("spec/fixtures/engine").join(file)).unwrap();
            let mut d = Decoder::new(tty);
            let mut out = vec![];
            for chunk in bytes.chunks(size) {
                out.extend(d.feed(chunk).unwrap());
            }
            out.extend(d.finish());
            if tty {
                assert_eq!(
                    json!(out.iter().map(|l| &l.text).collect::<Vec<_>>()),
                    exp[key]
                );
            } else {
                assert_eq!(json!(out), exp[key]);
            }
        }
    }
}
#[test]
fn huge_unterminated_logs_are_bounded() {
    let mut d = Decoder::new(true);
    let out = d.feed(&vec![b'x'; 2_000_000]).unwrap();
    assert!(out.len() > 20);
    assert!(out.iter().all(|l| l.text.len() < 65600));
    assert!(d.finish()[0].text.len() < 65536);
}
#[test]
fn log_controls_and_blank_lines() {
    let mut d = Decoder::new(true);
    let mut lines = d.feed(b"one\n\ntwo\r\nlast").unwrap();
    lines.extend(d.finish());
    assert_eq!(
        lines.iter().map(|l| l.text.as_str()).collect::<Vec<_>>(),
        vec!["one", "", "two", "last"]
    );
    assert_eq!(
        runtop::logs::clean("hi\x1b]52;c;secret\x07\x1b[31mred\x1b[0m"),
        "hired"
    );
    assert!(Decoder::new(false).feed(&[4, 0, 0, 0, 0, 0, 0, 1]).is_err());
}
#[test]
fn snapshot_roundtrip_and_remote_safety() {
    let mut d = Document::parse(
        &fs::read_to_string(root().join("spec/fixtures/snapshots/demo.json")).unwrap(),
    )
    .unwrap();
    for t in &mut d.targets {
        if t.remote() {
            t.read_only = false;
        }
    }
    let parsed = Document::parse(&serde_json::to_string(&d).unwrap()).unwrap();
    assert!(
        parsed
            .targets
            .iter()
            .filter(|t| t.remote())
            .all(|t| t.read_only)
    );
    assert!(Document::parse(r#"{"schema":"unknown","generated_at":"","targets":[]}"#).is_err());
}
#[test]
fn discovery_parsers() {
    let v = backend::json_lines(
        &fs::read(root().join("spec/fixtures/cli/limactl_list.jsonl")).unwrap(),
    )
    .unwrap();
    let t = backend::lima_target(&v[0]);
    assert_eq!(t.key, "lima:docker");
    assert_eq!(t.vm.unwrap().cpus, 4);
    assert!(backend::colima_target(&json!({"name":"../bad","runtime":"docker"}), "/tmp").is_none());
    assert!(
        backend::colima_target(&json!({"name":"test","runtime":"containerd"}), "/tmp").is_none()
    );
}
#[tokio::test]
async fn remote_mutations_refused_before_io() {
    let t = Snapshot {
        kind: "context".into(),
        state: "ok".into(),
        ..Default::default()
    };
    for verb in ["rm", "stop", "restart", "exec", "system", ""] {
        assert!(backend::remote(&t, verb, &[]).await.is_err());
    }
    assert!(
        Backend::default()
            .act(&t, "start", &["abc".into()])
            .await
            .is_err()
    );
}
#[tokio::test]
async fn stale_and_readonly_refuse_actions() {
    let b = Backend::default();
    for (stale, read_only) in [(true, false), (false, true)] {
        let t = Snapshot {
            kind: "host".into(),
            state: "ok".into(),
            stale,
            read_only,
            ..Default::default()
        };
        assert!(b.act(&t, "remove", &["abc".into()]).await.is_err());
    }
}
#[test]
fn storage_unknown_not_zero() {
    let text = backend::storage_text(&fixture("engine/disk_usage.json"));
    assert!(text.contains("Volumes: unknown"));
    assert!(text.contains("Container writable layers: 30B"));
    assert!(text.contains("not additive"));
}
#[test]
fn environment_masked() {
    let v = backend::masked_inspect(json!({"Config":{"Env":["TOKEN=secret","EMPTY="]}}));
    assert!(!v.to_string().contains("secret"));
    assert!(v.to_string().contains("TOKEN="));
}
#[test]
fn render_goldens() {
    let d = Document::parse(
        &fs::read_to_string(root().join("spec/fixtures/snapshots/demo.json")).unwrap(),
    )
    .unwrap();
    for width in [100u16, 60, 40] {
        let mut app = App::new(d.targets.clone(), 0, false);
        let backend = ratatui::backend::TestBackend::new(width, 30);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let buf = terminal.backend().buffer();
        let mut text = String::new();
        for y in 0..30 {
            for x in 0..width {
                text.push_str(buf[(x, y)].symbol());
            }
            text.push('\n');
        }
        let path = root().join(format!("rust/tests/golden/glance-{width}.txt"));
        if std::env::var_os("RUNTOP_UPDATE_GOLDENS").is_some() {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, &text).unwrap();
        }
        assert_eq!(fs::read_to_string(path).unwrap(), text);
    }
}
#[test]
fn command_edition_and_dump() {
    let bin = env!("CARGO_BIN_EXE_rt");
    let out = std::process::Command::new(bin)
        .arg("--edition")
        .output()
        .unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout, b"lite\n");
    let out = std::process::Command::new(bin)
        .args(["--demo", "--dump", "lima:docker"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let d: Document = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(d.targets.len(), 1);
    assert!(!d.targets[0].containers.is_empty());
}

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let p = std::env::temp_dir().join(format!(
            "runtop-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        fs::create_dir_all(&p).unwrap();
        Self(p)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
#[test]
fn edition_dispatch_and_broken_candidates() {
    use std::os::unix::fs::PermissionsExt;
    let dir = Scratch::new();
    let full = dir.0.join("full");
    let broken = dir.0.join("broken");
    fs::create_dir_all(&full).unwrap();
    fs::create_dir_all(&broken).unwrap();
    let executable = full.join("runtop");
    fs::write(&executable, "#!/bin/sh\nprintf 'full\\n'\n").unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    std::os::unix::fs::symlink("/nonexistent", broken.join("runtop")).unwrap();
    let path = std::env::join_paths([&broken, &full]).unwrap();
    let bin = env!("CARGO_BIN_EXE_runtop");
    let run = |extra: &[&str], edition: Option<&str>| {
        let mut c = std::process::Command::new(bin);
        c.env("PATH", &path)
            .env_remove("RUNTOP_HANDOFF")
            .env_remove("RUNTOP_EDITION")
            .args(extra);
        if let Some(v) = edition {
            c.env("RUNTOP_EDITION", v);
        }
        c.output().unwrap()
    };
    assert_eq!(run(&["--edition"], None).stdout, b"full\n");
    assert_eq!(run(&["--edition", "--lite"], None).stdout, b"lite\n");
    assert_eq!(run(&["--edition"], Some("lite")).stdout, b"lite\n");
    assert_eq!(
        std::process::Command::new(env!("CARGO_BIN_EXE_rt"))
            .env("PATH", &path)
            .env("RUNTOP_EDITION", "full")
            .arg("--edition")
            .output()
            .unwrap()
            .stdout,
        b"lite\n"
    );
    assert!(
        !std::process::Command::new(bin)
            .env("PATH", &broken)
            .env("RUNTOP_EDITION", "full")
            .arg("--edition")
            .output()
            .unwrap()
            .status
            .success()
    );
    assert_eq!(
        std::process::Command::new(bin)
            .env("PATH", &path)
            .env("RUNTOP_HANDOFF", "1")
            .env_remove("RUNTOP_EDITION")
            .arg("--edition")
            .output()
            .unwrap()
            .stdout,
        b"lite\n"
    );
}
#[test]
fn dispatch_skips_another_lite_copy() {
    let dir = Scratch::new();
    fs::copy(env!("CARGO_BIN_EXE_runtop"), dir.0.join("runtop")).unwrap();
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_runtop"))
        .env("PATH", &dir.0)
        .env_remove("RUNTOP_EDITION")
        .env_remove("RUNTOP_HANDOFF")
        .arg("--edition")
        .output()
        .unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout, b"lite\n");
}

async fn fake_engine(
    images_fail: bool,
) -> (
    Scratch,
    Snapshot,
    tokio::task::JoinHandle<()>,
    std::sync::Arc<std::sync::Mutex<Vec<String>>>,
) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let dir = Scratch::new();
    let path = dir.0.join("engine.sock");
    let listener = tokio::net::UnixListener::bind(&path).unwrap();
    let requests = std::sync::Arc::new(std::sync::Mutex::new(vec![]));
    let log = requests.clone();
    let task = tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                break;
            };
            let log = log.clone();
            tokio::spawn(async move {
                let mut raw = vec![];
                let mut buf = [0; 4096];
                loop {
                    let n = socket.read(&mut buf).await.unwrap();
                    if n == 0 {
                        return;
                    }
                    raw.extend_from_slice(&buf[..n]);
                    if raw.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                    if raw.len() > 16384 {
                        return;
                    }
                }
                let first = String::from_utf8_lossy(&raw)
                    .lines()
                    .next()
                    .unwrap()
                    .to_string();
                log.lock().unwrap().push(first.clone());
                let path = first.split_whitespace().nth(1).unwrap();
                let (status, body) = if path == "/version" {
                    ("200 OK", json!({"ApiVersion":"1.45"}).to_string())
                } else if path.contains("/containers/json") {
                    ("200 OK", fixture("engine/containers_json.json").to_string())
                } else if path.contains("/images/json") {
                    if images_fail {
                        ("500 Error", "failure".into())
                    } else {
                        ("200 OK", fixture("engine/images_json.json").to_string())
                    }
                } else if path.contains("/stats?") {
                    (
                        "200 OK",
                        fixture("engine/stats_streamfalse.json").to_string(),
                    )
                } else if path.contains("/start") {
                    ("204 No Content", String::new())
                } else {
                    ("404 Missing", "missing".into())
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = socket.write_all(response.as_bytes()).await;
            });
        }
    });
    let t = Snapshot {
        key: "host".into(),
        name: "fixture".into(),
        kind: "host".into(),
        endpoint: format!("unix://{}", path.display()),
        state: "ok".into(),
        ..Default::default()
    };
    (dir, t, task, requests)
}
#[tokio::test]
async fn socket_transport_negotiates_version_and_retains_partial_data() {
    let (_dir, t, server, requests) = fake_engine(true).await;
    let s = Backend::default().fetch(&t, true).await;
    assert_eq!(s.state, "ok");
    assert!(!s.containers.is_empty());
    assert!(s.images_error.is_some());
    let calls = requests.lock().unwrap();
    assert_eq!(
        calls.iter().filter(|s| s.contains("GET /version ")).count(),
        1
    );
    assert!(
        calls
            .iter()
            .filter(|s| !s.contains("GET /version "))
            .all(|s| s.contains("/v1.45/"))
    );
    server.abort();
}
#[tokio::test]
async fn socket_action_binds_id_and_rejects_path_injection() {
    let (_dir, t, server, requests) = fake_engine(false).await;
    let b = Backend::default();
    let result = b.act(&t, "start", &["abcdef012345".into()]).await.unwrap();
    assert!(result.contains("1/1 succeeded"));
    assert!(
        requests
            .lock()
            .unwrap()
            .iter()
            .any(|s| s.starts_with("POST /v1.45/containers/abcdef012345/start?t=10 "))
    );
    let n = requests.lock().unwrap().len();
    assert!(
        b.act(&t, "start", &["../images/prune".into()])
            .await
            .is_err()
    );
    assert_eq!(requests.lock().unwrap().len(), n);
    server.abort();
}
#[tokio::test]
async fn missing_socket_is_not_empty_success() {
    let t = Snapshot {
        endpoint: "unix:///nonexistent/runtop-test.sock".into(),
        ..Default::default()
    };
    let s = Backend::default().containers(&t).await;
    assert_eq!(s.state, "no_docker_socket");
}

#[test]
fn offline_manual_matches_shared_bundle() {
    let bin = env!("CARGO_BIN_EXE_rt");
    let out = std::process::Command::new(bin)
        .args(["docs", "--json"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let result: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let expected: serde_json::Value =
        serde_json::from_str(include_str!("../../spec/manual.json")).unwrap();
    assert_eq!(result["topics"], expected["topics"]);
    assert_eq!(result["edition"], "lite");
    assert_eq!(result["schema"], "runtop.docs/v1");
    let out = std::process::Command::new(bin)
        .args(["docs", "--list", "--json"])
        .output()
        .unwrap();
    let result: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert!(
        result["topics"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t.get("content").is_none())
    );
    let out = std::process::Command::new(bin)
        .args(["docs", "missing-topic"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
}
