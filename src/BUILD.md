# Open Runde 2.000 export

The files in this directory are generated static instances of the Open Runde
v10 rounding algorithm applied to Inter 4.001 at `opsz=14`.

## Inputs

- Upright: `InterVariable.ttf`
- Italic: `InterVariable-Italic.woff2`
- Inter source commit: `353b61b9f4430d5f420d56605a6e7993e0941470`
- Rounding model: `openrunde-reference-fit-v10-family-support-intersection-unfillet`
- Units per em: 2048

Exact input and output SHA-256 checksums, radius values, glyph counts, and QA
fallbacks are recorded in `export-manifest.json`.

## Outputs

- `desktop/`: 18 installable TrueType fonts
- `web/`: 18 matching WOFF2 fonts and `open-runde.css`

The exporter preserves composite glyphs and OpenType layout tables. It applies
rounding to simple outlines, converts accepted cubic blends back to quadratic
TrueType contours, recalculates bounds and font metrics, and renders proofs
from the finished TTF files.

Run `tools/build_v2_export.py --help` for the export interface. The reviewed
rounding core is passed explicitly with `--rounding-core` so the exact geometry
implementation used for an export is always an intentional input.
