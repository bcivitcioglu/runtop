# Install and update

Choose **lite** for the compact workspace or **full** for the multi-pane workspace
and archive tools. Both run against an existing engine. Installation does not
create a machine or start containers.

## Guided installer

Download the installer once, then select an edition:

```sh
curl -fsSL https://raw.githubusercontent.com/bcivitcioglu/runtop/master/scripts/install.sh -o /tmp/runtop-install.sh
sh /tmp/runtop-install.sh lite
```

Use `full` or `both` instead of `lite` to install those editions. The default is
lite. The installer downloads the latest stable release, verifies lite's archive
checksum, and installs in `~/.local/bin`. No compiler or administrator access is
needed. Full automatically prepares its package runner and a suitable interpreter
when needed; the first installation therefore needs an internet connection.

Run the same command again to update. Full and lite can be installed in either
order. `rt` always runs lite; `runtop` opens full when available. Installing lite
keeps an existing full entry point. An unrelated existing command is not overwritten.

If the default directory is not already on your PATH, the installer adds one
idempotent PATH entry to your zsh, bash, or fish configuration; open a new
terminal afterward. Set `RUNTOP_UPDATE_PATH=0` to leave shell configuration
untouched. Custom directories and unsupported shells get an explicit PATH
instruction instead. The installer also reports if another installation appears
earlier on your current PATH. Verify the commands your shell will use:

```sh
runtop --version
runtop --edition
rt --version
```

## Choose a directory or version

```sh
RUNTOP_BIN_DIR="$HOME/bin" sh /tmp/runtop-install.sh both
RUNTOP_VERSION=0.1.1 sh /tmp/runtop-install.sh lite
```

`RUNTOP_BIN_DIR` must be absolute. `RUNTOP_VERSION` defaults to `latest`; an exact
version must exist for each selected edition. Full and lite resolve their latest
published versions independently, so one edition's release cannot block the other.

## Package installation

Full can also be installed with `uv tool install --upgrade runtop`; lite with
`cargo install --locked runtop`. These routes require their package tools to be
available. Prefer the guided installer if you want dependency setup handled for you.

An old full installation may be pinned to a version. To replace a pin, use
`uv tool install --upgrade --refresh-package runtop --force runtop` or rerun the
guided installer. An already-installed process keeps running its old code; restart
the workspace after updating.

## Platforms and troubleshooting

Lite archives are available for macOS and Linux on ARM64 and x86-64. Other
platforms require a supported source-build environment. Full requires an
interpreter version 3.11 or later and a compatible terminal.

If installation succeeds but a command is missing, follow the PATH instruction.
If the wrong edition starts, check `command -v runtop` and `runtop --edition`;
use `rt` for lite. If no machines appear, `runtop --doctor` checks live discovery
and connectivity. `runtop docs agents` works offline even with no engine installed (0.1.2 and later).
