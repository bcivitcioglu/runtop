use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

pub const SCHEMA: &str = "runtop.snapshot/v1";
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Stats {
    pub cpu_percent: Option<f64>,
    pub mem_bytes: u64,
    pub mem_limit_bytes: u64,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Container {
    pub id: String,
    pub name: String,
    pub image: String,
    pub state: String,
    pub status: String,
    pub health: Option<String>,
    pub exit_code: Option<i64>,
    pub project: String,
    pub service: String,
    pub ports: Vec<String>,
    pub stats: Option<Stats>,
}
impl Container {
    pub fn attention(&self) -> bool {
        self.health.as_deref() == Some("unhealthy")
            || matches!(self.state.as_str(), "restarting" | "dead")
            || self.exit_code.is_some_and(|n| n != 0)
    }
    pub fn matches(&self, query: &str) -> bool {
        if query == "attention" {
            return self.attention();
        }
        [
            &self.name,
            &self.image,
            &self.project,
            &self.service,
            &self.state,
            &self.status,
        ]
        .iter()
        .any(|s| s.to_lowercase().contains(query))
            || self.ports.iter().any(|s| s.contains(query))
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Image {
    pub id: String,
    #[serde(rename = "ref")]
    pub reference: String,
    pub size_bytes: u64,
    pub containers: Option<u64>,
    pub dangling: bool,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Vm {
    pub status: String,
    pub vm_type: String,
    pub arch: String,
    pub cpus: u64,
    pub memory_bytes: u64,
    pub disk_bytes: u64,
    pub disk_used_bytes: Option<u64>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub key: String,
    pub kind: String,
    pub name: String,
    pub read_only: bool,
    pub endpoint: String,
    pub state: String,
    pub error: Option<String>,
    pub vm: Option<Vm>,
    pub containers: Vec<Container>,
    pub images: Vec<Image>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub images_error: Option<String>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub stale: bool,
    #[serde(skip)]
    pub images_loaded: bool,
    #[serde(skip)]
    pub stats_sampled: bool,
}
impl Snapshot {
    pub fn remote(&self) -> bool {
        self.kind == "context"
    }
    pub fn writable(&self) -> bool {
        !self.read_only && !self.remote() && !self.stale && self.state == "ok"
    }
    pub fn socket(&self) -> Option<&str> {
        self.endpoint.strip_prefix("unix://")
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Document {
    pub schema: String,
    pub generated_at: String,
    pub targets: Vec<Snapshot>,
}
impl Document {
    pub fn new(targets: Vec<Snapshot>) -> Self {
        Self {
            schema: SCHEMA.into(),
            generated_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            targets,
        }
    }
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        let mut d: Self = serde_json::from_str(text)?;
        anyhow::ensure!(d.schema == SCHEMA, "unsupported snapshot schema");
        for t in &mut d.targets {
            t.read_only |= t.remote();
            t.images_loaded = true;
        }
        Ok(d)
    }
}
pub fn string(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or("").to_owned()
}
pub fn number(v: &Value, key: &str) -> u64 {
    v[key].as_u64().unwrap_or(0)
}
pub fn array(v: &Value) -> &[Value] {
    v.as_array().map(Vec::as_slice).unwrap_or(&[])
}
pub fn health(status: &str) -> Option<String> {
    [
        ("(healthy)", "healthy"),
        ("(unhealthy)", "unhealthy"),
        ("(health: starting)", "starting"),
    ]
    .iter()
    .find(|(s, _)| status.contains(s))
    .map(|(_, s)| s.to_string())
}
pub fn exit_code(status: &str) -> Option<i64> {
    status
        .strip_prefix("Exited (")?
        .split(')')
        .next()?
        .parse()
        .ok()
}
pub fn port_key(s: &str) -> (u64, u64) {
    let (a, b) = s.split_once("->").unwrap_or((s, s));
    (a.parse().unwrap_or(0), b.parse().unwrap_or(0))
}
pub fn normalize_container(v: &Value) -> Container {
    let mut names: Vec<_> = array(&v["Names"])
        .iter()
        .filter_map(Value::as_str)
        .map(|s| s.trim_start_matches('/'))
        .collect();
    names.sort_unstable();
    let mut ports: Vec<String> = array(&v["Ports"])
        .iter()
        .filter_map(|p| {
            let private = number(p, "PrivatePort");
            let public = number(p, "PublicPort");
            if public > 0 {
                Some(format!("{public}->{private}"))
            } else if private > 0 {
                Some(private.to_string())
            } else {
                None
            }
        })
        .collect();
    ports.sort_unstable_by_key(|p| port_key(p));
    ports.dedup();
    let status = string(v, "Status");
    Container {
        id: string(v, "Id").chars().take(12).collect(),
        name: names.join(","),
        image: string(v, "Image"),
        state: string(v, "State").to_lowercase(),
        health: health(&status),
        exit_code: exit_code(&status),
        status,
        project: string(&v["Labels"], "com.docker.compose.project"),
        service: string(&v["Labels"], "com.docker.compose.service"),
        ports,
        stats: None,
    }
}
pub fn normalize_image(v: &Value) -> Image {
    let tags: Vec<_> = array(&v["RepoTags"])
        .iter()
        .filter_map(Value::as_str)
        .filter(|s| *s != "<none>:<none>")
        .collect();
    let reference = if let Some(tag) = tags.first() {
        tag.split("@sha256:").next().unwrap_or(tag).to_string()
    } else if let Some(d) = array(&v["RepoDigests"]).first().and_then(Value::as_str) {
        let (repo, hash) = d.split_once('@').unwrap_or((d, ""));
        format!(
            "{repo}@{}",
            hash.trim_start_matches("sha256:")
                .chars()
                .take(12)
                .collect::<String>()
        )
    } else {
        "<none>:<none>".into()
    };
    Image {
        id: string(v, "Id")
            .trim_start_matches("sha256:")
            .chars()
            .take(12)
            .collect(),
        reference,
        size_bytes: number(v, "Size"),
        containers: v["Containers"].as_u64(),
        dangling: tags.is_empty(),
    }
}
pub fn stats(v: &Value, previous: Option<&Value>) -> Stats {
    let cpu = &v["cpu_stats"];
    let precpu = &v["precpu_stats"];
    let p = if precpu["system_cpu_usage"].is_u64() {
        precpu
    } else {
        previous.unwrap_or(precpu)
    };
    let delta = number(&cpu["cpu_usage"], "total_usage")
        .checked_sub(number(&p["cpu_usage"], "total_usage"));
    let sys = number(cpu, "system_cpu_usage").checked_sub(number(p, "system_cpu_usage"));
    let cores = cpu["online_cpus"]
        .as_u64()
        .filter(|n| *n > 0)
        .unwrap_or_else(|| array(&cpu["cpu_usage"]["percpu_usage"]).len().max(1) as u64);
    let cpu_percent = match (delta, sys) {
        (Some(d), Some(s)) if s > 0 && p["system_cpu_usage"].is_u64() => {
            Some((d as f64 / s as f64 * cores as f64 * 100.0 * 10000.0).round() / 10000.0)
        }
        _ => None,
    };
    let mem = &v["memory_stats"];
    let cache = mem["stats"]["inactive_file"]
        .as_u64()
        .or_else(|| mem["stats"]["total_inactive_file"].as_u64())
        .unwrap_or(0);
    Stats {
        cpu_percent,
        mem_bytes: number(mem, "usage").saturating_sub(cache),
        mem_limit_bytes: number(mem, "limit"),
    }
}
pub fn normalize_remote_container(v: &Value) -> Container {
    let labels: serde_json::Map<String, Value> = string(v, "Labels")
        .split(',')
        .filter_map(|s| s.split_once('='))
        .map(|(k, v)| (k.into(), json!(v)))
        .collect();
    let ports = remote_ports(&string(v, "Ports"));
    let status = string(v, "Status");
    Container {
        id: string(v, "ID").chars().take(12).collect(),
        name: string(v, "Names"),
        image: string(v, "Image"),
        state: string(v, "State").to_lowercase(),
        health: health(&status),
        exit_code: exit_code(&status),
        status,
        project: labels
            .get("com.docker.compose.project")
            .and_then(Value::as_str)
            .unwrap_or("")
            .into(),
        service: labels
            .get("com.docker.compose.service")
            .and_then(Value::as_str)
            .unwrap_or("")
            .into(),
        ports,
        stats: None,
    }
}
pub fn remote_ports(s: &str) -> Vec<String> {
    use std::sync::LazyLock;
    static PAIR: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"(\d+)->(\d+)").unwrap());
    static BARE: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"(\d+)/(?:tcp|udp)").unwrap());
    let mut used = std::collections::HashSet::new();
    let mut out = vec![];
    for c in PAIR.captures_iter(s) {
        used.insert(c[1].to_string());
        used.insert(c[2].to_string());
        out.push(format!("{}->{}", &c[1], &c[2]));
    }
    for c in BARE.captures_iter(s) {
        if !used.contains(&c[1]) {
            out.push(c[1].to_string());
        }
    }
    out.sort_unstable_by_key(|p| port_key(p));
    out.dedup();
    out
}
pub fn parse_size(s: &str) -> u64 {
    let s = s.trim();
    let split = s
        .find(|c: char| !c.is_ascii_digit() && c != '.')
        .unwrap_or(s.len());
    let n = s[..split].parse::<f64>().unwrap_or(0.0);
    let mult = match s[split..].trim().to_uppercase().as_str() {
        "KB" => 1e3,
        "MB" => 1e6,
        "GB" => 1e9,
        "TB" => 1e12,
        "KIB" => 1024.0,
        "MIB" => 1048576.0,
        "GIB" => 1073741824.0,
        _ => 1.0,
    };
    (n * mult) as u64
}
pub fn normalize_remote_image(v: &Value) -> Image {
    let repo = string(v, "Repository");
    let tag = string(v, "Tag");
    let dangling = repo == "<none>";
    Image {
        id: string(v, "ID")
            .trim_start_matches("sha256:")
            .chars()
            .take(12)
            .collect(),
        reference: if dangling {
            "<none>:<none>".into()
        } else if tag == "<none>" || tag.is_empty() {
            repo
        } else {
            format!("{repo}:{tag}")
        },
        size_bytes: parse_size(&string(v, "Size")),
        containers: v["Containers"]
            .as_u64()
            .or_else(|| v["Containers"].as_str().and_then(|s| s.parse().ok())),
        dangling,
    }
}
pub fn human(n: u64) -> String {
    if n >= 1 << 30 {
        format!("{:.1}G", n as f64 / (1u64 << 30) as f64)
    } else if n >= 1 << 20 {
        format!("{:.1}M", n as f64 / (1u64 << 20) as f64)
    } else if n >= 1 << 10 {
        format!("{:.1}K", n as f64 / 1024.0)
    } else {
        format!("{n}B")
    }
}
