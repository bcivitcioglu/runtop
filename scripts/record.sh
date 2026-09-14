#!/usr/bin/env bash
# Record a scripted demo of runtop: asciinema cast -> GIF (agg) -> PNG keyframes.
#
#   scripts/record.sh <runtop> <name> "<keys>" <seconds> [COLSxROWS] [-- extra tool args]
#
# keys: space-separated tokens understood by the app's --keys flag, e.g.
#   "down down enter wait:2 tab ctrl+p s t o p space w e b enter wait:3"
# Output: assets/<tool>/<name>.gif, assets/<tool>/<name>.png (last frame),
#         assets/<tool>/frames/<name>-<n>.png (keyframes for design review; not for README).
# OUT_DIR=… overrides the output directory (use a scratch dir for live, non-demo recordings).
# AGG_THEME=github-light (any agg --theme) renders light-theme recordings.
set -euo pipefail

tool=${1:?tool}; name=${2:?name}; keys=${3:?keys}; secs=${4:?seconds}; size=${5:-160x45}
shift $(( $# < 5 ? $# : 5 ))
[[ ${1:-} == "--" ]] && shift
extra=("$@")

root=$(cd "$(dirname "$0")/.." && pwd)
out=${OUT_DIR:-$root/assets/$tool}
mkdir -p "$out/frames"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

case $tool in
  rt) cmd=("$root/target/release/rt") ;;
  runtop) cmd=(uv run --project "$root" runtop) ;;
  *) echo "unknown tool: $tool" >&2; exit 2 ;;
esac
[[ ${extra[*]:-} == *--live* ]] || cmd+=(--demo)
cmd+=(--keys "$keys" --quit-after "$secs")
for a in "${extra[@]:-}"; do [[ -n $a && $a != --live ]] && cmd+=("$a"); done

printf -v cmdline '%q ' "${cmd[@]}"
TERM=xterm-256color COLORTERM=truecolor \
  asciinema rec --quiet --overwrite --window-size "$size" --idle-time-limit 3 \
  -c "$cmdline" "$tmp/$name.cast"

agg --font-size 16 --idle-time-limit 3 --last-frame-duration 3 ${AGG_THEME:+--theme "$AGG_THEME"} "$tmp/$name.cast" "$out/$name.gif"

frames=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 "$out/$name.gif")
rm -f "$out/frames/$name"-*.png
n=0
# The very last frame is usually the terminal after the app left the alternate screen.
last=$(( frames > 2 ? frames - 2 : frames - 1 ))
for f in $(( frames / 5 )) $(( frames * 2 / 5 )) $(( frames * 3 / 5 )) $(( frames * 4 / 5 )) $last; do
  n=$((n + 1))
  ffmpeg -loglevel error -y -i "$out/$name.gif" -vf "select=eq(n\,$f)" -fps_mode passthrough -frames:v 1 "$out/frames/$name-$n.png"
done
cp "$out/frames/$name-$n.png" "$out/$name.png"
echo "recorded $out/$name.gif ($frames frames), keyframes in $out/frames/"
