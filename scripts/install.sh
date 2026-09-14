#!/bin/sh
# Install a released edition into a user-selected executable directory.
set -eu
edition=${1:-full}
case "$edition" in
  full|runtop|lite|both) ;;
  -h|--help) printf 'usage: install.sh [full|lite|both]\n'; exit 0 ;;
  *) printf 'Unknown edition: %s\n' "$edition" >&2; exit 2 ;;
esac
bin_dir=${RUNTOP_BIN_DIR:-$HOME/.local/bin}
version=${RUNTOP_VERSION:-0.1.0}
case "$version" in *[!0-9.]*|'') printf 'Invalid version\n' >&2; exit 2 ;; esac
if [ "$edition" != lite ]; then
  if ! command -v uv >/dev/null 2>&1; then
    printf 'The full-edition package runner (uv) is required.\n' >&2; exit 1
  fi
  UV_TOOL_BIN_DIR="$bin_dir" uv tool install --upgrade "runtop==$version"
fi
if [ "$edition" = lite ] || [ "$edition" = both ]; then
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) target=aarch64-apple-darwin ;;
    Darwin/x86_64) target=x86_64-apple-darwin ;;
    Linux/x86_64) target=x86_64-unknown-linux-musl ;;
    Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-musl ;;
    *) printf 'No release executable for this platform.\n' >&2; exit 1 ;;
  esac
  temp_dir=$(mktemp -d)
  trap 'rm -rf "$temp_dir"' EXIT HUP INT TERM
  name="runtop-lite-$version-$target.tar.gz"
  url="https://github.com/bcivitcioglu/runtop/releases/download/rs%2Fv$version"
  curl --fail --silent --show-error --location "$url/$name" -o "$temp_dir/$name"
  curl --fail --silent --show-error --location "$url/$name.sha256" -o "$temp_dir/$name.sha256"
  if command -v sha256sum >/dev/null 2>&1; then
    (cd "$temp_dir" && sha256sum -c "$name.sha256")
  else
    (cd "$temp_dir" && shasum -a 256 -c "$name.sha256")
  fi
  tar -xzf "$temp_dir/$name" -C "$temp_dir" rt runtop
  mkdir -p "$bin_dir"
  install -m 755 "$temp_dir/rt" "$bin_dir/rt"
  if [ ! -e "$bin_dir/runtop" ] || [ "$("$bin_dir/runtop" --edition 2>/dev/null || true)" = lite ]; then
    install -m 755 "$temp_dir/runtop" "$bin_dir/runtop"
  fi
fi
printf 'Installed %s in %s\n' "$edition" "$bin_dir"
