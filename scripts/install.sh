#!/bin/sh
# User-local installation: no compiler or administrator access needed.
set -eu
edition=${1:-lite}
case "$edition" in
  full|runtop) edition=full ;;
  lite|both) ;;
  -h|--help) printf 'usage: install.sh [lite|full|both]\nOptions: RUNTOP_BIN_DIR, RUNTOP_VERSION (default: latest)\n'; exit 0 ;;
  *) printf 'Unknown edition: %s\n' "$edition" >&2; exit 2 ;;
esac
bin_dir=${RUNTOP_BIN_DIR:-$HOME/.local/bin}
version=${RUNTOP_VERSION:-latest}
case "$bin_dir" in /*) ;; *) printf 'RUNTOP_BIN_DIR must be an absolute path\n' >&2; exit 2 ;; esac
if [ "$version" != latest ]; then
  case "$version" in *[!0-9.]*|'') printf 'Invalid version\n' >&2; exit 2 ;; esac
fi
command -v curl >/dev/null 2>&1 || { printf 'curl is required to download releases.\n' >&2; exit 1; }
temp_dir=$(mktemp -d)
trap 'rm -rf "$temp_dir"' EXIT
trap 'exit 1' HUP INT TERM
fetch() { curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location "$1" -o "$2"; }
# Refuse to replace an unrelated command. Recognized editions can be upgraded in either order.
for command_name in runtop rt; do
  [ "$command_name" != rt ] || [ "$edition" != full ] || continue
  destination="$bin_dir/$command_name"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    existing=$("$destination" --version 2>/dev/null || true)
    case "$existing" in 'runtop '*' (full)'|'runtop '*' (lite)') ;;
      *) printf 'Refusing to replace unrelated command: %s\nChoose RUNTOP_BIN_DIR to install elsewhere.\n' "$destination" >&2; exit 1 ;;
    esac
  fi
done
if [ "$edition" != full ]; then
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64|Darwin/aarch64) target=aarch64-apple-darwin ;;
    Darwin/x86_64) target=x86_64-apple-darwin ;;
    Linux/x86_64) target=x86_64-unknown-linux-musl ;;
    Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-musl ;;
    *) printf 'No lite executable for this platform.\n' >&2; exit 1 ;;
  esac
  lite_version=$version
  if [ "$lite_version" = latest ]; then
    release_url=$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
      -o /dev/null --write-out '%{url_effective}' https://github.com/bcivitcioglu/runtop/releases/latest)
    lite_version=$(printf '%s\n' "$release_url" | sed -n 's@.*/tag/rs%2[Ff]v\([0-9][0-9.]*\)$@\1@p;s@.*/tag/rs/v\([0-9][0-9.]*\)$@\1@p')
    [ -n "$lite_version" ] || { printf 'Could not determine the latest lite release; set RUNTOP_VERSION.\n' >&2; exit 1; }
  fi
  name="runtop-lite-$lite_version-$target.tar.gz"
  url="https://github.com/bcivitcioglu/runtop/releases/download/rs%2Fv$lite_version"
  fetch "$url/$name" "$temp_dir/$name"
  fetch "$url/$name.sha256" "$temp_dir/checksum"
  expected=$(awk 'NR==1 {print $1}' "$temp_dir/checksum")
  case "$expected" in *[!0-9a-fA-F]*|'') printf 'Invalid release checksum\n' >&2; exit 1 ;; esac
  [ "${#expected}" -eq 64 ] || { printf 'Invalid release checksum\n' >&2; exit 1; }
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$temp_dir/$name" | awk '{print $1}')
  else
    actual=$(shasum -a 256 "$temp_dir/$name" | awk '{print $1}')
  fi
  [ "$actual" = "$expected" ] || { printf 'Release checksum mismatch; nothing installed.\n' >&2; exit 1; }
  tar -xzf "$temp_dir/$name" -C "$temp_dir" rt runtop
  [ "$("$temp_dir/rt" --edition)" = lite ] || { printf 'Invalid lite executable\n' >&2; exit 1; }
fi
if [ "$edition" != lite ]; then
  if command -v uv >/dev/null 2>&1; then
    runner=$(command -v uv)
  else
    printf 'Preparing the full-edition installer...\n'
    fetch https://astral.sh/uv/install.sh "$temp_dir/prepare.sh"
    UV_UNMANAGED_INSTALL="$temp_dir/runner" UV_NO_MODIFY_PATH=1 sh "$temp_dir/prepare.sh"
    runner="$temp_dir/runner/uv"
  fi
  requirement=runtop
  [ "$version" = latest ] || requirement="runtop==$version"
  UV_TOOL_BIN_DIR="$bin_dir" "$runner" tool install --python '>=3.11' --upgrade --refresh-package runtop --force "$requirement"
fi
mkdir -p "$bin_dir"
place() {
  install -m 755 "$temp_dir/$1" "$bin_dir/.$1-install-$$"
  mv -f "$bin_dir/.$1-install-$$" "$bin_dir/$1"
}
if [ "$edition" != full ]; then
  place rt
  existing_edition=$("$bin_dir/runtop" --edition 2>/dev/null || true)
  [ "$existing_edition" = full ] || place runtop
  "$bin_dir/rt" --version
fi
[ "$edition" = lite ] || "$bin_dir/runtop" --version
printf 'Installed %s in %s\n' "$edition" "$bin_dir"
# Set up the default user-local path for supported shells, but only when the
# current PATH lacks it. Custom destinations remain explicit, and
# RUNTOP_UPDATE_PATH=0 opts out of profile changes.
path_configured=0
case ":$PATH:" in
  *":$bin_dir:"*) path_configured=1 ;;
esac
if [ "$path_configured" = 0 ] && [ -z "${RUNTOP_BIN_DIR:-}" ] && [ "${RUNTOP_UPDATE_PATH:-1}" != 0 ]; then
  profile_file=''
  shell_name=${SHELL:-}
  case "${shell_name##*/}" in
    zsh) profile_file="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) case "$(uname -s)" in Darwin) profile_file="$HOME/.bash_profile" ;; *) profile_file="$HOME/.bashrc" ;; esac ;;
    fish) profile_file="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/runtop.fish" ;;
  esac
  if [ -n "$profile_file" ] && grep -Fq '# runtop executable path' "$profile_file" 2>/dev/null; then
    printf 'PATH is already configured in %s. Open a new terminal to use it.\n' "$profile_file"
    path_configured=1
  elif [ -n "$profile_file" ] && mkdir -p "$(dirname "$profile_file")"; then
    if [ "${shell_name##*/}" = fish ]; then
      path_setup='
# runtop executable path
if test "$PATH[1]" != "$HOME/.local/bin"
    set -gx PATH "$HOME/.local/bin" $PATH
end
'
    else
      path_setup='
# runtop executable path
case "$PATH" in
  "$HOME/.local/bin"|"$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
'
    fi
    if printf '%s' "$path_setup" >> "$profile_file"; then
      printf 'Configured PATH in %s. Open a new terminal to use it.\n' "$profile_file"
      path_configured=1
    else
      printf 'Could not update %s; use the PATH instruction below.\n' "$profile_file" >&2
    fi
  fi
fi
if [ "$path_configured" = 0 ]; then
  printf '\nAdd this directory to PATH in your shell configuration:\n  export PATH="%s:$PATH"\n' "$bin_dir"
fi
resolved=$(command -v runtop || true)
if [ -n "$resolved" ] && [ "$resolved" != "$bin_dir/runtop" ]; then
  printf '\nYour shell currently finds %s first. Run %s/runtop directly, or put %s first on PATH.\n' "$resolved" "$bin_dir" "$bin_dir"
fi
