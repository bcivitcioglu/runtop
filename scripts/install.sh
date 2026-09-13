#!/bin/sh
# Install a released full edition. No branch builds or registry placeholders.
set -eu
case "${1:-full}" in
  full|runtop) ;;
  -h|--help) printf 'usage: install.sh [full]\n'; exit 0 ;;
  *) printf 'Only the full edition is available from this installer.\n' >&2; exit 2 ;;
esac
if ! command -v uv >/dev/null 2>&1; then
  printf 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/\n' >&2
  exit 1
fi
export UV_TOOL_BIN_DIR="${RUNTOP_BIN_DIR:-$HOME/.local/bin}"
uv tool install --upgrade 'runtop>=0.1.0'
printf 'Installed runtop (full) in %s\n' "$UV_TOOL_BIN_DIR"
