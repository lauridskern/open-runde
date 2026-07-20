#!/usr/bin/env python3
"""Validate Open Runde 2.000 static release artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

from fontTools.ttLib import TTFont
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "src" / "desktop"
WEB = ROOT / "src" / "web"
PROOFS = ROOT / "proofs" / "v2"
WINDING_PROOF = PROOFS / "diagnostics" / "winding-overlaps.png"
TERMINAL_PROOF = PROOFS / "diagnostics" / "heavy-terminals.png"
ALGORITHM = "openrunde-reference-fit-v11-family-support-intersection-unfillet"
TANGENT_SOLVER_TIERS = (
    "locked",
    "joint",
    "sparse-1",
    "sparse-2",
    "sparse-4",
    "sparse-5",
    "sparse-6",
    "full-4",
    "sparse-7",
)
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
PROOF_CHARACTERS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ".,:;!?…‘’“”\"'-–—_/\\|@#&%*+−=<>$€£¥¢₽₹₩©®™°•·()[]{}‹›«»^~"
    "←↑→↓✓ÀÁÂÃÄÅÆÇÐÈÉÊËÌÍÎÏÑÒÓÔÕÖØŒŠÞÜÝŽ"
    "àáâãäåæçðèéêëìíîïñòóôõöøœßšþüýÿžƴʂ"
)
PROOF_LINES: Tuple[str, ...] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789  0123456789",
    ". , : ; ! ? …  ' \" ‘ ’ “ ”  - – —  _ / \\ |  @ # & % * + − = < >",
    "$ € £ ¥ ¢ ₽ ₹ ₩   © ® ™   ° • ·",
    "( )  [ ]  { }   ‹ ›  « »   ^ ~   ← ↑ → ↓   ✓",
    "À Á Â Ã Ä Å Æ Ç Ð È É Ê Ë Ì Í Î Ï Ñ Ò Ó Ô Õ Ö Ø Œ Š Þ Ü Ý Ž",
    "à á â ã ä å æ ç ð è é ê ë ì í î ï ñ ò ó ô õ ö ø œ ß š þ ü ý ÿ ž",
    "AMNVWXYZ QRGJK   rfkgzxwvy ƴ ʂ   4 7 6 9",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def family_input_sha256(records: List[Dict[str, object]]) -> str:
    payload = [
        {
            "style": record.get("style"),
            "ttf_sha256": record.get("ttf_sha256"),
            "source_sha256": record.get("source_sha256"),
            "proof_sha256": record.get("proof_sha256"),
        }
        for record in records
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def face_names(weight: int, weight_name: str, italic: bool) -> Dict[str, str]:
    if weight == 400:
        actual = "Italic" if italic else "Regular"
        ps_style = "Italic" if italic else "Regular"
    else:
        actual = f"{weight_name} Italic" if italic else weight_name
        ps_style = f"{weight_name}Italic" if italic else weight_name
    if weight in (400, 700):
        legacy_family = "Open Runde"
        legacy_style = (
            "Bold Italic"
            if weight == 700 and italic
            else "Bold"
            if weight == 700
            else "Italic"
            if italic
            else "Regular"
        )
    else:
        legacy_family = f"Open Runde {weight_name}"
        legacy_style = "Italic" if italic else "Regular"
    return {
        "actual": actual,
        "ps": f"OpenRunde-{ps_style}",
        "legacy_family": legacy_family,
        "legacy_style": legacy_style,
    }


def expect(condition: bool, message: str, errors: List[str]) -> None:
    if not condition:
        errors.append(message)


def dominant_contour_area(font: TTFont, glyph_name: str) -> float:
    """Return the signed area of a simple glyph's largest contour."""
    glyph = font["glyf"][glyph_name]
    coordinates, end_points, _ = glyph.getCoordinates(font["glyf"])
    start = 0
    areas: List[float] = []
    for end in end_points:
        points = coordinates[start : end + 1]
        area = sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        ) / 2.0
        areas.append(area)
        start = end + 1
    return max(areas, key=abs)


def raw_join_angle(coordinates, previous: int, point: int, following: int) -> float:
    incoming = (
        coordinates[point][0] - coordinates[previous][0],
        coordinates[point][1] - coordinates[previous][1],
    )
    outgoing = (
        coordinates[following][0] - coordinates[point][0],
        coordinates[following][1] - coordinates[point][1],
    )
    incoming_length = math.hypot(*incoming)
    outgoing_length = math.hypot(*outgoing)
    if incoming_length < 1.0 or outgoing_length < 1.0:
        return math.inf
    return math.degrees(
        math.atan2(
            abs(incoming[0] * outgoing[1] - incoming[1] * outgoing[0]),
            incoming[0] * outgoing[0] + incoming[1] * outgoing[1],
        )
    )


def main() -> int:
    errors: List[str] = []
    manifest = json.loads((ROOT / "src" / "export-manifest.json").read_text())
    raw_manifest_faces = manifest.get("faces", [])
    expected_styles = [
        face_names(weight, weight_name, italic)["actual"]
        for italic in (False, True)
        for weight, weight_name, _ in WEIGHTS
    ]
    manifest_styles = [face.get("style") for face in raw_manifest_faces]
    expect(manifest.get("family") == "Open Runde", "manifest family", errors)
    expect(manifest.get("version") == "2.000", "manifest version", errors)
    expect(manifest.get("algorithm") == ALGORITHM, "manifest algorithm", errors)
    expect(manifest.get("optical_size") == 14, "manifest optical size", errors)
    expect(len(raw_manifest_faces) == 18, "manifest face count", errors)
    expect(len(set(manifest_styles)) == 18, "manifest styles are unique", errors)
    expect(manifest_styles == expected_styles, "manifest face order/styles", errors)
    manifest_faces = {
        face["style"]: face
        for face in raw_manifest_faces
        if isinstance(face, dict) and isinstance(face.get("style"), str)
    }
    fingerprints = manifest.get("build_fingerprints", {})
    expected_builder_sha = sha256(ROOT / "tools" / "build_v2_export.py")
    expected_tangent_sha = sha256(ROOT / "tools" / "tangent_quantization.py")
    expect(
        fingerprints.get("builder_sha256") == expected_builder_sha,
        "manifest builder fingerprint",
        errors,
    )
    expect(
        fingerprints.get("tangent_quantization_sha256") == expected_tangent_sha,
        "manifest tangent solver fingerprint",
        errors,
    )
    expect(
        isinstance(fingerprints.get("rounding_core_sha256"), str)
        and len(fingerprints["rounding_core_sha256"]) == 64,
        "manifest rounding-core fingerprint",
        errors,
    )
    expected_proof_text_sha = hashlib.sha256(
        "\n".join(PROOF_LINES).encode("utf-8")
    ).hexdigest()
    expect(
        manifest.get("proof_text_sha256") == expected_proof_text_sha,
        "manifest proof text fingerprint",
        errors,
    )
    expected_ps: List[str] = []
    roman_orders: List[List[str]] = []
    italic_orders: List[List[str]] = []
    source_hashes = {False: set(), True: set()}

    for italic in (False, True):
        for weight, weight_name, panose_weight in WEIGHTS:
            names = face_names(weight, weight_name, italic)
            expected_ps.append(names["ps"])
            ttf_path = DESKTOP / f"{names['ps']}.ttf"
            web_path = WEB / f"{names['ps']}.woff2"
            proof_path = PROOFS / f"{names['ps']}.png"
            expect(ttf_path.exists(), f"missing {ttf_path}", errors)
            expect(web_path.exists(), f"missing {web_path}", errors)
            expect(proof_path.exists(), f"missing {proof_path}", errors)
            if not ttf_path.exists() or not web_path.exists():
                continue

            font = TTFont(ttf_path, recalcTimestamp=False)
            web = TTFont(web_path, recalcTimestamp=False)
            name = font["name"]
            os2 = font["OS/2"]
            expected_selection = (
                0x00A1
                if italic and weight == 700
                else 0x0081
                if italic
                else 0x00A0
                if weight == 700
                else 0x00C0
            )
            expected_mac_style = (1 if weight == 700 else 0) | (2 if italic else 0)
            expect(name.getDebugName(0).startswith("Copyright 2016 The Inter Project Authors"), f"{ttf_path.name}: copyright", errors)
            expect("Laurids Kern" in name.getDebugName(0), f"{ttf_path.name}: modifier copyright", errors)
            expect(name.getDebugName(1) == names["legacy_family"], f"{ttf_path.name}: name ID 1", errors)
            expect(name.getDebugName(2) == names["legacy_style"], f"{ttf_path.name}: name ID 2", errors)
            expect(name.getDebugName(4) == f"Open Runde {names['actual']}", f"{ttf_path.name}: name ID 4", errors)
            expect(name.getDebugName(5) == "Version 2.000", f"{ttf_path.name}: version name", errors)
            expect(name.getDebugName(6) == names["ps"], f"{ttf_path.name}: PostScript name", errors)
            expect(name.getDebugName(7) is None, f"{ttf_path.name}: unexpected trademark", errors)
            expect(name.getDebugName(13) is not None and "SIL Open Font License" in name.getDebugName(13), f"{ttf_path.name}: license", errors)
            expect(name.getDebugName(14) == "https://openfontlicense.org", f"{ttf_path.name}: license URL", errors)
            expect(name.getDebugName(16) == "Open Runde", f"{ttf_path.name}: typographic family", errors)
            expect(name.getDebugName(17) == names["actual"], f"{ttf_path.name}: typographic style", errors)
            expect(os2.usWeightClass == weight, f"{ttf_path.name}: weight class", errors)
            expect(os2.usWidthClass == 5, f"{ttf_path.name}: width class", errors)
            expect(os2.fsType == 0, f"{ttf_path.name}: embedding flags", errors)
            expect(os2.fsSelection == expected_selection, f"{ttf_path.name}: selection flags", errors)
            expect(os2.achVendID == "    ", f"{ttf_path.name}: vendor ID", errors)
            expect(os2.panose.bWeight == panose_weight, f"{ttf_path.name}: PANOSE weight", errors)
            expect(font["head"].fontRevision == 2.0, f"{ttf_path.name}: head version", errors)
            expect(font["head"].macStyle == expected_mac_style, f"{ttf_path.name}: macStyle", errors)
            expect(abs(font["post"].italicAngle - (-9.4 if italic else 0.0)) < 0.01, f"{ttf_path.name}: italic angle", errors)
            expect(font["hhea"].caretSlopeRun == (339 if italic else 0), f"{ttf_path.name}: caret slope", errors)
            expect(all(tag in font for tag in ("GDEF", "GPOS", "GSUB", "STAT")), f"{ttf_path.name}: layout/STAT tables", errors)
            expect(not any(tag in font for tag in ("fvar", "gvar", "avar", "HVAR", "MVAR")), f"{ttf_path.name}: static font has variation tables", errors)
            expect(len(font.getGlyphOrder()) == (2901 if italic else 2937), f"{ttf_path.name}: glyph count", errors)
            non_clockwise = [
                glyph_name
                for glyph_name in font.getGlyphOrder()
                if not font["glyf"][glyph_name].isComposite()
                and font["glyf"][glyph_name].numberOfContours > 0
                and dominant_contour_area(font, glyph_name) >= 0
            ]
            expect(
                not non_clockwise,
                f"{ttf_path.name}: non-clockwise outer contours {non_clockwise[:8]}",
                errors,
            )
            expect(PROOF_CHARACTERS.issubset({chr(codepoint) for codepoint in font.getBestCmap()}), f"{ttf_path.name}: proof coverage", errors)
            expect(web.flavor == "woff2", f"{web_path.name}: flavor", errors)
            expect(web["name"].getDebugName(6) == names["ps"], f"{web_path.name}: PostScript name", errors)
            expect(web.getGlyphOrder() == font.getGlyphOrder(), f"{web_path.name}: glyph order", errors)
            expect(set(web.keys()) == set(font.keys()), f"{web_path.name}: table parity", errors)
            if proof_path.exists():
                with Image.open(proof_path) as proof:
                    expect(proof.size == (2600, 1740), f"{proof_path.name}: proof dimensions", errors)
                    expect(proof.mode == "RGB", f"{proof_path.name}: proof color mode", errors)

            output = io.BytesIO()
            font.save(output)
            expect(len(output.getvalue()) > 300_000, f"{ttf_path.name}: recompilation", errors)
            (italic_orders if italic else roman_orders).append(font.getGlyphOrder())

            record = manifest_faces.get(names["actual"])
            expect(record is not None, f"{ttf_path.name}: manifest record", errors)
            if record:
                expect(record.get("weight") == weight, f"{ttf_path.name}: manifest weight", errors)
                expected_source = "InterVariable-Italic.woff2" if italic else "InterVariable.ttf"
                expect(record.get("source") == expected_source, f"{ttf_path.name}: source label", errors)
                source_hash = record.get("source_sha256")
                expect(isinstance(source_hash, str) and len(source_hash) == 64, f"{ttf_path.name}: source SHA", errors)
                if isinstance(source_hash, str):
                    source_hashes[italic].add(source_hash)
                expect(record.get("build_fingerprints") == fingerprints, f"{ttf_path.name}: build fingerprints", errors)
                expect(record.get("ttf") == f"src/desktop/{ttf_path.name}", f"{ttf_path.name}: manifest TTF path", errors)
                expect(record.get("woff2") == f"src/web/{web_path.name}", f"{web_path.name}: manifest WOFF2 path", errors)
                expect(record.get("proof") == f"proofs/v2/{proof_path.name}", f"{proof_path.name}: manifest proof path", errors)
                expect(record.get("ttf_sha256") == sha256(ttf_path), f"{ttf_path.name}: manifest SHA", errors)
                expect(record.get("woff2_sha256") == sha256(web_path), f"{web_path.name}: manifest SHA", errors)
                if proof_path.exists():
                    expect(record.get("proof_sha256") == sha256(proof_path), f"{proof_path.name}: manifest SHA", errors)
                expect(record.get("tangent_conversion_failures") == 0, f"{ttf_path.name}: tangent conversion failures", errors)
                safe_fallbacks = record.get(
                    "conversion_safe_fallback_glyphs", []
                )
                expect(
                    isinstance(safe_fallbacks, list)
                    and len(safe_fallbacks) == len(set(safe_fallbacks))
                    and all(
                        isinstance(glyph_name, str)
                        and glyph_name in font["glyf"]
                        for glyph_name in safe_fallbacks
                    ),
                    f"{ttf_path.name}: conversion-safe fallback records",
                    errors,
                )
                expect(record.get("tangent_curve_join_candidates", 0) > 0, f"{ttf_path.name}: tangent coverage is empty", errors)
                expect(
                    record.get("tangent_joins_checked", -1)
                    + record.get("tangent_lattice_degenerate_joins", -1)
                    == record.get("tangent_curve_join_candidates"),
                    f"{ttf_path.name}: tangent coverage accounting",
                    errors,
                )
                expect(record.get("tangent_exact_lattice_reductions", -1) >= 0, f"{ttf_path.name}: exact lattice reduction accounting", errors)
                expect(record.get("tangent_lattice_degenerate_joins", -1) >= 0, f"{ttf_path.name}: lattice-degenerate join accounting", errors)
                degenerate_records = record.get(
                    "tangent_lattice_degenerate_join_records", {}
                )
                expect(
                    isinstance(degenerate_records, dict),
                    f"{ttf_path.name}: lattice-degenerate records",
                    errors,
                )
                valid_degenerate_count = 0
                if isinstance(degenerate_records, dict):
                    for glyph_name, triples in degenerate_records.items():
                        glyph_valid = glyph_name in font["glyf"]
                        expect(glyph_valid, f"{ttf_path.name}: unknown lattice-degenerate glyph {glyph_name}", errors)
                        if not glyph_valid:
                            continue
                        glyph = font["glyf"][glyph_name]
                        expect(not glyph.isComposite(), f"{ttf_path.name}: composite lattice-degenerate glyph {glyph_name}", errors)
                        if glyph.isComposite():
                            continue
                        coordinates, end_points, flags = glyph.getCoordinates(font["glyf"])
                        neighbors = {}
                        contour_start = 0
                        for contour_end in end_points:
                            contour_size = contour_end - contour_start + 1
                            for point_index in range(contour_start, contour_end + 1):
                                offset = point_index - contour_start
                                neighbors[point_index] = (
                                    contour_start + (offset - 1) % contour_size,
                                    contour_start + (offset + 1) % contour_size,
                                )
                            contour_start = contour_end + 1
                        if not isinstance(triples, list):
                            expect(False, f"{ttf_path.name}: invalid lattice-degenerate records for {glyph_name}", errors)
                            continue
                        for triple in triples:
                            valid = (
                                isinstance(triple, list)
                                and len(triple) == 3
                                and all(type(index) is int for index in triple)
                            )
                            if valid:
                                previous, point, following = triple
                                valid = (
                                    min(triple) >= 0
                                    and max(triple) < len(coordinates)
                                    and bool(flags[point] & 1)
                                    and neighbors.get(point) == (previous, following)
                                    and (
                                        coordinates[previous] == coordinates[point]
                                        or coordinates[point] == coordinates[following]
                                    )
                                )
                            expect(valid, f"{ttf_path.name}: invalid lattice-degenerate join {glyph_name} {triple}", errors)
                            valid_degenerate_count += int(valid)
                expect(
                    valid_degenerate_count
                    == record.get("tangent_lattice_degenerate_joins"),
                    f"{ttf_path.name}: lattice-degenerate record count",
                    errors,
                )
                expect(record.get("max_smooth_join_angle", 180) <= 0.600001, f"{ttf_path.name}: smooth-join angle", errors)
                expect(record.get("max_tangent_direction_shift", 180) <= 45.000001, f"{ttf_path.name}: tangent direction shift", errors)
                expect(record.get("max_tangent_regular_direction_shift", 180) <= 6.000001, f"{ttf_path.name}: regular-handle direction shift", errors)
                expect(record.get("max_tangent_micro_direction_shift", 180) <= 45.000001, f"{ttf_path.name}: micro-handle direction shift", errors)
                expect(record.get("max_tangent_on_curve_move", 180) <= 7.000001, f"{ttf_path.name}: on-curve tangent movement", errors)
                expect(record.get("max_tangent_control_move", 180) <= 9.000001, f"{ttf_path.name}: tangent control movement", errors)
                solver_tiers = record.get("tangent_solver_tiers", {})
                expect(
                    sum(solver_tiers.get(tier, -10_000) for tier in TANGENT_SOLVER_TIERS)
                    == record.get("glyphs_changed"),
                    f"{ttf_path.name}: tangent solver-tier accounting",
                    errors,
                )
                regression = record.get("tangent_regression_joins", {})
                expect(set(regression) == {"e", "c", "s"}, f"{ttf_path.name}: tangent regression coverage", errors)
                for character in ("e", "c", "s"):
                    triples = regression.get(character, [])
                    glyph_name = font.getBestCmap()[ord(character)]
                    coordinates, end_points, flags = font["glyf"][glyph_name].getCoordinates(font["glyf"])
                    neighbors = {}
                    contour_start = 0
                    for contour_end in end_points:
                        contour_size = contour_end - contour_start + 1
                        for index in range(contour_start, contour_end + 1):
                            offset = index - contour_start
                            neighbors[index] = (
                                contour_start + (offset - 1) % contour_size,
                                contour_start + (offset + 1) % contour_size,
                            )
                        contour_start = contour_end + 1
                    valid_triples = [
                        (previous, point, following)
                        for previous, point, following in triples
                        if min(previous, point, following) >= 0
                        and max(previous, point, following) < len(coordinates)
                        and flags[point] & 1
                        and neighbors.get(point) == (previous, following)
                    ]
                    angles = [
                        raw_join_angle(coordinates, previous, point, following)
                        for previous, point, following in valid_triples
                    ]
                    expect(len(angles) == len(triples) and bool(angles), f"{ttf_path.name}: {character} raw tangent indices", errors)
                    expect(max(angles, default=math.inf) <= 0.600001, f"{ttf_path.name}: {character} raw tangent angle", errors)

    expect(all(order == roman_orders[0] for order in roman_orders[1:]), "upright glyph orders differ", errors)
    expect(all(order == italic_orders[0] for order in italic_orders[1:]), "italic glyph orders differ", errors)
    expect(len(source_hashes[False]) == 1, "upright source hashes differ", errors)
    expect(len(source_hashes[True]) == 1, "italic source hashes differ", errors)
    expected_ttf_files = {f"{postscript}.ttf" for postscript in expected_ps}
    expected_woff2_files = {f"{postscript}.woff2" for postscript in expected_ps}
    expected_proof_files = {f"{postscript}.png" for postscript in expected_ps}
    expect(
        {path.name for path in DESKTOP.iterdir() if path.is_file()}
        == expected_ttf_files,
        "desktop release file set",
        errors,
    )
    expect(
        {path.name for path in WEB.iterdir() if path.is_file()}
        == expected_woff2_files | {"open-runde.css"},
        "web release file set",
        errors,
    )
    expect(
        {path.name for path in PROOFS.iterdir() if path.is_file()}
        == expected_proof_files,
        "proof file set",
        errors,
    )
    expected_family_fingerprint = family_input_sha256(raw_manifest_faces)
    expect(
        manifest.get("family_input_sha256") == expected_family_fingerprint,
        "manifest family-input fingerprint",
        errors,
    )
    expected_diagnostic_names = {"winding-overlaps.png", "heavy-terminals.png"}
    diagnostics = manifest.get("diagnostics", {})
    expect(isinstance(diagnostics, dict), "manifest diagnostics object", errors)
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    expect(set(diagnostics) == expected_diagnostic_names, "manifest diagnostics", errors)
    diagnostic_files = {
        path.name for path in (PROOFS / "diagnostics").glob("*") if path.is_file()
    }
    expect(diagnostic_files == expected_diagnostic_names, "diagnostic proof file set", errors)
    for filename in expected_diagnostic_names:
        path = PROOFS / "diagnostics" / filename
        record = diagnostics.get(filename, {})
        expect(record.get("path") == f"proofs/v2/diagnostics/{filename}", f"{filename}: manifest path", errors)
        if path.exists():
            expect(record.get("sha256") == sha256(path), f"{filename}: manifest SHA", errors)
        expect(record.get("family_input_sha256") == expected_family_fingerprint, f"{filename}: family-input fingerprint", errors)
    expect(WINDING_PROOF.exists(), "missing focused winding proof", errors)
    if WINDING_PROOF.exists():
        with Image.open(WINDING_PROOF) as proof:
            expect(proof.size == (2600, 2500), "focused winding proof dimensions", errors)
            expect(proof.mode == "RGB", "focused winding proof color mode", errors)
    expect(TERMINAL_PROOF.exists(), "missing focused terminal proof", errors)
    if TERMINAL_PROOF.exists():
        with Image.open(TERMINAL_PROOF) as proof:
            expect(proof.size == (2600, 1900), "focused terminal proof dimensions", errors)
            expect(proof.mode == "RGB", "focused terminal proof color mode", errors)
    expect(not (ROOT / "src" / "glyphs").exists(), "obsolete glyphs directory remains", errors)
    css = (WEB / "open-runde.css").read_text()
    expect(css.count("@font-face") == 18, "CSS face count", errors)
    for postscript in expected_ps:
        expect(f'{postscript}.woff2' in css, f"CSS missing {postscript}", errors)

    if errors:
        print(f"FAILED: {len(errors)} issue(s)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PASS: 18 TTFs, 18 WOFF2 files, and 18 proofs validated")
    print("PASS: metadata, style linking, winding, coverage, tables, hashes, proofs, and CSS validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
