use crate::{
    backend::{self, Backend},
    cli::{Args, selected_index},
    logs::{Line as LogLine, clean},
    model::*,
};
use anyhow::{Context, Result, ensure};
use crossterm::{
    event::{
        DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyCode, KeyEvent,
        KeyEventKind, KeyModifiers, MouseEventKind,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use futures_util::StreamExt;
use ratatui::{
    Frame, Terminal,
    backend::CrosstermBackend,
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap},
};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    io::{self, IsTerminal},
    time::{Duration, Instant},
};
use tokio::{sync::mpsc, task::JoinHandle};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Row {
    Machine,
    Project(String),
    Container(String),
    Images,
    Image(String),
}
#[derive(Clone)]
struct Pending {
    scroll: u16,
    target: Snapshot,
    verb: String,
    ids: Vec<String>,
    names: Vec<String>,
}
enum Message {
    Discovery(Vec<Snapshot>, Vec<String>),
    Snapshot(usize, Box<Snapshot>),
    Detail(usize, String),
    Action(String),
}
#[derive(Default)]
struct Overlay {
    title: String,
    text: String,
    scroll: u16,
    logs: bool,
    follow: bool,
    wrap: bool,
}
#[derive(Default, Clone)]
struct Totals {
    count: usize,
    running: usize,
    cpu: f64,
    memory: u64,
    attention: bool,
    cpu_unknown: bool,
    mem_unknown: bool,
}
pub struct App {
    container_index: HashMap<String, usize>,
    totals: HashMap<String, Totals>,
    pub targets: Vec<Snapshot>,
    pub selected: usize,
    pub cursor: usize,
    pub rows: Vec<Row>,
    pub query: String,
    pub filtering: bool,
    pub sort: u8,
    pub folded: HashSet<String>,
    pub images_open: bool,
    cache: HashMap<String, Snapshot>,
    histories: HashMap<String, VecDeque<f64>>,
    generation: usize,
    pending: Option<Pending>,
    overlay: Option<Overlay>,
    logs: VecDeque<LogLine>,
    log_bytes: usize,
    message: String,
    read_only: bool,
    light: bool,
    list_state: ListState,
    last_area: Rect,
    last_fetch: Instant,
    blurred: bool,
}
impl App {
    pub fn new(targets: Vec<Snapshot>, selected: usize, read_only: bool) -> Self {
        let mut app = Self {
            container_index: HashMap::new(),
            totals: HashMap::new(),
            targets,
            selected,
            cursor: 0,
            rows: vec![],
            query: String::new(),
            filtering: false,
            sort: 0,
            folded: HashSet::new(),
            images_open: false,
            cache: HashMap::new(),
            histories: HashMap::new(),
            generation: 0,
            pending: None,
            overlay: None,
            logs: VecDeque::new(),
            log_bytes: 0,
            message: String::new(),
            read_only,
            light: std::env::var("RUNTOP_THEME").as_deref() == Ok("light"),
            list_state: ListState::default(),
            last_area: Rect::default(),
            last_fetch: Instant::now() - Duration::from_secs(60),
            blurred: false,
        };
        for t in &app.targets {
            if t.state != "loading" {
                app.cache.insert(t.key.clone(), t.clone());
            }
        }
        app.rebuild();
        app
    }
    fn target(&self) -> Option<&Snapshot> {
        self.targets.get(self.selected)
    }
    fn snapshot(&self) -> Option<&Snapshot> {
        let t = self.target()?;
        self.cache.get(&t.key).or(Some(t))
    }
    fn group_key(&self, p: &str) -> String {
        format!(
            "{}|{p}",
            self.target().map(|t| t.key.as_str()).unwrap_or("")
        )
    }
    fn container(&self, id: &str) -> Option<&Container> {
        self.snapshot()?
            .containers
            .get(*self.container_index.get(id)?)
    }
    pub fn rebuild(&mut self) {
        let old = self.rows.get(self.cursor).cloned();
        let mut rows = vec![];
        let mut indexes = HashMap::new();
        let mut totals: HashMap<String, Totals> = HashMap::new();
        if let Some(s) = self.snapshot() {
            rows.push(Row::Machine);
            indexes.extend(
                s.containers
                    .iter()
                    .enumerate()
                    .map(|(i, c)| (c.id.clone(), i)),
            );
            let mut cs: Vec<_> = s
                .containers
                .iter()
                .filter(|c| c.matches(&self.query))
                .collect();
            cs.sort_by(|a, b| {
                a.project.cmp(&b.project).then_with(|| match self.sort {
                    1 => b
                        .stats
                        .as_ref()
                        .and_then(|s| s.cpu_percent)
                        .unwrap_or(-1.0)
                        .total_cmp(&a.stats.as_ref().and_then(|s| s.cpu_percent).unwrap_or(-1.0))
                        .then(a.name.cmp(&b.name)),
                    2 => b
                        .stats
                        .as_ref()
                        .map(|s| s.mem_bytes)
                        .cmp(&a.stats.as_ref().map(|s| s.mem_bytes))
                        .then(a.name.cmp(&b.name)),
                    _ => a.name.cmp(&b.name),
                })
            });
            let mut project: Option<&str> = None;
            for c in cs {
                let total = totals.entry(c.project.clone()).or_default();
                total.count += 1;
                total.running += usize::from(c.state == "running");
                total.attention |= c.attention();
                if c.state == "running" {
                    total.cpu_unknown |= c.stats.as_ref().and_then(|s| s.cpu_percent).is_none();
                    total.mem_unknown |= c.stats.is_none();
                }
                if let Some(st) = &c.stats {
                    total.cpu += st.cpu_percent.unwrap_or(0.0);
                    total.memory = total.memory.saturating_add(st.mem_bytes);
                }
                if project != Some(&c.project) {
                    rows.push(Row::Project(c.project.clone()));
                    project = Some(&c.project);
                }
                if !self.folded.contains(&self.group_key(&c.project)) {
                    rows.push(Row::Container(c.id.clone()));
                }
            }
            rows.push(Row::Images);
            if self.images_open {
                rows.extend(s.images.iter().map(|i| Row::Image(i.id.clone())));
            }
        }
        self.cursor = old
            .and_then(|old| rows.iter().position(|r| r == &old))
            .unwrap_or(self.cursor)
            .min(rows.len().saturating_sub(1));
        self.rows = rows;
        self.container_index = indexes;
        self.totals = totals;
    }
    fn apply(&mut self, mut s: Snapshot) {
        let key = s.key.clone();
        if let Some(old) = self.cache.get(&key) {
            if s.state == "unreachable" && old.state == "ok" {
                let error = s.error.clone();
                s = old.clone();
                s.stale = true;
                s.error = error;
                s.stats_sampled = false;
            } else if s.state == "ok" {
                if !s.images_loaded {
                    s.images = old.images.clone();
                }
                if !s.stats_sampled {
                    for c in &mut s.containers {
                        if let Some(old) = old.containers.iter().find(|o| o.id == c.id) {
                            c.stats = old.stats.clone();
                        }
                    }
                }
            }
        }
        if s.stats_sampled && !s.stale {
            for c in &s.containers {
                if let Some(cpu) = c.stats.as_ref().and_then(|s| s.cpu_percent) {
                    let history = self.histories.entry(format!("{key}|{}", c.id)).or_default();
                    if history.len() >= 40 {
                        history.pop_front();
                    }
                    history.push_back(cpu);
                }
            }
        }
        let live: HashSet<_> = s
            .containers
            .iter()
            .map(|c| format!("{key}|{}", c.id))
            .collect();
        let prefix = format!("{key}|");
        self.histories
            .retain(|k, _| !k.starts_with(&prefix) || live.contains(k));
        if let Some(p) = &self.pending {
            if p.target.key == key
                && (s.stale
                    || s.state != "ok"
                    || p.ids
                        .iter()
                        .any(|id| !s.containers.iter().any(|c| &c.id == id)))
            {
                self.pending = None;
                self.message = "Confirmation cancelled: target data changed".into();
            }
        }
        self.cache.insert(key, s);
        self.rebuild();
    }
    fn members(&self) -> (Vec<String>, Vec<String>) {
        let Some(s) = self.snapshot() else {
            return (vec![], vec![]);
        };
        let selected = self.rows.get(self.cursor);
        let cs: Vec<_> = s
            .containers
            .iter()
            .filter(|c| match selected {
                Some(Row::Container(id)) => &c.id == id,
                Some(Row::Project(p)) => &c.project == p && c.matches(&self.query),
                _ => false,
            })
            .collect();
        (
            cs.iter().map(|c| c.id.clone()).collect(),
            cs.iter().map(|c| c.name.clone()).collect(),
        )
    }
    fn request_action(&mut self, key: char) {
        let Some(target) = self.snapshot().cloned() else {
            return;
        };
        if self.read_only || target.read_only || target.remote() || target.stale {
            self.message = "Actions unavailable: read-only or stale target".into();
            return;
        }
        let machine = self.rows.get(self.cursor) == Some(&Row::Machine);
        let verb = match (key, machine) {
            ('s', true) => "vm-start",
            ('x', true) => "vm-stop",
            ('s', false) => "start",
            ('x', false) => "stop",
            ('R', _) => "restart",
            ('X', _) => "remove",
            ('p', _) => "prune",
            _ => return,
        };
        if machine && matches!(key, 's' | 'x') && !matches!(target.kind.as_str(), "lima" | "colima")
        {
            self.message = "Machine lifecycle is unavailable for this target".into();
            return;
        }
        if !verb.starts_with("vm-") && !target.writable() {
            self.message = "Target is unavailable".into();
            return;
        }
        if verb == "prune" && (!target.images_loaded || target.images_error.is_some()) {
            self.message = "Image data is unavailable".into();
            return;
        }
        let (ids, names) = self.members();
        if !verb.starts_with("vm-") && verb != "prune" && ids.is_empty() {
            self.message = "Select a container or project".into();
            return;
        }
        self.pending = Some(Pending {
            scroll: 0,
            target,
            verb: verb.into(),
            ids,
            names,
        });
    }
    fn move_cursor(&mut self, delta: isize) {
        self.cursor = self
            .cursor
            .saturating_add_signed(delta)
            .min(self.rows.len().saturating_sub(1));
    }
    fn toggle(&mut self) {
        match self.rows.get(self.cursor).cloned() {
            Some(Row::Project(p)) => {
                let k = self.group_key(&p);
                if !self.folded.remove(&k) {
                    self.folded.insert(k);
                }
            }
            Some(Row::Images) => self.images_open = !self.images_open,
            _ => {}
        }
        self.rebuild();
    }
    fn switch(&mut self, delta: isize) {
        if self.targets.is_empty() {
            return;
        }
        self.selected =
            (self.selected as isize + delta).rem_euclid(self.targets.len() as isize) as usize;
        self.cursor = 0;
        self.pending = None;
        self.overlay = None;
        self.generation += 1;
        self.last_fetch = Instant::now() - Duration::from_secs(60);
        self.rebuild();
    }
    fn help(&mut self) {
        self.overlay=Some(Overlay{title:"Help".into(),text:"Navigate  ↑↓ / jk   PgUp/PgDn   g/G\nMachines  [ ] / ← → / Tab\nFold      Space / Enter\nLogs      l / Enter on container\nInspect   i    Storage D\nFilter    /    Clear Esc    Sort o\nActions   s start · x stop · R restart · X remove\nImages    p prune dangling\nShell     e\nRefresh   r    Theme t    Help ?    Quit q\n\nLogs: f follow · w wrap · ↑↓ scroll · Esc back\nAll actions confirm and bind to the original target.\nRemote and stale targets are read-only.\nArchive recording and search require the full edition.".into(),wrap:true,..Default::default()});
    }
}
fn padded(s: &str, width: usize) -> String {
    let s = clean(s);
    let mut text = String::new();
    let mut used = 0;
    for c in s.chars() {
        let w = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if used + w > width {
            break;
        }
        text.push(c);
        used += w;
    }
    text.extend(std::iter::repeat_n(' ', width.saturating_sub(used)));
    text
}
fn spark(values: impl Iterator<Item = f64>) -> String {
    values
        .map(|n| {
            ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'][((n / 100.0 * 7.0).round() as usize).min(7)]
        })
        .collect()
}
fn state_color(c: &Container) -> Color {
    if c.attention() {
        Color::Red
    } else if c.state == "running" {
        Color::Green
    } else {
        Color::DarkGray
    }
}
impl App {
    fn row_line(&self, row: &Row, width: usize) -> Line<'static> {
        let Some(s) = self.snapshot() else {
            return Line::default();
        };
        match row {
            Row::Machine => Line::from(vec![
                Span::styled("◈ ", Style::default().fg(Color::Cyan)),
                Span::styled(
                    format!(
                        "{}  {}{}",
                        s.name,
                        s.state,
                        if s.read_only || self.read_only {
                            "  read-only"
                        } else {
                            ""
                        }
                    ),
                    Style::default().add_modifier(Modifier::BOLD),
                ),
            ]),
            Row::Project(p) => {
                let total = self.totals.get(p).cloned().unwrap_or_default();
                let running = total.running;
                let cpu = total.cpu;
                let mem = total.memory;
                let color = if total.attention {
                    Color::Yellow
                } else if running == total.count {
                    Color::Green
                } else {
                    Color::DarkGray
                };
                Line::from(Span::styled(
                    format!(
                        "{} {}  {running}/{} up{}",
                        if self.folded.contains(&self.group_key(p)) {
                            "▸"
                        } else {
                            "▾"
                        },
                        if p.is_empty() { "standalone" } else { p },
                        total.count,
                        if width >= 60 && !s.remote() {
                            format!(
                                "   {}  {}",
                                if total.cpu_unknown {
                                    "—".into()
                                } else {
                                    format!("{cpu:.1}%")
                                },
                                if total.mem_unknown {
                                    "—".into()
                                } else {
                                    human(mem)
                                }
                            )
                        } else {
                            String::new()
                        }
                    ),
                    Style::default().fg(color).add_modifier(Modifier::BOLD),
                ))
            }
            Row::Container(id) => {
                let Some(c) = self.container(id) else {
                    return Line::default();
                };
                let mut spans = vec![Span::styled("  ● ", Style::default().fg(state_color(c)))];
                let stats = !s.remote();
                let wide = width >= 100;
                let medium = width >= 60;
                let name_width = if wide {
                    26
                } else if medium {
                    22
                } else {
                    width.saturating_sub(if stats { 17 } else { 5 }).max(1)
                };
                spans.push(Span::raw(padded(&c.name, name_width)));
                if medium {
                    spans.push(Span::styled(
                        padded(c.health.as_deref().unwrap_or(&c.state), 11),
                        Style::default().fg(state_color(c)),
                    ));
                }
                if stats {
                    let cpu = c
                        .stats
                        .as_ref()
                        .and_then(|s| s.cpu_percent)
                        .map(|n| format!("{n:.1}%"))
                        .unwrap_or("—".into());
                    let mem = c
                        .stats
                        .as_ref()
                        .map(|s| human(s.mem_bytes))
                        .unwrap_or("—".into());
                    spans.push(Span::raw(format!(
                        " {} {}",
                        padded(&cpu, 6),
                        padded(&mem, 6)
                    )));
                    if wide {
                        let hist = self.histories.get(&format!("{}|{id}", s.key));
                        spans.push(Span::styled(
                            format!(
                                " {} ",
                                padded(
                                    &spark(
                                        hist.into_iter()
                                            .flatten()
                                            .rev()
                                            .take(8)
                                            .copied()
                                            .collect::<Vec<_>>()
                                            .into_iter()
                                            .rev()
                                    ),
                                    8
                                )
                            ),
                            Style::default().fg(Color::Cyan),
                        ));
                    }
                }
                if wide {
                    spans.push(Span::raw(format!(
                        " {} {}",
                        padded(&c.ports.join(","), 16),
                        c.image
                    )));
                }
                Line::from(spans)
            }
            Row::Images => Line::from(Span::styled(
                format!(
                    "{} Images  {} · {} dangling{}",
                    if self.images_open { "▾" } else { "▸" },
                    s.images.len(),
                    s.images.iter().filter(|i| i.dangling).count(),
                    if s.images_error.is_some() {
                        " · unavailable"
                    } else if !s.images_loaded {
                        " · loading"
                    } else {
                        ""
                    }
                ),
                Style::default().fg(Color::Cyan),
            )),
            Row::Image(id) => {
                let Some(i) = s.images.iter().find(|i| &i.id == id) else {
                    return Line::default();
                };
                Line::from(format!(
                    "  {} {}  {}",
                    padded(&i.reference, width.saturating_sub(24)),
                    human(i.size_bytes),
                    i.id
                ))
            }
        }
    }
    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let bg = if self.light {
            Color::Rgb(247, 248, 250)
        } else {
            Color::Rgb(17, 21, 28)
        };
        let fg = if self.light {
            Color::Rgb(31, 39, 52)
        } else {
            Color::Rgb(212, 220, 231)
        };
        frame.render_widget(Block::default().style(Style::default().bg(bg).fg(fg)), area);
        let layout = Layout::vertical([
            Constraint::Length(2),
            Constraint::Length(2),
            Constraint::Min(1),
            Constraint::Length(2),
        ])
        .split(area);
        let target = self
            .target()
            .map(|t| t.name.as_str())
            .unwrap_or("discovering");
        let title = Line::from(vec![
            Span::styled(
                " runtop ",
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::raw(format!("lite  /  {target}  ")),
            Span::styled(
                format!(
                    "{}/{}",
                    if self.targets.is_empty() {
                        0
                    } else {
                        self.selected + 1
                    },
                    self.targets.len()
                ),
                Style::default().fg(Color::DarkGray),
            ),
        ]);
        frame.render_widget(
            Paragraph::new(title).block(Block::default().borders(Borders::BOTTOM)),
            layout[0],
        );
        let status = if self.filtering || !self.query.is_empty() {
            format!(
                " /{}{}  · sort: {}",
                self.query,
                if self.filtering { "▏" } else { "" },
                ["name", "cpu", "memory"][self.sort as usize]
            )
        } else if let Some(s) = self.snapshot() {
            if s.stale {
                format!(
                    " STALE · {}",
                    s.error.as_deref().unwrap_or("refresh failed")
                )
            } else if let Some(e) = &s.error {
                format!(" {e}")
            } else if let Some(vm) = &s.vm {
                format!(
                    " {} · {} CPUs · {} memory · {} disk capacity",
                    vm.status,
                    vm.cpus,
                    human(vm.memory_bytes),
                    human(vm.disk_bytes)
                )
            } else {
                format!(
                    " {} containers · {} images{}",
                    s.containers.len(),
                    s.images.len(),
                    if s.remote() { " · read-only" } else { "" }
                )
            }
        } else {
            " Discovering machines…".into()
        };
        frame.render_widget(Paragraph::new(clean(&status)), layout[1]);
        self.last_area = layout[2];
        if self.rows.is_empty() {
            frame.render_widget(
                Paragraph::new(" No targets. Press r to retry; ? for help."),
                layout[2],
            );
        } else {
            let items: Vec<_> = self
                .rows
                .iter()
                .map(|r| ListItem::new(self.row_line(r, layout[2].width as usize)))
                .collect();
            self.list_state.select(Some(self.cursor));
            let selection = if self.light {
                Color::Rgb(210, 224, 239)
            } else {
                Color::Rgb(36, 50, 67)
            };
            frame.render_stateful_widget(
                List::new(items)
                    .highlight_style(Style::default().bg(selection).add_modifier(Modifier::BOLD))
                    .highlight_symbol("▎"),
                layout[2],
                &mut self.list_state,
            );
            if self.snapshot().is_some_and(|s| {
                s.state == "ok" && s.containers.iter().all(|c| !c.matches(&self.query))
            }) {
                let rect = Rect::new(
                    layout[2].x + 2,
                    layout[2].y + 2,
                    layout[2].width.saturating_sub(2),
                    1,
                );
                if rect.y < layout[2].bottom() {
                    frame.render_widget(
                        Paragraph::new(if self.query.is_empty() {
                            "No containers"
                        } else {
                            "No matches"
                        }),
                        rect,
                    );
                }
            }
        }
        let footer = if self.message.is_empty() {
            " ↑↓ move  [ ] machine  / filter  i inspect  l logs  ? help  q quit".into()
        } else {
            format!(" {}", clean(&self.message))
        };
        frame.render_widget(
            Paragraph::new(footer).block(Block::default().borders(Borders::TOP)),
            layout[3],
        );
        if let Some(overlay) = &self.overlay {
            frame.render_widget(Clear, area);
            let block = Block::default()
                .borders(Borders::ALL)
                .title(format!(" {} · Esc back ", overlay.title))
                .style(Style::default().bg(bg).fg(fg));
            let inner = block.inner(area);
            frame.render_widget(block, area);
            if overlay.logs {
                let lines: Vec<Line> = self
                    .logs
                    .iter()
                    .map(|l| {
                        Line::from(vec![
                            Span::styled(
                                format!(
                                    "{}{}",
                                    if l.prefix.is_empty() { "" } else { &l.prefix },
                                    if l.prefix.is_empty() { "" } else { " │ " }
                                ),
                                Style::default().fg(Color::Cyan),
                            ),
                            Span::styled(
                                clean(&l.text),
                                Style::default().fg(if l.stream == "stderr" {
                                    Color::Red
                                } else {
                                    fg
                                }),
                            ),
                        ])
                    })
                    .collect();
                let height = inner.height as usize;
                let scroll = if overlay.follow {
                    lines.len().saturating_sub(height).min(u16::MAX as usize) as u16
                } else {
                    overlay.scroll
                };
                let mut p = Paragraph::new(Text::from(lines)).scroll((scroll, 0));
                if overlay.wrap {
                    p = p.wrap(Wrap { trim: false });
                }
                frame.render_widget(p, inner);
            } else {
                let mut p = Paragraph::new(overlay.text.clone()).scroll((overlay.scroll, 0));
                if overlay.wrap {
                    p = p.wrap(Wrap { trim: false });
                }
                frame.render_widget(p, inner);
            }
        }
        if let Some(p) = &self.pending {
            let width = area.width.saturating_sub(4).min(80);
            let height = area.height.saturating_sub(4).min(18);
            let rect = Rect::new(
                area.x + (area.width - width) / 2,
                area.y + (area.height - height) / 2,
                width,
                height,
            );
            frame.render_widget(Clear, rect);
            let names = if p.names.is_empty() {
                p.target.name.clone()
            } else {
                p.names.join("\n")
            };
            let text = format!(
                "{} on {}\n\n{}\n\n[y] confirm · ↑↓ scroll · any other key cancels",
                p.verb,
                p.target.name,
                clean(&names)
            );
            frame.render_widget(
                Paragraph::new(text)
                    .scroll((p.scroll, 0))
                    .wrap(Wrap { trim: false })
                    .block(Block::default().borders(Borders::ALL).title(" Confirm "))
                    .style(Style::default().bg(bg).fg(Color::Yellow)),
                rect,
            );
        }
    }
}
struct TerminalGuard;
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen);
    }
}
fn terminal_start(mouse: bool) -> Result<Terminal<CrosstermBackend<io::Stdout>>> {
    enable_raw_mode()?;
    execute!(io::stdout(), EnterAlternateScreen)?;
    if mouse {
        execute!(io::stdout(), EnableMouseCapture)?;
    }
    Ok(Terminal::new(CrosstermBackend::new(io::stdout()))?)
}
fn abort(task: &mut Option<JoinHandle<()>>) {
    if let Some(task) = task.take() {
        task.abort();
    }
}
fn fetch(
    app: &mut App,
    backend: &Backend,
    tx: &mpsc::Sender<Message>,
    task: &mut Option<JoinHandle<()>>,
) {
    if task.as_ref().is_some_and(|h| !h.is_finished()) {
        return;
    }
    let Some(t) = app.target().cloned() else {
        return;
    };
    let b = backend.clone();
    let tx = tx.clone();
    let generation = app.generation;
    app.last_fetch = Instant::now();
    *task = Some(tokio::spawn(async move {
        let mut s = b.containers(&t).await;
        let _ = tx
            .send(Message::Snapshot(generation, Box::new(s.clone())))
            .await;
        if s.state != "ok" {
            return;
        }
        let base = s.clone();
        let images = b.images(&base);
        let stats = b.fill_stats(&base);
        tokio::pin!(images);
        tokio::pin!(stats);
        tokio::select! {
            image=&mut images=>{match image{Ok(v)=>{s.images=v;s.images_loaded=true;},Err(e)=>s.images_error=Some(e.to_string())};let _=tx.send(Message::Snapshot(generation,Box::new(s.clone()))).await;s.containers=stats.await;s.stats_sampled=true;},
            cs=&mut stats=>{s.containers=cs;s.stats_sampled=true;let _=tx.send(Message::Snapshot(generation,Box::new(s.clone()))).await;s.stats_sampled=false;match images.await{Ok(v)=>{s.images=v;s.images_loaded=true;},Err(e)=>s.images_error=Some(e.to_string())};}
        }
        let _ = tx.send(Message::Snapshot(generation, Box::new(s))).await;
    }));
}
async fn remote_logs(
    t: &Snapshot,
    id: &str,
    tx: mpsc::Sender<LogLine>,
    prefix: String,
) -> Result<()> {
    backend::validate_id(id)?;
    let mut child = tokio::process::Command::new("docker")
        .args([
            "--context",
            &t.name,
            "logs",
            "--tail",
            "200",
            "--timestamps",
            "-f",
            id,
        ])
        .kill_on_drop(true)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()?;
    async fn reader(
        mut pipe: impl tokio::io::AsyncRead + Unpin,
        tx: mpsc::Sender<LogLine>,
        prefix: String,
        stderr: bool,
    ) -> Result<()> {
        use tokio::io::AsyncReadExt;
        let mut buf = [0; 8192];
        let mut decoder = crate::logs::Decoder::new(true);
        loop {
            let n = pipe.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            for mut l in decoder.feed(&buf[..n])? {
                l.prefix = prefix.clone();
                if stderr {
                    l.stream = "stderr".into();
                }
                tx.send(l).await.context("log view closed")?;
            }
        }
        for mut l in decoder.finish() {
            l.prefix = prefix.clone();
            if stderr {
                l.stream = "stderr".into();
            }
            tx.send(l).await.context("log view closed")?;
        }
        Ok(())
    }
    let out = child.stdout.take().context("stdout")?;
    let err = child.stderr.take().context("stderr")?;
    tokio::try_join!(
        reader(out, tx.clone(), prefix.clone(), false),
        reader(err, tx, prefix, true)
    )?;
    ensure!(child.wait().await?.success(), "remote log stream failed");
    Ok(())
}
fn start_logs(
    app: &mut App,
    args: &Args,
    backend: &Backend,
    tx: &mpsc::Sender<LogLine>,
    tasks: &mut Vec<JoinHandle<()>>,
) {
    for t in tasks.drain(..) {
        t.abort();
    }
    let Some(target) = app.snapshot().cloned() else {
        return;
    };
    let (ids, names) = app.members();
    if ids.is_empty() {
        app.message = "Select a container or project".into();
        return;
    }
    if ids.len() > 64 {
        app.message = "Select at most 64 containers for combined logs".into();
        return;
    }
    app.logs.clear();
    app.log_bytes = 0;
    app.overlay = Some(Overlay {
        title: format!("Logs · {}", names.join(", ")),
        logs: true,
        follow: true,
        ..Default::default()
    });
    if args.demo || args.snapshot.is_some() {
        for i in 0..50 {
            app.logs.push_back(LogLine {
                stream: if i % 7 == 0 { "stderr" } else { "stdout" }.into(),
                text: format!("sample event {i}: service ready"),
                prefix: names[0].clone(),
            });
        }
        return;
    }
    let b = backend.clone();
    let tx = tx.clone();
    tasks.push(tokio::spawn(async move {
        // One reader per selected source, capped at 64. Backpressure bounds buffers.
        let work = futures_util::stream::iter(ids.into_iter().zip(names).map(|(id, name)| {
            let t = target.clone();
            let b = b.clone();
            let tx = tx.clone();
            async move {
                let result = if t.remote() {
                    remote_logs(&t, &id, tx.clone(), name.clone()).await
                } else {
                    match b.engine(&t).await {
                        Ok(e) => e.logs(&id, true, tx.clone(), name.clone()).await,
                        Err(e) => Err(e),
                    }
                };
                if let Err(e) = result {
                    let _ = tx
                        .send(LogLine {
                            stream: "stderr".into(),
                            text: format!("Log stream stopped: {e}"),
                            prefix: name,
                        })
                        .await;
                }
            }
        }))
        .buffer_unordered(64);
        tokio::pin!(work);
        while work.next().await.is_some() {}
    }));
}
fn detail(
    app: &mut App,
    args: &Args,
    backend: &Backend,
    tx: &mpsc::Sender<Message>,
    task: &mut Option<JoinHandle<()>>,
    storage: bool,
) {
    abort(task);
    let Some(target) = app.snapshot().cloned() else {
        return;
    };
    let row = app.rows.get(app.cursor).cloned();
    let generation = app.generation;
    app.overlay = Some(Overlay {
        title: if storage { "Storage" } else { "Inspector" }.into(),
        text: "Loading…".into(),
        wrap: true,
        ..Default::default()
    });
    if args.demo || args.snapshot.is_some() {
        let text = if storage {
            backend::storage_text(
                &serde_json::json!({"LayersSize":1234000000,"Containers":[{"SizeRw":3450000}],"Volumes":[],"BuildCache":[]}),
            )
        } else {
            match row {
                Some(Row::Container(id)) => app
                    .container(&id)
                    .and_then(|c| serde_json::to_string_pretty(c).ok())
                    .unwrap_or_default(),
                _ => serde_json::to_string_pretty(&target).unwrap_or_default(),
            }
        };
        app.overlay.as_mut().unwrap().text = text;
        return;
    }
    let b = backend.clone();
    let tx = tx.clone();
    *task = Some(tokio::spawn(async move {
        let result: Result<String> = async {
            if storage {
                ensure!(
                    !target.remote(),
                    "Storage is available only for local targets"
                );
                let e = b.engine(&target).await?;
                Ok(backend::storage_text(
                    &e.json_method(reqwest::Method::GET, "/system/df", 30)
                        .await?,
                ))
            } else if let Some(Row::Container(id)) = row {
                Ok(serde_json::to_string_pretty(&backend::masked_inspect(
                    b.inspect(&target, &id).await?,
                ))?)
            } else {
                Ok(serde_json::to_string_pretty(&target)?)
            }
        }
        .await;
        let _ = tx
            .send(Message::Detail(
                generation,
                result.unwrap_or_else(|e| e.to_string()),
            ))
            .await;
    }));
}
fn scripted_key(token: &str) -> Option<KeyEvent> {
    let code = match token {
        "down" => KeyCode::Down,
        "up" => KeyCode::Up,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "enter" => KeyCode::Enter,
        "space" => KeyCode::Char(' '),
        "tab" => KeyCode::Tab,
        "escape" | "esc" => KeyCode::Esc,
        "pgdown" => KeyCode::PageDown,
        "pgup" => KeyCode::PageUp,
        s if s.chars().count() == 1 => KeyCode::Char(s.chars().next()?),
        _ => return None,
    };
    Some(KeyEvent::new(code, KeyModifiers::NONE))
}
pub async fn run(args: Args) -> Result<()> {
    ensure!(
        io::stdout().is_terminal(),
        "interactive mode needs a terminal; use ps --json for scripts"
    );
    let fixture = args.fixture()?;
    let initial = fixture
        .as_ref()
        .map(|d| d.targets.clone())
        .unwrap_or_default();
    let selected = selected_index(&initial, args.target.as_deref());
    let mut app = App::new(initial, selected, args.read_only);
    let backend = Backend::default();
    let (tx, mut rx) = mpsc::channel::<Message>(16);
    let (log_tx, mut log_rx) = mpsc::channel::<LogLine>(256);
    let mut fetch_task = None;
    let mut detail_task = None;
    let mut discovery_task = None;
    let mut action_task = None;
    let mut log_tasks: Vec<JoinHandle<()>> = vec![];
    let _guard = TerminalGuard;
    let mut terminal = terminal_start(!args.no_mouse)?;
    let mut events = EventStream::new();
    let mut heartbeat = tokio::time::interval(Duration::from_millis(50));
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let started = Instant::now();
    let mut last_discovery = Instant::now() - Duration::from_secs(60);
    let mut dirty = true;
    let mut quit = false;
    let mut script: VecDeque<String> = args
        .keys
        .as_deref()
        .unwrap_or("")
        .split_whitespace()
        .map(str::to_string)
        .collect();
    let mut next_key = Instant::now() + Duration::from_millis(400);
    while !quit {
        if dirty {
            terminal.draw(|f| app.draw(f))?;
            dirty = false;
        }
        let mut input = None;
        tokio::select! {
            _=tokio::signal::ctrl_c()=>{quit=true;},
            event=events.next()=>{match event{Some(Ok(Event::Key(k))) if k.kind!=KeyEventKind::Release=>input=Some(k),Some(Ok(Event::Resize(..)))=>dirty=true,Some(Ok(Event::FocusLost))=>app.blurred=true,Some(Ok(Event::FocusGained))=>app.blurred=false,
                Some(Ok(Event::Mouse(m)))=>{match m.kind{MouseEventKind::ScrollDown=>app.move_cursor(3),MouseEventKind::ScrollUp=>app.move_cursor(-3),MouseEventKind::Down(_)=>{
                    if m.row<2 {app.switch(1);abort(&mut fetch_task);}else if app.last_area.contains((m.column,m.row).into()){let i=app.list_state.offset()+(m.row-app.last_area.y) as usize;if i==app.cursor{app.toggle();}else{app.cursor=i.min(app.rows.len().saturating_sub(1));}}},_=>{}}dirty=true;},Some(Err(e))=>return Err(e.into()),None=>quit=true,_=>{}}},
            message=rx.recv()=>{if let Some(m)=message{match m{
                Message::Discovery(targets,errors)=>{if targets.is_empty()&&!errors.is_empty()&&!app.targets.is_empty(){app.message=errors.join("; ");for s in app.cache.values_mut(){s.stale=true;}app.pending=None;dirty=true;continue;}let old=app.target().cloned();let first=app.targets.is_empty();app.targets=targets;app.selected=if first{selected_index(&app.targets,args.target.as_deref())}else{selected_index(&app.targets,old.as_ref().map(|t|t.key.as_str()))};
                    if old.as_ref().map(|t|(&t.key,&t.endpoint))!=app.target().map(|t|(&t.key,&t.endpoint)){app.generation+=1;app.pending=None;app.overlay=None;abort(&mut fetch_task);abort(&mut detail_task);for t in log_tasks.drain(..){t.abort();}app.last_fetch=Instant::now()-Duration::from_secs(60);}
                    app.cache.retain(|k,s|app.targets.iter().any(|t|&t.key==k&&t.endpoint==s.endpoint));app.histories.retain(|k,_|app.targets.iter().any(|t|k.starts_with(&format!("{}|",t.key))));for target in &app.targets {if let Some(s)=app.cache.get_mut(&target.key){s.vm=target.vm.clone();if target.vm.as_ref().is_some_and(|v|v.status!="Running"){s.state="vm_stopped".into();s.containers.clear();s.images.clear();}}}app.message=errors.join("; ");app.rebuild();},
                Message::Snapshot(generation,s)=>if generation==app.generation{app.apply(*s);},
                Message::Detail(generation,text)=>if generation==app.generation{if let Some(o)=&mut app.overlay{o.text=text;}},
                Message::Action(text)=>{app.message=text;app.last_fetch=Instant::now()-Duration::from_secs(60);}
            }dirty=true;}},
            _=heartbeat.tick()=>{
                if args.quit_after.is_some_and(|s|started.elapsed().as_secs_f64()>=s){quit=true;}
                if fixture.is_none(){if last_discovery.elapsed()>=Duration::from_secs(if app.blurred{40}else{10})&&discovery_task.as_ref().is_none_or(|h:&JoinHandle<()>|h.is_finished()){
                    let tx=tx.clone();let contexts=!args.no_contexts;discovery_task=Some(tokio::spawn(async move{let (t,e)=backend::discover(contexts).await;let _=tx.send(Message::Discovery(t,e)).await;}));last_discovery=Instant::now();}
                    let interval=if app.target().is_some_and(Snapshot::remote){15}else{2}*if app.blurred{4}else{1};if app.last_fetch.elapsed()>=Duration::from_secs(interval){fetch(&mut app,&backend,&tx,&mut fetch_task);}}
                let mut n=0;while let Ok(line)=log_rx.try_recv(){if app.overlay.as_ref().is_some_and(|o|o.logs){app.log_bytes+=line.text.len()+line.prefix.len();app.logs.push_back(line);while app.logs.len()>2000||app.log_bytes>2*1024*1024 {if let Some(old)=app.logs.pop_front(){app.log_bytes-=old.text.len()+old.prefix.len();}else{break;}}dirty=true;}n+=1;if n>=256{break;}}
                if Instant::now()>=next_key{if let Some(token)=script.pop_front(){if let Some(wait)=token.strip_prefix("wait:").and_then(|s|s.parse::<f64>().ok()).filter(|n|n.is_finite()&&*n>=0.0){next_key=Instant::now()+Duration::from_secs_f64(wait);}else{input=scripted_key(&token);next_key=Instant::now()+Duration::from_millis(200);}}}
            }
        }
        let Some(key) = input else {
            continue;
        };
        dirty = true;
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            quit = true;
            continue;
        }
        if let Some(p) = &mut app.pending {
            match key.code {
                KeyCode::Up => {
                    p.scroll = p.scroll.saturating_sub(1);
                    continue;
                }
                KeyCode::Down => {
                    p.scroll = p.scroll.saturating_add(1);
                    continue;
                }
                KeyCode::PageUp => {
                    p.scroll = p.scroll.saturating_sub(10);
                    continue;
                }
                KeyCode::PageDown => {
                    p.scroll = p.scroll.saturating_add(10);
                    continue;
                }
                _ => {}
            }
        }
        if let Some(p) = app.pending.take() {
            if key.code == KeyCode::Char('y') {
                if fixture.is_some() {
                    app.message = format!("Demo: {} confirmed for {}", p.verb, p.target.name);
                } else if action_task
                    .as_ref()
                    .is_none_or(|h: &JoinHandle<()>| h.is_finished())
                {
                    let b = backend.clone();
                    let tx = tx.clone();
                    action_task = Some(tokio::spawn(async move {
                        let result: Result<String> = async {
                            let (targets, _) = backend::discover(true).await;
                            let current = targets
                                .iter()
                                .find(|t| t.key == p.target.key && t.endpoint == p.target.endpoint)
                                .context("Target changed; action cancelled")?;
                            if !p.verb.starts_with("vm-") {
                                let fresh = b.containers(current).await;
                                ensure!(
                                    fresh.state == "ok",
                                    "Target unavailable; action cancelled"
                                );
                                ensure!(
                                    p.ids
                                        .iter()
                                        .all(|id| fresh.containers.iter().any(|c| &c.id == id)),
                                    "Container disappeared; action cancelled"
                                );
                            }
                            b.act(&p.target, &p.verb, &p.ids).await
                        }
                        .await;
                        let _ = tx
                            .send(Message::Action(result.unwrap_or_else(|e| e.to_string())))
                            .await;
                    }));
                    app.message = "Action in progress…".into();
                } else {
                    app.message = "Another action is still in progress".into();
                }
            }
            continue;
        }
        if let Some(o) = &mut app.overlay {
            match key.code {
                KeyCode::Esc | KeyCode::Char('q') => {
                    app.overlay = None;
                    abort(&mut detail_task);
                    for t in log_tasks.drain(..) {
                        t.abort();
                    }
                    while log_rx.try_recv().is_ok() {}
                }
                KeyCode::Char('f') if o.logs => o.follow = !o.follow,
                KeyCode::Char('w') => o.wrap = !o.wrap,
                KeyCode::Up | KeyCode::Char('k') => {
                    o.follow = false;
                    o.scroll = o.scroll.saturating_sub(1);
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    o.follow = false;
                    o.scroll = o.scroll.saturating_add(1);
                }
                KeyCode::PageUp => {
                    o.follow = false;
                    o.scroll = o.scroll.saturating_sub(10);
                }
                KeyCode::PageDown => {
                    o.follow = false;
                    o.scroll = o.scroll.saturating_add(10);
                }
                KeyCode::Char('g') => {
                    o.follow = false;
                    o.scroll = 0;
                }
                KeyCode::Char('G') => {
                    if o.logs {
                        o.follow = true;
                    } else {
                        o.scroll = o
                            .text
                            .lines()
                            .count()
                            .saturating_sub((terminal.size()?.height as usize).saturating_sub(2))
                            .min(u16::MAX as usize) as u16;
                    }
                }
                _ => {}
            }
            continue;
        }
        if app.filtering {
            match key.code {
                KeyCode::Esc => {
                    app.filtering = false;
                    app.query.clear();
                }
                KeyCode::Enter => app.filtering = false,
                KeyCode::Backspace => {
                    app.query.pop();
                }
                KeyCode::Char(c) if app.query.len() < 256 => {
                    app.query.push(c.to_ascii_lowercase());
                }
                _ => {}
            }
            app.rebuild();
            continue;
        }
        match key.code {
            KeyCode::Char('q') => quit = true,
            KeyCode::Up | KeyCode::Char('k') => app.move_cursor(-1),
            KeyCode::Down | KeyCode::Char('j') => app.move_cursor(1),
            KeyCode::PageUp => app.move_cursor(-10),
            KeyCode::PageDown => app.move_cursor(10),
            KeyCode::Char('g') => app.cursor = 0,
            KeyCode::Char('G') => app.cursor = app.rows.len().saturating_sub(1),
            KeyCode::Left | KeyCode::Char('[') => {
                app.switch(-1);
                abort(&mut fetch_task);
                abort(&mut detail_task);
            }
            KeyCode::Right | KeyCode::Char(']') | KeyCode::Tab => {
                app.switch(1);
                abort(&mut fetch_task);
                abort(&mut detail_task);
            }
            KeyCode::Char('/') => app.filtering = true,
            KeyCode::Esc => {
                app.query.clear();
                app.rebuild();
            }
            KeyCode::Char('o') => {
                app.sort = (app.sort + 1) % 3;
                app.rebuild();
            }
            KeyCode::Char('t') => app.light = !app.light,
            KeyCode::Char('r') => {
                abort(&mut fetch_task);
                app.last_fetch = Instant::now() - Duration::from_secs(60);
                last_discovery = Instant::now() - Duration::from_secs(60);
            }
            KeyCode::Char('?') => app.help(),
            KeyCode::Enter if matches!(app.rows.get(app.cursor), Some(Row::Container(_))) => {
                start_logs(&mut app, &args, &backend, &log_tx, &mut log_tasks)
            }
            KeyCode::Enter | KeyCode::Char(' ') => app.toggle(),
            KeyCode::Char('l') => start_logs(&mut app, &args, &backend, &log_tx, &mut log_tasks),
            KeyCode::Char('i') => detail(&mut app, &args, &backend, &tx, &mut detail_task, false),
            KeyCode::Char('D') => detail(&mut app, &args, &backend, &tx, &mut detail_task, true),
            KeyCode::Char(c @ ('s' | 'x' | 'R' | 'X' | 'p')) => app.request_action(c),
            KeyCode::Char('e') => {
                if let (Some(t), Some(Row::Container(id))) =
                    (app.snapshot().cloned(), app.rows.get(app.cursor).cloned())
                {
                    if args.demo || args.snapshot.is_some() {
                        app.message = "Shell is unavailable in demo mode".into();
                    } else if app.read_only || !t.writable() {
                        app.message = "Shell unavailable: read-only or stale target".into();
                    } else {
                        let fresh = backend.containers(&t).await;
                        if fresh.state != "ok" || !fresh.containers.iter().any(|c| c.id == id) {
                            app.message = "Container is unavailable".into();
                            continue;
                        }
                        disable_raw_mode()?;
                        execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen)?;
                        let result=std::process::Command::new("docker").args(["--host",&t.endpoint,"exec","-it",&id,"sh","-c","if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]).status();
                        enable_raw_mode()?;
                        execute!(io::stdout(), EnterAlternateScreen)?;
                        if !args.no_mouse {
                            execute!(io::stdout(), EnableMouseCapture)?;
                        }
                        terminal.clear()?;
                        app.message = result
                            .map(|s| format!("Shell exited: {s}"))
                            .unwrap_or_else(|e| e.to_string());
                    }
                }
            }
            _ => {}
        }
    }
    for task in [&mut fetch_task, &mut discovery_task, &mut detail_task] {
        abort(task);
    }
    for t in log_tasks {
        t.abort();
    }
    if let Some(task) = action_task {
        task.abort();
        let _ = task.await;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn app() -> App {
        let d = Document::parse(include_str!("../../spec/fixtures/snapshots/demo.json")).unwrap();
        App::new(d.targets, 0, false)
    }
    #[test]
    fn selection_survives_sort_and_refresh() {
        let mut a = app();
        a.cursor = a
            .rows
            .iter()
            .position(|r| matches!(r, Row::Container(_)))
            .unwrap();
        let selected = a.rows[a.cursor].clone();
        a.sort = 2;
        a.rebuild();
        assert_eq!(a.rows[a.cursor], selected);
        a.apply(a.snapshot().unwrap().clone());
        assert_eq!(a.rows[a.cursor], selected);
    }
    #[test]
    fn stale_data_retained_and_mutations_blocked() {
        let mut a = app();
        let before = a.snapshot().unwrap().containers.len();
        let mut failed = a.target().unwrap().clone();
        failed.state = "unreachable".into();
        failed.error = Some("offline".into());
        failed.containers.clear();
        a.apply(failed);
        assert!(a.snapshot().unwrap().stale);
        assert_eq!(a.snapshot().unwrap().containers.len(), before);
        a.request_action('p');
        assert!(a.pending.is_none());
    }
    #[test]
    fn project_actions_capture_filtered_members() {
        let mut a = app();
        let c = a.snapshot().unwrap().containers[0].clone();
        a.query = c.name.clone();
        a.rebuild();
        a.cursor = a
            .rows
            .iter()
            .position(|r| matches!(r, Row::Project(_)))
            .unwrap();
        a.request_action('x');
        let p = a.pending.as_ref().unwrap();
        assert_eq!(p.ids, vec![c.id]);
        let endpoint = p.target.endpoint.clone();
        a.switch(1);
        assert!(a.pending.is_none());
        assert_ne!(a.target().unwrap().endpoint, endpoint);
    }
    #[test]
    fn bounded_history_and_no_duplicate_partial_samples() {
        let mut a = app();
        let mut s = a.snapshot().unwrap().clone();
        s.stats_sampled = true;
        let id = s.containers[0].id.clone();
        s.containers[0].stats = Some(Stats {
            cpu_percent: Some(2.0),
            ..Default::default()
        });
        for _ in 0..100 {
            a.apply(s.clone());
        }
        let key = format!("{}|{id}", s.key);
        assert_eq!(a.histories[&key].len(), 40);
        s.stats_sampled = false;
        a.apply(s);
        assert_eq!(a.histories[&key].len(), 40);
    }
    #[test]
    fn tiny_terminal_and_unicode_are_safe() {
        let mut a = app();
        a.targets[0].name = "界界\x1b[2J".into();
        for width in [1, 5, 20, 40] {
            let mut t = Terminal::new(ratatui::backend::TestBackend::new(width, 4)).unwrap();
            t.draw(|f| a.draw(f)).unwrap();
        }
        assert_eq!(padded("界界", 3), "界 ");
    }
}
