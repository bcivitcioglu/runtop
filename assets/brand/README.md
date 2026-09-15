# runtop brand

The runtop mark, Tessera, is four rounded tiles set as a diamond. The top tile is
filled, like the `●` runtop shows beside a running machine. The other three are
open, like `○`. Every file here is plain vector paths: no text elements, fonts,
gradients or embedded images.

## Files

| File | Use |
| --- | --- |
| `mark.svg` | Mark in `currentColor`, for inline SVG that inherits text color |
| `mark-on-light.svg`, `mark-on-dark.svg` | Mark with fixed colors for light and dark backgrounds |
| `mark-lit-on-light.svg`, `mark-lit-on-dark.svg` | Color version with the top tile in sage |
| `logo.svg` | Mark and wordmark in `currentColor` |
| `logo-on-light.svg`, `logo-on-dark.svg` | Mark and wordmark with fixed colors |
| `logo-lite.svg`, `logo-lite-on-light.svg`, `logo-lite-on-dark.svg` | The runtop lite lockup |
| `favicon.svg`, `favicon-16.png`, `favicon-32.png` | Favicon: the mark on a sage tile |
| `icon-512.png` | Avatar and app icon, the same sage tile |

Images cannot inherit `currentColor`. In a README, or anywhere the file is loaded
as an image, use the fixed-color files. For GitHub, switch them by theme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/logo-on-dark.svg">
  <img alt="runtop" src="assets/brand/logo-on-light.svg" width="286" height="64">
</picture>
```

## Construction

The mark sits in a 64-unit square. Before rotation, it is four squares of side
12.5 at x and y positions 14 and 37.5, each with corner radius 2 and a 6-unit
stroke on round joins. The top-left square is also filled. The group is then
rotated 45° about (32, 32).

Each tile measures 18.5 units across, tiles are separated by a 5-unit gap, and the
diamond spans 59.4 units. Six values rebuild the mark: 12.5, 14, 37.5, 2, 6 and 45°.

The wordmark uses the same 6-unit stroke with round caps. Its x-height runs from
19 to 45, its arches and bowls have radius 13, and the t's hook has radius 7. In
the lockup the mark occupies 0 to 64 and the word starts 68 units to the right.
The lite lockup adds the word "lite" in the same letters.

The favicon is a 64-unit sage square with corner radius 14, holding the mark at
72% scale.

## Color

| Name | Hex | Use |
| --- | --- | --- |
| Stone 900 | `#1c1917` | Dark ground |
| Stone 700 | `#44403c` | Mark and wordmark on light grounds |
| Stone 200 | `#e7e5e4` | Mark and wordmark on dark grounds |
| Stone 50 | `#fafaf9` | Light ground; mark on the sage tile |
| Deep sage | `#5f7161` | Lit tile on light grounds; favicon tile |
| Sage | `#87a087` | Lit tile on dark grounds |

Measured WCAG 2.1 contrast:

| Pair | Ratio |
| --- | --- |
| Stone 700 on Stone 50 | 9.84 |
| Stone 200 on Stone 900 | 13.93 |
| Deep sage on Stone 50 | 5.00 |
| Sage on Stone 900 | 6.17 |
| Stone 50 mark on the deep sage tile | 5.00 |

## Rules

- The one-color mark is the default. Sage appears on the favicon, the app icon and
  the lit version.
- Leave at least one tile width of clear space around the mark: 18.5 units at the
  64-unit size.
- Do not stack the mark above the wordmark. Square spaces use the mark alone.
- The smallest size is 16 px.
- In a terminal, a title bar shows `◆` as the mark's one-character form.
