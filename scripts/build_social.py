"""Build the 1280x640 social preview card from the runtop mark and wordmark.

GitHub, Slack, X and the docs site all read a 2:1 card. GitHub wants at least
640x320 and under 1 MB, and renders 1280x640 best, so that is the size here.
The card is drawn from the same six numbers as the mark; only the two lines of
supporting text need a font, and they fall back through a geometric sans.
"""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FONT = 'Avenir Next, Helvetica Neue, Helvetica, Arial, sans-serif'
TAGLINE = 'A terminal workspace for containers and their machines.'
EDITIONS = 'FULL EDITION · LITE EDITION'
# Stone 900/200/50/700, deep sage and sage, as in assets/brand/README.md.
THEMES = {
    'dark': {'ground': '#1c1917', 'ink': '#e7e5e4', 'lit': '#87a087', 'text': '#a8a29e', 'accent': '#87a087'},
    'light': {'ground': '#fafaf9', 'ink': '#44403c', 'lit': '#5f7161', 'text': '#57534e', 'accent': '#5f7161'},
}


def card(theme):
    ink, lit = theme['ink'], theme['lit']
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640" role="img" aria-label="runtop: {TAGLINE}">
  <title>runtop</title>
  <rect width="1280" height="640" fill="{theme['ground']}"/>
  <g transform="translate(282.5 150) scale(2.5)">
    <g transform="rotate(45 32 32)" fill="none" stroke="{ink}" stroke-width="6" stroke-linejoin="round"><rect x="14" y="14" width="12.5" height="12.5" rx="2" fill="{lit}" stroke="{lit}"/><rect x="37.5" y="14" width="12.5" height="12.5" rx="2"/><rect x="14" y="37.5" width="12.5" height="12.5" rx="2"/><rect x="37.5" y="37.5" width="12.5" height="12.5" rx="2"/></g>
    <g transform="translate(68 0)" fill="none" stroke="{ink}" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"><path d="M 15 45 V 19 M 15 32 A 13 13 0 0 1 28 19"/><path d="M 42 19 V 32 A 13 13 0 0 0 68 32 V 19 M 68 19 V 45"/><path d="M 82 45 V 19 M 82 32 A 13 13 0 0 1 108 32 V 45"/><path d="M 124 11 V 38 A 7 7 0 0 0 131 45 H 134 M 118 19 H 133"/><path d="M 146 32 A 13 13 0 1 1 172 32 A 13 13 0 1 1 146 32"/><path d="M 186 19 V 57 M 186 32 A 13 13 0 1 1 212 32 A 13 13 0 1 1 186 32"/></g>
  </g>
  <text x="640" y="410" text-anchor="middle" font-family="{FONT}" font-size="38" fill="{theme['text']}" letter-spacing="0.3">{TAGLINE}</text>
  <text x="640" y="484" text-anchor="middle" font-family="{FONT}" font-size="21" font-weight="500" fill="{theme['accent']}" letter-spacing="5.5">{EDITIONS}</text>
</svg>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', choices=sorted(THEMES), default='dark')
    parser.add_argument('--out', type=Path, default=ROOT/'assets/brand', help='directory for the card')
    parser.add_argument('--name', default='social-preview', help='base name for the pair of files')
    args = parser.parse_args()
    svg = args.out/f'{args.name}.svg'
    png = args.out/f'{args.name}.png'
    svg.write_text(card(THEMES[args.theme]))
    subprocess.run(['rsvg-convert', '-w', '1280', '-h', '640', str(svg), '-o', str(png)], check=True)
    size = png.stat().st_size
    if size > 1_000_000:
        raise SystemExit(f'{png.name} is {size} bytes; GitHub rejects social previews over 1 MB')
    shown = [path.relative_to(ROOT) if path.is_relative_to(ROOT) else path for path in (svg, png)]
    print(f'{shown[0]} and {shown[1]} built at 1280x640 ({size/1024:.0f} KB)')


if __name__ == '__main__': main()
