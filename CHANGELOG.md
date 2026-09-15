# Changelog

## 0.1.1 — 2026-09-15

- Boxed Containers / Images selector in full, independent of machine selection.
- Visible machine navigation in lite, with a keyboard and mouse chooser.
- Continuous log ingestion and efficient visible-line rendering in lite; wrapped
  logs follow the actual bottom of the view.
- Ignore obsolete refresh results after a machine changes state, and keep mouse
  input inside active lite overlays.

## 0.1.0 — 2026-09-14

- Full workspace with machine discovery, grouped containers, inspector,
  project logs, confirmed actions, search, themes and narrow-terminal layouts.
- Managed-machine lifecycle routing, local socket deduplication, and read-only
  remote contexts.
- On-demand storage accounting, connectivity diagnostics and stale-data handling.
- Progressive container refreshes while image and stats requests complete independently.
- Optional log recording with configurable destination, rotation, retention and storage
  limits; archive export and search through the TUI and CLI.
- Compact lite edition with grouped containers, live statistics, logs, inspection,
  on-demand storage, confirmed actions, and responsive terminal layouts.
- Shared scriptable listings and snapshot JSON across both editions.
- Edition-aware launchers, standalone release archives, and package release workflows.
