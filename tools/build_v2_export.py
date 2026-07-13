#!/usr/bin/env python3
"""Build the Open Runde 2.000 static family and release proofs.

The geometry implementation is supplied with ``--rounding-core`` so this
exporter can use the reviewed algorithm verbatim while keeping font writing,
metadata, and proof generation reproducible in this repository.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import hashlib
import importlib.util
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from fontTools.otlLib.builder import buildStatTable
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont


VERSION = "2.000"
RELEASE_DATE = datetime(2026, 7, 13, tzinfo=timezone.utc)
MAC_EPOCH_OFFSET = 2082844800
WEIGHTS: Tuple[Tuple[int, str, int], ...] = (
    (100, "Thin", 2),
    (200, "ExtraLight", 3),
    (300, "Light", 4),
    (400, "Regular", 5),
    (500, "Medium", 6),
    (600, "SemiBold", 7),
    (700, "Bold", 8),
    (800, "ExtraBold", 9),
    (900, "Black", 10),
)
PROOF_LINES: Tuple[str, ...] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789  0123456789",
    ". , : ; ! ? …  ‘ ’ “ ”  - – —  _ / \\ |  @ # & % * + − = < >",
    "$ € £ ¥ ¢ ₽ ₹ ₩   © ® ™   ° • ·",
    "( )  [ ]  { }   ‹ ›  « »   ^ ~   ← ↑ → ↓   ✓",
    "À Á Â Ã Ä Å Æ Ç Ð È É Ê Ë Ì Í Î Ï Ñ Ò Ó Ô Õ Ö Ø Œ Š Þ Ü Ý Ž",
    "à á â ã ä å æ ç ð è é ê ë ì í î ï ñ ò ó ô õ ö ø œ ß š þ ü ý ÿ ž",
    "AMNVWXYZ QRGJK   rfkgzxwvy   4 7 6 9",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounding-core", type=Path, required=True)
    parser.add_argument("--roman-font", type=Path, required=True)
    parser.add_argument("--italic-font", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("src"))
    parser.add_argument("--proof-root", type=Path, default=Path("proofs/v2"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument(
        "--style",
        choices=("all", "upright", "italic"),
        default="all",
        help="Build the complete family or only one style half.",
    )
    return parser.parse_args()


def load_rounding_core(path: Path):
    spec = importlib.util.spec_from_file_location("openrunde_rounding_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load rounding core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scanline_intervals(infos, y: float) -> List[float]:
    widths: List[float] = []
    for info in infos:
        intersections: List[float] = []
        for start, end in zip(info.polygon, info.polygon[1:]):
            if (start[1] <= y < end[1]) or (end[1] <= y < start[1]):
                t = (y - start[1]) / (end[1] - start[1])
                intersections.append(start[0] + t * (end[0] - start[0]))
        intersections.sort()
        widths.extend(
            intersections[index + 1] - intersections[index]
            for index in range(0, len(intersections) - 1, 2)
        )
    return [width for width in widths if width > 0]


def measure_h_stem(core, font: TTFont) -> float:
    glyph_name = font.getBestCmap()[ord("H")]
    infos = core.collect_path(core.glyph_to_path(font.getGlyphSet(), glyph_name))
    bounds = core.bounds_of_infos(infos)
    y = bounds[1] + 0.78 * (bounds[3] - bounds[1])
    stems = sorted(scanline_intervals(infos, y))[:2]
    if len(stems) != 2:
        raise RuntimeError("Could not measure both H stems")
    return sum(stems) / 2.0


def optical_boost(weight: int) -> float:
    if weight <= 600:
        t = max(0.0, min(1.0, (600.0 - weight) / 500.0))
        compensation = 0.02 + (0.18 - 0.02) * t**1.25
    else:
        t = max(0.0, min(1.0, (weight - 600.0) / 300.0))
        compensation = 0.02 + (0.05 - 0.02) * t**1.30
    return 1.0 + compensation


def low_weight_addition(weight: int) -> float:
    if weight >= 600:
        return 0.0
    t = max(0.0, min(1.0, (600.0 - weight) / 500.0))
    return 16.0 * t**1.15


def radius_for(weight: int, stem: float, base_stem: float) -> float:
    return 74.0 * stem / base_stem * optical_boost(weight) + low_weight_addition(weight)


def contour_infos_to_glyph(infos) -> object:
    target = TTGlyphPen(None)
    pen = Cu2QuPen(target, max_err=0.75, reverse_direction=False)
    for info in infos:
        if not info.segments:
            continue
        pen.moveTo(info.segments[0].start)
        for segment in info.segments:
            if segment.kind == "line":
                pen.lineTo(segment.end)
            elif segment.kind == "quad":
                pen.qCurveTo(segment.points[1], segment.points[2])
            elif segment.kind == "cubic":
                pen.curveTo(segment.points[1], segment.points[2], segment.points[3])
            else:
                raise ValueError(f"Unsupported segment kind: {segment.kind}")
        pen.closePath()
    return target.glyph()


def set_name(font: TTFont, name_id: int, value: str) -> None:
    table = font["name"]
    table.setName(value, name_id, 3, 1, 0x409)
    table.setName(value, name_id, 1, 0, 0)


def face_names(weight: int, weight_name: str, italic: bool) -> Dict[str, str]:
    if weight == 400:
        actual_style = "Italic" if italic else "Regular"
        postscript_style = "Italic" if italic else "Regular"
    else:
        actual_style = f"{weight_name} Italic" if italic else weight_name
        postscript_style = f"{weight_name}Italic" if italic else weight_name

    if weight in (400, 700):
        legacy_family = "Open Runde"
        if weight == 700:
            legacy_style = "Bold Italic" if italic else "Bold"
        else:
            legacy_style = "Italic" if italic else "Regular"
    else:
        legacy_family = f"Open Runde {weight_name}"
        legacy_style = "Italic" if italic else "Regular"

    postscript = f"OpenRunde-{postscript_style}"
    return {
        "legacy_family": legacy_family,
        "legacy_style": legacy_style,
        "actual_style": actual_style,
        "full_name": f"Open Runde {actual_style}",
        "postscript": postscript,
    }


def update_metadata(
    font: TTFont,
    weight: int,
    weight_name: str,
    panose_weight: int,
    italic: bool,
) -> Dict[str, str]:
    names = face_names(weight, weight_name, italic)
    font["name"].names = [record for record in font["name"].names if record.nameID > 25]
    values = {
        0: (
            "Copyright 2016 The Inter Project Authors "
            "(https://github.com/rsms/inter). "
            "Modifications copyright 2023-2026 Laurids Kern."
        ),
        1: names["legacy_family"],
        2: names["legacy_style"],
        3: f"{VERSION};LauridsKern;{names['postscript']}",
        4: names["full_name"],
        5: f"Version {VERSION}",
        6: names["postscript"],
        8: "Laurids Kern",
        9: "Rasmus Andersson; rounded adaptation by Laurids Kern",
        10: "Open Runde is a rounded derivative of Inter.",
        11: "https://lau.ke",
        12: "https://rsms.me",
        13: "This Font Software is licensed under the SIL Open Font License, Version 1.1.",
        14: "https://openfontlicense.org",
        16: "Open Runde",
        17: names["actual_style"],
        19: "Open Runde Aa Bb Cc 0123",
    }
    for name_id, value in values.items():
        set_name(font, name_id, value)

    os2 = font["OS/2"]
    os2.usWeightClass = weight
    os2.usWidthClass = 5
    os2.fsType = 0
    if italic:
        os2.fsSelection = 0x00A1 if weight == 700 else 0x0081
    else:
        os2.fsSelection = 0x00A0 if weight == 700 else 0x00C0
    os2.achVendID = "    "
    os2.panose.bWeight = panose_weight

    head = font["head"]
    head.fontRevision = 2.0
    head.macStyle = (1 if weight == 700 else 0) | (2 if italic else 0)
    timestamp = calendar.timegm(RELEASE_DATE.utctimetuple()) + MAC_EPOCH_OFFSET
    head.created = timestamp
    head.modified = timestamp

    axes = [
        {
            "tag": "wght",
            "name": "Weight",
            "ordering": 0,
            "values": [
                {
                    "value": weight,
                    "name": weight_name,
                    **({"flags": 0x2, "linkedValue": 700} if weight == 400 else {}),
                }
            ],
        },
        {
            "tag": "ital",
            "name": "Italic",
            "ordering": 1,
            "values": [
                (
                    {"value": 1, "name": "Italic"}
                    if italic
                    else {"value": 0, "name": "Roman", "flags": 0x2, "linkedValue": 1}
                )
            ],
        },
    ]
    buildStatTable(font, axes, elidedFallbackName="Regular")
    os2.recalcAvgCharWidth(font)
    os2.recalcUnicodeRanges(font)
    os2.recalcCodePageRanges(font)
    font["name"].names.sort()
    return names


def reverse_cmap(font: TTFont) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for codepoint, glyph_name in sorted(font.getBestCmap().items()):
        result.setdefault(glyph_name, chr(codepoint))
    return result


def build_face(
    core,
    source: Path,
    output_root: Path,
    proof_root: Path,
    weight: int,
    weight_name: str,
    panose_weight: int,
    italic: bool,
    base_stem: float,
) -> Dict[str, object]:
    style_label = "Italic" if italic else "Upright"
    font = core.load_font(source, weight, 14.0)
    # The italic review source is WOFF2. Static desktop outputs must be raw
    # SFNT TrueType data regardless of the input container flavor.
    font.flavor = None
    font.recalcBBoxes = True
    font.recalcTimestamp = False
    upm = int(font["head"].unitsPerEm)
    stem = measure_h_stem(core, font)
    radius = radius_for(weight, stem, base_stem)
    glyph_set = font.getGlyphSet()
    glyph_table = font["glyf"]
    glyph_order = font.getGlyphOrder()
    character_for = reverse_cmap(font)
    changed: List[str] = []
    reverted: List[str] = []
    warnings: Dict[str, List[str]] = {}
    rounded_corners = 0
    normalized_rails = 0
    simple_count = 0

    print(
        f"Building {weight_name} {style_label}: radius={radius:.3f}, glyphs={len(glyph_order)}",
        flush=True,
    )
    for index, glyph_name in enumerate(glyph_order, 1):
        glyph = glyph_table[glyph_name]
        if glyph.isComposite() or glyph.numberOfContours <= 0:
            continue
        simple_count += 1
        original_path = core.glyph_to_path(glyph_set, glyph_name)
        result = core.round_glyph(
            character_for.get(glyph_name, glyph_name),
            glyph_name,
            float(font["hmtx"][glyph_name][0]),
            original_path,
            radius,
            8.0,
            1.0,
            2.0,
            upm,
            "openrunde-fit",
            True,
        )
        if result.reverted:
            reverted.append(glyph_name)
            if result.warnings:
                warnings[glyph_name] = list(result.warnings)
            continue
        accepted_normalizations = [
            record for record in result.normalizations if record.get("accepted")
        ]
        if result.rounded_count or accepted_normalizations:
            glyph_table[glyph_name] = contour_infos_to_glyph(result.rounded)
            changed.append(glyph_name)
            rounded_corners += result.rounded_count
            normalized_rails += sum(
                int(record.get("rail_pairs_reconstructed", 0))
                for record in accepted_normalizations
            )
        if simple_count % 250 == 0:
            print(
                f"  {weight_name} {style_label}: {index}/{len(glyph_order)}; changed={len(changed)}",
                flush=True,
            )

    names = update_metadata(font, weight, weight_name, panose_weight, italic)
    for glyph_name in glyph_order:
        glyph_table[glyph_name].recalcBounds(glyph_table)
    if hasattr(font["maxp"], "recalc"):
        font["maxp"].recalc(font)
    if hasattr(font["hhea"], "recalc"):
        font["hhea"].recalc(font)

    desktop_root = output_root / "desktop"
    web_root = output_root / "web"
    desktop_root.mkdir(parents=True, exist_ok=True)
    web_root.mkdir(parents=True, exist_ok=True)
    ttf_path = desktop_root / f"{names['postscript']}.ttf"
    woff2_path = web_root / f"{names['postscript']}.woff2"
    font.save(ttf_path, reorderTables=True)

    web_font = TTFont(ttf_path, recalcTimestamp=False)
    web_font.flavor = "woff2"
    web_font.save(woff2_path, reorderTables=True)
    render_proof(ttf_path, proof_root / f"{names['postscript']}.png", weight, weight_name, italic)

    print(
        f"Finished {weight_name} {style_label}: changed={len(changed)}, "
        f"corners={rounded_corners}, rails={normalized_rails}, reverted={len(reverted)}",
        flush=True,
    )
    return {
        "style": names["actual_style"],
        "weight": weight,
        "source": str(source),
        "source_sha256": sha256(source),
        "radius_at_90_degrees": round(radius, 6),
        "measured_H_stem": round(stem, 6),
        "glyphs": len(glyph_order),
        "simple_glyphs_processed": simple_count,
        "glyphs_changed": len(changed),
        "rounded_corners": rounded_corners,
        "normalized_rail_pairs": normalized_rails,
        "reverted_glyphs": reverted,
        "warnings": warnings,
        "ttf": str(ttf_path),
        "ttf_sha256": sha256(ttf_path),
        "woff2": str(woff2_path),
        "woff2_sha256": sha256(woff2_path),
    }


def fit_font(path: Path, text: str, maximum_size: int, maximum_width: int) -> ImageFont.FreeTypeFont:
    size = maximum_size
    while size > 24:
        font = ImageFont.truetype(str(path), size=size)
        bounds = font.getbbox(text)
        if bounds[2] - bounds[0] <= maximum_width:
            return font
        size -= 2
    return ImageFont.truetype(str(path), size=24)


def render_proof(path: Path, output: Path, weight: int, weight_name: str, italic: bool) -> None:
    width, height, supersample = 2600, 1740, 2
    canvas = Image.new("RGB", (width * supersample, height * supersample), "white")
    draw = ImageDraw.Draw(canvas)
    navy = (15, 23, 42)
    muted = (86, 96, 113)
    grid = (226, 232, 240)
    title = f"Open Runde {weight_name}{' Italic' if italic else ''}"
    title_font = ImageFont.truetype(str(path), size=66 * supersample)
    meta_font = ImageFont.truetype(str(path), size=28 * supersample)
    draw.text((70 * supersample, 42 * supersample), title, font=title_font, fill=navy)
    draw.text(
        (72 * supersample, 126 * supersample),
        f"Version {VERSION}  ·  wght {weight}  ·  {'italic' if italic else 'upright'}  ·  complete release proof",
        font=meta_font,
        fill=muted,
    )
    draw.line(
        (70 * supersample, 182 * supersample, (width - 70) * supersample, 182 * supersample),
        fill=grid,
        width=2 * supersample,
    )

    y = 218
    row_height = 158
    for line in PROOF_LINES:
        body_font = fit_font(path, line, 112 * supersample, (width - 150) * supersample)
        draw.text((74 * supersample, y * supersample), line, font=body_font, fill=navy)
        y += row_height

    canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, "PNG", optimize=True)


def write_css(output_root: Path) -> None:
    blocks: List[str] = ["/* Open Runde 2.000 static web family */"]
    for weight, weight_name, _ in WEIGHTS:
        for italic in (False, True):
            names = face_names(weight, weight_name, italic)
            blocks.extend(
                [
                    "@font-face {",
                    '  font-family: "Open Runde";',
                    f"  font-style: {'italic' if italic else 'normal'};",
                    f"  font-weight: {weight};",
                    "  font-display: swap;",
                    f"  src: url(\"{names['postscript']}.woff2\") format(\"woff2\");",
                    "}",
                    "",
                ]
            )
    (output_root / "web" / "open-runde.css").write_text("\n".join(blocks), encoding="utf-8")


def build_face_job(job: Dict[str, object]) -> Dict[str, object]:
    core = load_rounding_core(Path(str(job["rounding_core"])))
    return build_face(
        core,
        Path(str(job["source"])),
        Path(str(job["output_root"])),
        Path(str(job["proof_root"])),
        int(job["weight"]),
        str(job["weight_name"]),
        int(job["panose_weight"]),
        bool(job["italic"]),
        float(job["base_stem"]),
    )


def main() -> int:
    args = parse_args()
    core = load_rounding_core(args.rounding_core)
    for required in (args.roman_font, args.italic_font):
        if not required.exists():
            raise SystemExit(f"Missing input font: {required}")

    bold = core.load_font(args.roman_font, 700, 14.0)
    base_stem = measure_h_stem(core, bold)
    jobs: List[Dict[str, object]] = []
    style_sources = (
        ((False, args.roman_font), (True, args.italic_font))
        if args.style == "all"
        else ((False, args.roman_font),)
        if args.style == "upright"
        else ((True, args.italic_font),)
    )
    for italic, source in style_sources:
        for weight, weight_name, panose_weight in WEIGHTS:
            jobs.append(
                {
                    "rounding_core": str(args.rounding_core),
                    "source": str(source),
                    "output_root": str(args.output_root),
                    "proof_root": str(args.proof_root),
                    "weight": weight,
                    "weight_name": weight_name,
                    "panose_weight": panose_weight,
                    "italic": italic,
                    "base_stem": base_stem,
                }
            )

    worker_count = max(1, min(args.jobs, len(jobs)))
    print(f"Building {len(jobs)} faces with {worker_count} workers", flush=True)
    if worker_count == 1:
        records = [build_face_job(job) for job in jobs]
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as executor:
            records = list(executor.map(build_face_job, jobs))

    write_css(args.output_root)
    manifest_path = args.output_root / "export-manifest.json"
    if args.style != "all" and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        merged = {face["style"]: face for face in previous.get("faces", [])}
        merged.update({face["style"]: face for face in records})
        expected_styles = [
            face_names(weight, weight_name, italic)["actual_style"]
            for italic in (False, True)
            for weight, weight_name, _ in WEIGHTS
        ]
        records = [merged[style] for style in expected_styles]
    manifest = {
        "family": "Open Runde",
        "version": VERSION,
        "algorithm": "openrunde-reference-fit-v10-family-support-intersection-unfillet",
        "optical_size": 14,
        "base_bold_H_stem": round(base_stem, 6),
        "faces": records,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} faces and {len(records)} proofs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
