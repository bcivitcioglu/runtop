# Remembered workspace preferences

The settings below are supported in 0.1.2 and later. Preferences are small choices that make the next session feel familiar. They
contain no container data or saved log contents, and do not resume actions.

| Remembered setting | Full | Lite |
|---|---|---|
| Last selected machine | Yes | Yes |
| Theme | Yes | Dark/light |
| Sort order | Yes | Yes |
| Folded groups | Yes | Yes |
| Image view | Containers/Images selection | Images section open/closed |
| Pane widths and archive defaults | Yes | Not applicable |

Live sessions load preferences at startup and save them on normal exit. Demo and
snapshot sessions do not read or write preferences. Listings, diagnostics and
`docs` do not change workspace preferences. Search text, pending confirmations,
read-only flags and running log recordings are not restored.

An explicit `--target KEY` overrides the remembered machine. If that target no
longer exists, selection falls back to an available target. In lite,
`RUNTOP_THEME=light` or `RUNTOP_THEME=dark` overrides the saved theme for startup.
Changes made during that live session are saved when it exits.

## Files

The directory is `$XDG_CONFIG_HOME/runtop`, or `~/.config/runtop` if that variable
is unset. Full uses `config.json`; lite uses `lite.json`. Keeping separate files
lets each workspace keep its own layout without overwriting the other's settings.

Missing or malformed preferences fall back to defaults. Saves replace the file
atomically. To reset an edition, close its sessions and rename its preferences
file, then start it again. Saving can fail on a read-only configuration directory;
the workspace remains usable for that session.
