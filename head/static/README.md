# head/static

The front end: nine static pages, two stylesheets, plain ES modules, vendored uPlot, the self-hosted font.
No build step, no framework, no request leaves the origin.

| path | what |
|---|---|
| `*.html` | one page per feature, served at clean paths (`/`, `/stir`, `/metrics`, ...) by `porthole/routes/pages.py` |
| `css/skin.css` | the kraken tokens from `watcher/skin/kraken.skin.json` as custom properties (a test keeps them equal) |
| `css/porthole.css` | layout and components, and the color slot map below |
| `js/porthole.js` | shared: frame, region selector, captain's key dialog, API drawer, fetch, SSE, DOM helpers |
| `js/charts.js` | the uPlot wrapper: alignment, entity colors, tooltip, legend, table twin, units |
| `js/pages/*.js` | one module per page |
| `vendor/uplot/` | uPlot 1.6.32 with its LICENSE and `VENDOR.md` (hashes) |

## Palette validator

Run on 2026-10-09 with the dataviz skill's `validate_palette.py` (the JS twin in Node 22 gave the same numbers),
dark mode, against the card surface `#081f1e`:

```
python validate_palette.py "<eight colors>" --mode dark --surface "#081f1e"
```

The skin's fleet colors in file order (`#30cbd9,#b17ea4,#e2b658,#5a9c63,#5198fb,#d8907e,#9ba3a4,#a6b559`):

| check | result |
|---|---|
| lightness band (0.48 to 0.67) | FAIL: 6 of 8 colors are lighter than the band (L 0.68 to 0.80) |
| chroma floor (0.10) | FAIL: `#b17ea4` 0.081, `#d8907e` 0.092, `#9ba3a4` 0.009 (reads gray) |
| CVD separation, adjacent | FAIL: `#9ba3a4` and `#d8907e` ΔE 5.4 (protan) |
| normal-vision floor, adjacent | FAIL: `#9ba3a4` and `#d8907e` ΔE 10.2 |
| contrast against the surface | PASS: all 8 at 3:1 or more |

The design allows one fix without changing a hex value: re-order the slots. Every order was scored with the
validator's own color math, keeping tentacle-1 cyan and counting all three tentacle pairs, because the
tentacles share the water chart. Three orders pass both adjacent checks; the one used here also keeps the gold
`#e2b658` (ΔE 2.8 from the attention gold `#deab4f`) away from the members that show up most:

| check | result with the slot order below |
|---|---|
| lightness band | FAIL (unchanged; needs hex changes) |
| chroma floor | FAIL (unchanged; needs hex changes) |
| CVD separation, adjacent | PASS: worst `#5a9c63` and `#b17ea4` ΔE 8.3 (deutan) |
| normal-vision floor, adjacent | PASS: worst `#9ba3a4` and `#e2b658` ΔE 15.5 |
| contrast against the surface | PASS |

Lightness and chroma stay failed because fixing them means new hex values in the shared kraken skin, which is a
decision for whoever owns the skin. Until then every chart carries the secondary channels the validator asks for:
a legend for two or more series, names in the tooltip, and the table view.

## Slot order

`skin.css` stays equal to the skin file. Slots point at its colors through `--slot-N` in `css/porthole.css`;
the same order is `SLOT_PALETTE` in `porthole/config.py` and `palette` in `/api/config`.

| slot | fleet member | skin color | hex |
|---|---|---|---|
| 1 | tentacle-1 | `--fleet-1` | `#30cbd9` |
| 2 | tentacle-2 | `--fleet-6` | `#d8907e` |
| 3 | tentacle-3 | `--fleet-5` | `#5198fb` |
| 4 | load balancer | `--fleet-8` | `#a6b559` |
| 5 | head | `--fleet-2` | `#b17ea4` |
| 6 | database | `--fleet-4` | `#5a9c63` |
| 7 | kubernetes | `--fleet-3` | `#e2b658` |
| 8 | function | `--fleet-7` | `#9ba3a4` |

Series whose `resource_urn` is not in the fleet are drawn in `--dim` and labelled with their `resource_name` when
they have one. Members without a URN in the fleet description (the Function namespace, the Spaces bucket) are matched
by `resource_name`. A chart never draws more than 8 series; the
rest fold into "+N more (table view)".
