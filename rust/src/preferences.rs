//! Small per-edition preferences. Fixtures never read or write this file.
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Default, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Preferences {
    pub last_target: Option<String>,
    pub light: bool,
    pub sort: u8,
    pub folded: HashSet<String>,
    pub images_open: bool,
}
impl Preferences {
    pub fn path() -> Option<PathBuf> {
        let base = std::env::var_os("XDG_CONFIG_HOME")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".config")))?;
        Some(base.join("runtop/lite.json"))
    }
    pub fn load(path: &Path) -> Self {
        let mut p: Self = fs::read(path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        if p.sort > 2 {
            p.sort = 0;
        }
        p
    }
    pub fn save(&self, path: &Path) -> anyhow::Result<()> {
        use std::os::unix::fs::OpenOptionsExt;
        let parent = path
            .parent()
            .ok_or_else(|| anyhow::anyhow!("missing preference directory"))?;
        fs::create_dir_all(parent)?;
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let temp = parent.join(format!(".lite-{}-{stamp}.tmp", std::process::id()));
        let result = (|| -> anyhow::Result<()> {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temp)?;
            file.write_all(&serde_json::to_vec_pretty(self)?)?;
            file.write_all(b"\n")?;
            file.sync_all()?;
            fs::rename(&temp, path)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(temp);
        }
        result
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip_and_invalid_preferences() {
        let dir = std::env::temp_dir().join(format!("runtop-prefs-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("lite.json");
        let p = Preferences {
            last_target: Some("ctx:sample".into()),
            light: true,
            sort: 2,
            images_open: true,
            folded: HashSet::from(["local|app".into()]),
        };
        p.save(&path).unwrap();
        let loaded = Preferences::load(&path);
        assert!(loaded.light && loaded.images_open);
        assert_eq!(loaded.last_target, p.last_target);
        assert_eq!(loaded.folded, p.folded);
        assert_eq!(loaded.sort, 2);
        fs::write(&path, b"{broken").unwrap();
        assert_eq!(Preferences::load(&path).sort, 0);
        fs::write(&path, br#"{"sort":99}"#).unwrap();
        assert_eq!(Preferences::load(&path).sort, 0);
        fs::remove_dir_all(dir).unwrap();
    }
}
