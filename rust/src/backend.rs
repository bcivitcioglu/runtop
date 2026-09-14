use crate::model::*;
use anyhow::{Context, Result, bail, ensure};
use futures_util::{StreamExt, stream};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    path::Path,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::AsyncReadExt,
    process::Command,
    sync::{Mutex, OnceCell, mpsc},
    time::timeout,
};

const MAX_RESPONSE: usize = 32 * 1024 * 1024;
pub async fn command(program: &str, args: &[&str], seconds: u64) -> Result<Vec<u8>> {
    let mut cmd = Command::new(program);
    cmd.args(args)
        .kill_on_drop(true)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = cmd
        .spawn()
        .with_context(|| format!("cannot run {program}"))?;
    let mut stdout = child
        .stdout
        .take()
        .context("missing stdout")?
        .take(MAX_RESPONSE as u64 + 1);
    let mut stderr = child.stderr.take().context("missing stderr")?.take(65537);
    let result = timeout(Duration::from_secs(seconds), async {
        let mut out = vec![];
        let mut err = vec![];
        let (a, b) = tokio::join!(stdout.read_to_end(&mut out), stderr.read_to_end(&mut err));
        a?;
        b?;
        ensure!(
            out.len() <= MAX_RESPONSE && err.len() <= 65536,
            "command output exceeded limit"
        );
        let status = child.wait().await?;
        ensure!(status.success(), "{}", String::from_utf8_lossy(&err).trim());
        Ok(out)
    })
    .await;
    match result {
        Ok(r) => r,
        Err(_) => {
            let _ = child.kill().await;
            bail!("{program}: timed out")
        }
    }
}
pub async fn remote(target: &Snapshot, verb: &str, args: &[&str]) -> Result<Vec<u8>> {
    ensure!(
        matches!(
            verb,
            "ps" | "images" | "logs" | "inspect" | "stats" | "version"
        ),
        "remote operation is read-only"
    );
    let mut argv = vec!["--context", &target.name, verb];
    argv.extend_from_slice(args);
    command("docker", &argv, 15).await
}
pub fn json_lines(bytes: &[u8]) -> Result<Vec<Value>> {
    let text = std::str::from_utf8(bytes)?;
    if text.trim_start().starts_with('[') {
        return Ok(serde_json::from_str(text)?);
    }
    text.lines()
        .filter(|s| !s.trim().is_empty())
        .map(|s| Ok(serde_json::from_str(s)?))
        .collect()
}
#[derive(Clone)]
pub struct Engine {
    client: reqwest::Client,
    version: Arc<OnceCell<String>>,
}
impl Engine {
    pub fn new(socket: &str) -> Result<Self> {
        Ok(Self {
            client: reqwest::Client::builder()
                .unix_socket(socket)
                .connect_timeout(Duration::from_secs(2))
                .pool_max_idle_per_host(10)
                .build()?,
            version: Arc::new(OnceCell::new()),
        })
    }
    async fn response(
        &self,
        method: reqwest::Method,
        path: &str,
        seconds: u64,
    ) -> Result<reqwest::Response> {
        let url = if path == "/version" {
            format!("http://localhost{path}")
        } else {
            let version = self
                .version
                .get_or_try_init(|| async {
                    let r = self
                        .client
                        .get("http://localhost/version")
                        .timeout(Duration::from_secs(4))
                        .send()
                        .await?
                        .error_for_status()?;
                    let v: Value = serde_json::from_slice(&limited_bytes(r).await?)?;
                    let api = string(&v, "ApiVersion");
                    ensure!(
                        !api.is_empty() && api.chars().all(|c| c.is_ascii_digit() || c == '.'),
                        "invalid API version"
                    );
                    Ok::<_, anyhow::Error>(api)
                })
                .await?;
            format!("http://localhost/v{version}{path}")
        };
        let builder = self.client.request(method, url);
        let builder = if seconds > 0 {
            builder.timeout(Duration::from_secs(seconds))
        } else {
            builder
        };
        Ok(builder.send().await?.error_for_status()?)
    }
    pub async fn json(&self, path: &str) -> Result<Value> {
        self.json_method(reqwest::Method::GET, path, 8).await
    }
    pub async fn json_method(
        &self,
        method: reqwest::Method,
        path: &str,
        seconds: u64,
    ) -> Result<Value> {
        let bytes = limited_bytes(self.response(method, path, seconds).await?).await?;
        if bytes.is_empty() {
            Ok(Value::Null)
        } else {
            Ok(serde_json::from_slice(&bytes)?)
        }
    }
    pub async fn logs(
        &self,
        cid: &str,
        follow: bool,
        tx: mpsc::Sender<crate::logs::Line>,
        prefix: String,
    ) -> Result<()> {
        validate_id(cid)?;
        let inspect = self.json(&format!("/containers/{cid}/json")).await?;
        let tty = inspect["Config"]["Tty"].as_bool().unwrap_or(false);
        let path = format!(
            "/containers/{cid}/logs?stdout=1&stderr=1&tail=200&timestamps=1&follow={}",
            u8::from(follow)
        );
        let response = self
            .response(reqwest::Method::GET, &path, if follow { 0 } else { 8 })
            .await?;
        let mut stream = response.bytes_stream();
        let mut decoder = crate::logs::Decoder::new(tty);
        while let Some(bytes) = stream.next().await {
            for mut line in decoder.feed(&bytes?)? {
                line.prefix = prefix.clone();
                tx.send(line).await.context("log view closed")?;
            }
        }
        for mut line in decoder.finish() {
            line.prefix = prefix.clone();
            tx.send(line).await.context("log view closed")?;
        }
        Ok(())
    }
}
async fn limited_bytes(response: reqwest::Response) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        ensure!(
            out.len() + chunk.len() <= MAX_RESPONSE,
            "response exceeded limit"
        );
        out.extend_from_slice(&chunk);
    }
    Ok(out)
}
pub fn validate_id(id: &str) -> Result<()> {
    ensure!(
        !id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid container ID"
    );
    Ok(())
}
#[derive(Clone, Default)]
pub struct Backend {
    engines: Arc<Mutex<HashMap<String, Engine>>>,
    previous: Arc<Mutex<HashMap<String, Value>>>,
}
impl Backend {
    pub async fn engine(&self, target: &Snapshot) -> Result<Engine> {
        let socket = target.socket().context("no local socket")?;
        let mut engines = self.engines.lock().await;
        if let Some(e) = engines.get(socket) {
            return Ok(e.clone());
        }
        let e = Engine::new(socket)?;
        engines.insert(socket.into(), e.clone());
        Ok(e)
    }
    pub async fn containers(&self, t: &Snapshot) -> Snapshot {
        let mut s = t.clone();
        s.error = None;
        s.stale = false;
        s.containers.clear();
        s.images.clear();
        s.images_error = None;
        s.images_loaded = false;
        s.stats_sampled = false;
        if s.vm.as_ref().is_some_and(|v| v.status != "Running") {
            s.state = "vm_stopped".into();
            return s;
        }
        if !s.remote() && !s.socket().is_some_and(|p| Path::new(p).exists()) {
            s.state = "no_docker_socket".into();
            return s;
        }
        let result = async {
            if s.remote() {
                Ok(
                    json_lines(&remote(&s, "ps", &["-a", "--format", "json"]).await?)?
                        .iter()
                        .map(normalize_remote_container)
                        .collect::<Vec<_>>(),
                )
            } else {
                Ok::<_, anyhow::Error>(
                    array(
                        &self
                            .engine(&s)
                            .await?
                            .json("/containers/json?all=1")
                            .await?,
                    )
                    .iter()
                    .map(normalize_container)
                    .collect(),
                )
            }
        }
        .await;
        match result {
            Ok(mut containers) => {
                containers
                    .sort_unstable_by(|a, b| (&a.project, &a.name).cmp(&(&b.project, &b.name)));
                s.containers = containers;
                s.state = "ok".into();
            }
            Err(e) => {
                s.state = "unreachable".into();
                s.error = Some(e.to_string());
            }
        }
        s
    }
    pub async fn images(&self, t: &Snapshot) -> Result<Vec<Image>> {
        let mut images: Vec<_> = if t.remote() {
            json_lines(&remote(t, "images", &["--format", "json"]).await?)?
                .iter()
                .map(normalize_remote_image)
                .collect()
        } else {
            array(&self.engine(t).await?.json("/images/json").await?)
                .iter()
                .map(normalize_image)
                .collect()
        };
        images.sort_unstable_by_key(|i| std::cmp::Reverse(i.size_bytes));
        Ok(images)
    }
    pub async fn fill_stats(&self, t: &Snapshot) -> Vec<Container> {
        let mut containers = t.containers.clone();
        if t.remote() {
            return containers;
        }
        let Ok(engine) = self.engine(t).await else {
            return containers;
        };
        let mut previous = self.previous.lock().await;
        let prefix = format!("{}|", t.endpoint);
        let ids: HashSet<_> = containers
            .iter()
            .map(|c| format!("{prefix}{}", c.id))
            .collect();
        previous.retain(|k, _| !k.starts_with(&prefix) || ids.contains(k));
        let running: Vec<_> = containers
            .iter()
            .enumerate()
            .filter(|(_, c)| c.state == "running")
            .map(|(i, c)| (i, c.id.clone()))
            .collect();
        let mut pending = stream::iter(running.into_iter().map(|(i, id)| {
            let e = engine.clone();
            async move {
                (
                    i,
                    id.clone(),
                    e.json(&format!(
                        "/containers/{id}/stats?stream=false&one-shot=true"
                    ))
                    .await,
                )
            }
        }))
        .buffer_unordered(8);
        let mut samples = vec![];
        let _ = timeout(Duration::from_secs(4), async {
            while let Some(v) = pending.next().await {
                samples.push(v);
            }
        })
        .await;
        drop(pending);
        for (i, id, result) in samples {
            if let Ok(v) = result {
                let key = format!("{prefix}{id}");
                containers[i].stats = Some(stats(&v, previous.get(&key)));
                previous.insert(key, v["cpu_stats"].clone());
            }
        }
        containers
    }
    pub async fn fetch(&self, t: &Snapshot, with_stats: bool) -> Snapshot {
        let mut s = self.containers(t).await;
        if s.state != "ok" {
            return s;
        }
        let (images, containers) = tokio::join!(self.images(&s), async {
            if with_stats {
                self.fill_stats(&s).await
            } else {
                s.containers.clone()
            }
        });
        s.containers = containers;
        s.stats_sampled = with_stats;
        match images {
            Ok(images) => {
                s.images = images;
                s.images_loaded = true;
            }
            Err(e) => s.images_error = Some(e.to_string()),
        };
        s
    }
    pub async fn inspect(&self, t: &Snapshot, id: &str) -> Result<Value> {
        validate_id(id)?;
        if t.remote() {
            let v: Value = serde_json::from_slice(&remote(t, "inspect", &[id]).await?)?;
            Ok(v[0].clone())
        } else {
            self.engine(t)
                .await?
                .json(&format!("/containers/{id}/json"))
                .await
        }
    }
    pub async fn act(&self, t: &Snapshot, verb: &str, ids: &[String]) -> Result<String> {
        ensure!(
            !t.remote() && !t.read_only && !t.stale,
            "target is read-only or stale"
        );
        if matches!(verb, "vm-start" | "vm-stop") {
            ensure!(
                matches!(t.kind.as_str(), "lima" | "colima"),
                "not a managed machine"
            );
            ensure!(
                !t.name.starts_with('-') && !t.name.contains('/'),
                "invalid machine name"
            );
            let op = if verb == "vm-start" { "start" } else { "stop" };
            if t.kind == "lima" {
                command("limactl", &[op, &t.name], 600).await?;
            } else {
                command("colima", &[op, "--profile", &t.name], 600).await?;
            }
            return Ok(format!("{op}: {}", t.name));
        }
        ensure!(t.state == "ok", "target is unavailable");
        let e = self.engine(t).await?;
        if verb == "prune" {
            ensure!(
                t.images_error.is_none() && t.images_loaded,
                "image data is unavailable"
            );
            let v = e
                .json_method(
                    reqwest::Method::POST,
                    "/images/prune?filters=%7B%22dangling%22%3A%5B%22true%22%5D%7D",
                    90,
                )
                .await?;
            return Ok(format!("Reclaimed {}", human(number(&v, "SpaceReclaimed"))));
        }
        ensure!(
            matches!(verb, "start" | "stop" | "restart" | "remove"),
            "unsupported operation"
        );
        ensure!(!ids.is_empty(), "no containers selected");
        for id in ids {
            validate_id(id)?;
        }
        let mut failures = vec![];
        let mut success = 0;
        for id in ids {
            let (method, path) = if verb == "remove" {
                (reqwest::Method::DELETE, format!("/containers/{id}?force=1"))
            } else {
                (
                    reqwest::Method::POST,
                    format!("/containers/{id}/{verb}?t=10"),
                )
            };
            match e.json_method(method, &path, 30).await {
                Ok(_) => success += 1,
                Err(err) => failures.push(format!("{id}: {err}")),
            }
        }
        Ok(format!(
            "{verb}: {success}/{} succeeded{}",
            ids.len(),
            if failures.is_empty() {
                String::new()
            } else {
                format!("; {}", failures.join("; "))
            }
        ))
    }
}
pub fn lima_target(v: &Value) -> Snapshot {
    let name = string(v, "name");
    Snapshot {
        key: format!("lima:{name}"),
        kind: "lima".into(),
        name,
        endpoint: format!("unix://{}/sock/docker.sock", string(v, "dir")),
        vm: Some(Vm {
            status: string(v, "status"),
            vm_type: string(v, "vmType"),
            arch: string(v, "arch"),
            cpus: number(v, "cpus"),
            memory_bytes: number(v, "memory"),
            disk_bytes: number(v, "disk"),
            disk_used_bytes: None,
        }),
        state: "loading".into(),
        ..Default::default()
    }
}
pub fn colima_target(v: &Value, root: &str) -> Option<Snapshot> {
    if !string(v, "runtime").starts_with("docker") {
        return None;
    }
    let name = string(v, "name");
    if name.is_empty()
        || name.starts_with('-')
        || name.contains('/')
        || matches!(name.as_str(), "." | "..")
    {
        return None;
    }
    Some(Snapshot {
        key: format!("colima:{name}"),
        kind: "colima".into(),
        name: name.clone(),
        endpoint: format!("unix://{root}/{name}/docker.sock"),
        vm: Some(Vm {
            status: string(v, "status"),
            arch: string(v, "arch"),
            cpus: number(v, "cpus"),
            memory_bytes: number(v, "memory"),
            disk_bytes: number(v, "disk"),
            ..Default::default()
        }),
        state: "loading".into(),
        ..Default::default()
    })
}
pub fn available(name: &str) -> bool {
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .any(|p| p.join(name).is_file())
}
pub async fn discover(contexts: bool) -> (Vec<Snapshot>, Vec<String>) {
    async fn source(name: &str, args: &[&str]) -> Result<Vec<Value>> {
        if !available(name) {
            return Ok(vec![]);
        }
        json_lines(&command(name, args, 10).await?)
    }
    let (lima, colima, ctx) = tokio::join!(
        source("limactl", &["list", "--format", "json"]),
        source("colima", &["list", "--json"]),
        async {
            if contexts {
                source("docker", &["context", "ls", "--format", "json"]).await
            } else {
                Ok(vec![])
            }
        }
    );
    let mut errors = vec![];
    let mut targets = vec![];
    match lima {
        Ok(v) => targets.extend(v.iter().map(lima_target)),
        Err(e) => errors.push(format!("machines: {e}")),
    };
    let root = std::env::var("COLIMA_HOME")
        .unwrap_or_else(|_| format!("{}/.colima", std::env::var("HOME").unwrap_or_default()));
    match colima {
        Ok(v) => targets.extend(v.iter().filter_map(|v| colima_target(v, &root))),
        Err(e) => errors.push(format!("profiles: {e}")),
    };
    let host = if let Ok(s) = std::env::var("DOCKER_HOST") {
        s.strip_prefix("unix://").map(str::to_string)
    } else {
        let runtime = std::env::var("XDG_RUNTIME_DIR")
            .map(|s| format!("{s}/docker.sock"))
            .unwrap_or_default();
        [runtime, "/var/run/docker.sock".into()]
            .into_iter()
            .find(|p| Path::new(p).exists())
    };
    if let Some(socket) = host {
        targets.push(Snapshot {
            key: "host".into(),
            kind: "host".into(),
            name: "host".into(),
            endpoint: format!("unix://{socket}"),
            state: "loading".into(),
            ..Default::default()
        });
    }
    match ctx {
        Ok(values) => {
            for v in values {
                let endpoint = string(&v, "DockerEndpoint");
                let name = string(&v, "Name");
                let local = endpoint.starts_with("unix://");
                if !local && !endpoint.starts_with("ssh://") && !endpoint.starts_with("tcp://") {
                    continue;
                }
                targets.push(Snapshot {
                    key: format!("{}:{name}", if local { "local" } else { "ctx" }),
                    kind: if local { "host" } else { "context" }.into(),
                    name,
                    read_only: !local,
                    endpoint,
                    state: "loading".into(),
                    ..Default::default()
                });
            }
        }
        Err(e) => errors.push(format!("contexts: {e}")),
    };
    let mut sockets = HashSet::new();
    targets.retain(|t| {
        t.socket()
            .is_none_or(|p| sockets.insert(std::fs::canonicalize(p).unwrap_or_else(|_| p.into())))
    });
    (targets, errors)
}
pub fn storage_text(v: &Value) -> String {
    fn sum(items: &Value, field: &str) -> Option<u64> {
        array(items)
            .iter()
            .try_fold(0u64, |a, v| a.checked_add(v.pointer(field)?.as_u64()?))
    }
    let show = |n: Option<u64>| n.map(human).unwrap_or("unknown".into());
    format!(
        "Storage accounting\n\nImage layers: {}\nContainer writable layers: {}\nVolumes: {}\nBuild cache (logical): {}\n\nCategories may share data and are not additive.\nGuest OS, bind mounts and some logs are excluded.\nNo automatic deletion.",
        show(v["LayersSize"].as_u64()),
        show(sum(&v["Containers"], "/SizeRw")),
        show(sum(&v["Volumes"], "/UsageData/Size")),
        show(sum(&v["BuildCache"], "/Size"))
    )
}
pub fn masked_inspect(mut v: Value) -> Value {
    if let Some(env) = v.pointer_mut("/Config/Env").and_then(Value::as_array_mut) {
        for value in env {
            if let Some(s) = value.as_str() {
                *value = json!(format!("{}=••••", s.split('=').next().unwrap_or("")));
            }
        }
    }
    v
}
