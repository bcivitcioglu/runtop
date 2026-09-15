//! Offline manual shared with the full edition and public website.
use serde_json::{Value, json};
pub fn run(topic: &str, list: bool, as_json: bool) -> i32 {
    let manual: Value =
        serde_json::from_str(include_str!("../../spec/manual.json")).expect("validated manual");
    let topics: Vec<_> = manual["topics"]
        .as_array()
        .expect("manual topics")
        .iter()
        .filter(|t| topic == "all" || t["id"].as_str() == Some(topic))
        .cloned()
        .collect();
    if topics.is_empty() {
        eprintln!("unknown topic {topic:?}; use rt docs --list");
        return 2;
    }
    if as_json {
        let topics: Vec<_> = topics
            .into_iter()
            .map(|mut t| {
                if list {
                    t.as_object_mut().unwrap().remove("content");
                }
                t
            })
            .collect();
        println!("{}", serde_json::to_string_pretty(&json!({"schema":"runtop.docs/v1", "version":env!("CARGO_PKG_VERSION"), "edition":"lite", "format":"markdown", "topics":topics})).unwrap());
    } else {
        for t in topics {
            if list {
                println!(
                    "{}\t{}",
                    t["id"].as_str().unwrap(),
                    t["title"].as_str().unwrap()
                );
            } else {
                println!("{}\n", t["content"].as_str().unwrap());
            }
        }
    }
    0
}
