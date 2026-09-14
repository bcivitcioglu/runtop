use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Line {
    pub stream: String,
    pub text: String,
    #[serde(skip)]
    pub prefix: String,
}
pub struct Decoder {
    tty: bool,
    header: Vec<u8>,
    remaining: usize,
    stream: usize,
    lines: [Vec<u8>; 2],
}
impl Decoder {
    pub fn new(tty: bool) -> Self {
        Self {
            tty,
            header: Vec::with_capacity(8),
            remaining: 0,
            stream: 0,
            lines: [vec![], vec![]],
        }
    }
    fn split(&mut self, stream: usize, bytes: &[u8], out: &mut Vec<Line>) {
        for &b in bytes {
            if b == b'\n' {
                self.emit(stream, out, false);
            } else {
                self.lines[stream].push(b);
                if self.lines[stream].len() >= 65536 {
                    self.emit(stream, out, true);
                }
            }
        }
    }
    fn emit(&mut self, stream: usize, out: &mut Vec<Line>, continued: bool) {
        let mut text = String::from_utf8_lossy(&self.lines[stream])
            .trim_end_matches('\r')
            .to_string();
        if continued {
            text.push_str(" [continued]");
        }
        out.push(Line {
            stream: if stream == 1 { "stderr" } else { "stdout" }.into(),
            text,
            prefix: String::new(),
        });
        self.lines[stream].clear();
    }
    pub fn feed(&mut self, mut bytes: &[u8]) -> Result<Vec<Line>> {
        let mut out = vec![];
        if self.tty {
            self.split(0, bytes, &mut out);
            return Ok(out);
        }
        while !bytes.is_empty() {
            if self.remaining == 0 {
                let n = (8 - self.header.len()).min(bytes.len());
                self.header.extend_from_slice(&bytes[..n]);
                bytes = &bytes[n..];
                if self.header.len() < 8 {
                    break;
                }
                ensure!(
                    matches!(self.header[0], 1 | 2) && self.header[1..4] == [0, 0, 0],
                    "invalid log frame"
                );
                self.stream = (self.header[0] - 1) as usize;
                self.remaining = u32::from_be_bytes(self.header[4..8].try_into().unwrap()) as usize;
                self.header.clear();
                if self.remaining == 0 {
                    continue;
                }
            }
            let n = self.remaining.min(bytes.len());
            self.split(self.stream, &bytes[..n], &mut out);
            bytes = &bytes[n..];
            self.remaining -= n;
        }
        Ok(out)
    }
    pub fn finish(&mut self) -> Vec<Line> {
        let mut out = vec![];
        for i in 0..2 {
            if !self.lines[i].is_empty() {
                self.emit(i, &mut out, false);
            }
        }
        out
    }
}
pub fn clean(s: &str) -> String {
    // Terminal control bytes never reach the renderer, including OSC hyperlinks.
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            match chars.next() {
                Some('[') => {
                    for c in chars.by_ref() {
                        if ('@'..='~').contains(&c) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    while let Some(c) = chars.next() {
                        if c == '\x07' {
                            break;
                        }
                        if c == '\x1b' && chars.peek() == Some(&'\\') {
                            chars.next();
                            break;
                        }
                    }
                }
                _ => {}
            }
        } else if !c.is_control() || c == '\t' {
            out.push(c);
        }
    }
    out
}
