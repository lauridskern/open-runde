#!/usr/bin/env python3
"""Validate Open Runde 2.000 static release artifacts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Dict, List, Tuple

from fontTools.ttLib import TTFont
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "src" / "desktop"
WEB = ROOT / "src" / "web"
PROOFS = ROOT / "proofs" / "v2"
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
    "àáâãäåæçðèéêëìíîïñòóôõöøœßšþüýÿž"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


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


def main() -> int:
    errors: List[str] = []
    manifest = json.loads((ROOT / "src" / "export-manifest.json").read_text())
    manifest_faces = {face["style"]: face for face in manifest["faces"]}
    expected_ps: List[str] = []
    roman_orders: List[List[str]] = []
    italic_orders: List[List[str]] = []

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
                expect(record["ttf_sha256"] == sha256(ttf_path), f"{ttf_path.name}: manifest SHA", errors)
                expect(record["woff2_sha256"] == sha256(web_path), f"{web_path.name}: manifest SHA", errors)

    expect(all(order == roman_orders[0] for order in roman_orders[1:]), "upright glyph orders differ", errors)
    expect(all(order == italic_orders[0] for order in italic_orders[1:]), "italic glyph orders differ", errors)
    expect(len(list(DESKTOP.glob("*.ttf"))) == 18, "desktop TTF count", errors)
    expect(not list(DESKTOP.glob("*.otf")), "obsolete OTF files remain", errors)
    expect(len(list(WEB.glob("*.woff2"))) == 18, "web WOFF2 count", errors)
    expect(not list(WEB.glob("*.woff")), "obsolete WOFF1 files remain", errors)
    expect(len(list(PROOFS.glob("*.png"))) == 18, "proof count", errors)
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
    print("PASS: metadata, style linking, coverage, tables, hashes, and CSS validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
