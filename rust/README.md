# runtop lite

A compact terminal workspace for containers and their machines.

```sh
cargo install --locked runtop
rt --demo
```

`rt` opens lite. The `runtop` launcher prefers the full edition when it is also
installed on your executable path. Use `--edition` to identify the selected edition.

Browse grouped containers, inspect resource use, follow logs, filter and sort,
inspect storage, and confirm actions without leaving the terminal. Remote targets
are always read-only. Local logs stay bounded in memory.

See the [user guide](https://github.com/bcivitcioglu/runtop#readme) for navigation,
installation, shared command-line output, and the full edition’s archive features.
