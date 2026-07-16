#!/usr/bin/env python3
"""Build the Open Runde 2.000 static family and release proofs.

The geometry implementation is supplied with ``--rounding-core`` so this
exporter can use the reviewed algorithm verbatim while keeping font writing,
metadata, and proof generation reproducible in this repository.
"""

from __future__ import annotations

import argparse
import atexit
import calendar
import concurrent.futures
import copy
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import os
import shutil
import sys
import tempfile
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

import pathops
from fontTools.otlLib.builder import buildStatTable
from fontTools.misc.roundTools import otRound
from fontTools.pens.basePen import BasePen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates, flagCubic
from PIL import Image, ImageDraw, ImageFont, features
from tangent_quantization import (
    QuantizationIssue,
    SmoothJoin,
    TangentQuantizationMetrics,
    quantize_glyph_tangents,
    smooth_join_components,
)


VERSION = "2.000"
RELEASE_DATE = datetime(2026, 7, 13, tzinfo=timezone.utc)
MAC_EPOCH_OFFSET = 2082844800
SMOOTH_JOIN_SOURCE_LIMIT = 0.01
SMOOTH_JOIN_OUTPUT_LIMIT = 0.6
TANGENT_MAX_CONTROL_MOVE = 4.25
TANGENT_FALLBACK_MAX_CONTROL_MOVE = 8.0
TANGENT_LARGE_CONTROL_MOVE = 9.0
TANGENT_MICRO_HANDLE_LIMIT = 4.0
TANGENT_MICRO_LINE_LIMIT = 8.0
TANGENT_MICRO_LINE_OUTPUT_LIMIT = 9.0
TANGENT_MICRO_LATERAL_LIMIT = 2.1
TANGENT_MICRO_DIRECTION_LIMIT = 45.0
TANGENT_REGULAR_DIRECTION_LIMIT = 6.0
TANGENT_SPARSE_TWO_DIRECTION_LIMIT = 3.0
TANGENT_MAX_FACTOR_ENTRIES = 100_000
TANGENT_MAX_WORK = 7_000_000
# A component may retry with additional explicit-line endpoints locked when a
# tangent-preserving candidate changes topology.  The search is breadth-first,
# so useful low-lock solutions are considered first.  Bound the combinatorial
# tail: components that still need more variants are safer on their untouched
# Inter outline than spending hours exploring increasingly constrained states.
TANGENT_MAX_LOCK_VARIANTS = 4
SOURCE_JOIN_MATCH_TOLERANCE = 1e-9
SOURCE_JOIN_IDENTITY_TOLERANCE = 1e-10
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounding-core", type=Path, required=True)
    parser.add_argument("--roman-font", type=Path, required=True)
    parser.add_argument("--italic-font", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("src"))
    parser.add_argument("--proof-root", type=Path, default=Path("proofs/v2"))
    parser.add_argument("--jobs", type=int, default=2)
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


class GlyphConversion(NamedTuple):
    glyph: object
    metrics: object
    curve_join_candidates: int
    exact_lattice_reductions: int
    lattice_degenerate_joins: int
    lattice_degenerate_join_records: Tuple[Tuple[int, int, int], ...]
    solver_tier: str
    joins: Tuple[SmoothJoin, ...]
    max_regular_direction_shift: float
    max_micro_direction_shift: float


class ReductionProvenance(NamedTuple):
    """A removed control and the surviving point representing its lattice locus."""

    source: Tuple[float, float]
    survivor_identity: int


class SourceSmoothJoinRecord(NamedTuple):
    contour: int
    point: Tuple[float, float]
    source_join: int
    minimum_handle_length: float


class ComponentConversion(NamedTuple):
    glyph: object
    metrics: TangentQuantizationMetrics
    solver_tier: str
    max_regular_direction_shift: float
    max_micro_direction_shift: float


def vector_angle_degrees(first: Tuple[float, float], second: Tuple[float, float]) -> float:
    first_length = math.hypot(*first)
    second_length = math.hypot(*second)
    if first_length < 1e-9 or second_length < 1e-9:
        return math.inf
    cosine = (first[0] * second[0] + first[1] * second[1]) / (
        first_length * second_length
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def tangent_handle_length(segment, at_start: bool) -> float:
    if segment.kind == "line":
        return math.dist(segment.start, segment.end)
    control = segment.points[1] if at_start else segment.points[-2]
    endpoint = segment.start if at_start else segment.end
    return math.dist(endpoint, control)


def micro_direction_limit(handle_length: float) -> float:
    """Bound angular change by a small lateral displacement in font units."""
    if handle_length <= TANGENT_MICRO_LATERAL_LIMIT:
        return TANGENT_MICRO_DIRECTION_LIMIT
    lateral_angle = math.degrees(
        math.asin(min(1.0, TANGENT_MICRO_LATERAL_LIMIT / handle_length))
    )
    return min(
        TANGENT_MICRO_DIRECTION_LIMIT,
        max(TANGENT_REGULAR_DIRECTION_LIMIT, lateral_angle),
    )


class TopologyFlattenPen(BasePen):
    """Flatten a simple glyph finely enough to detect sub-unit crossings."""

    def __init__(self, tolerance: float = 0.05) -> None:
        super().__init__(None)
        self.tolerance = tolerance
        self.contours: List[List[Tuple[float, float]]] = []
        self.current: List[Tuple[float, float]] = []

    @staticmethod
    def midpoint(
        first: Tuple[float, float], second: Tuple[float, float]
    ) -> Tuple[float, float]:
        return ((first[0] + second[0]) * 0.5, (first[1] + second[1]) * 0.5)

    @staticmethod
    def point_line_distance(
        point: Tuple[float, float],
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> float:
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        if length < 1e-12:
            return math.dist(point, start)
        return abs(
            direction[0] * (start[1] - point[1])
            - (start[0] - point[0]) * direction[1]
        ) / length

    def _moveTo(self, point) -> None:
        self.current = [tuple(map(float, point))]

    def _lineTo(self, point) -> None:
        self.current.append(tuple(map(float, point)))

    def _qCurveToOne(self, control, end) -> None:
        start = self.current[-1]
        control = tuple(map(float, control))
        end = tuple(map(float, end))

        def flatten(first, middle, last, depth: int) -> None:
            if (
                depth >= 16
                or self.point_line_distance(middle, first, last)
                <= self.tolerance
            ):
                self.current.append(last)
                return
            first_middle = self.midpoint(first, middle)
            middle_last = self.midpoint(middle, last)
            split = self.midpoint(first_middle, middle_last)
            flatten(first, first_middle, split, depth + 1)
            flatten(split, middle_last, last, depth + 1)

        flatten(start, control, end, 0)

    def _curveToOne(self, first_control, second_control, end) -> None:
        start = self.current[-1]
        first_control = tuple(map(float, first_control))
        second_control = tuple(map(float, second_control))
        end = tuple(map(float, end))

        def flatten(first, control_one, control_two, last, depth: int) -> None:
            flatness = max(
                self.point_line_distance(control_one, first, last),
                self.point_line_distance(control_two, first, last),
            )
            if depth >= 16 or flatness <= self.tolerance:
                self.current.append(last)
                return
            first_one = self.midpoint(first, control_one)
            one_two = self.midpoint(control_one, control_two)
            two_last = self.midpoint(control_two, last)
            first_middle = self.midpoint(first_one, one_two)
            middle_last = self.midpoint(one_two, two_last)
            split = self.midpoint(first_middle, middle_last)
            flatten(first, first_one, first_middle, split, depth + 1)
            flatten(split, middle_last, two_last, last, depth + 1)

        flatten(start, first_control, second_control, end, 0)

    def finish_contour(self, close: bool) -> None:
        if self.current:
            if close and self.current[-1] != self.current[0]:
                self.current.append(self.current[0])
            self.contours.append(self.current)
        self.current = []

    def _closePath(self) -> None:
        self.finish_contour(True)

    def _endPath(self) -> None:
        self.finish_contour(False)


def contour_has_proper_self_intersection(
    points: List[Tuple[float, float]],
) -> bool:
    segment_count = max(0, len(points) - 1)
    bounds = [
        (
            min(points[index][0], points[index + 1][0]),
            min(points[index][1], points[index + 1][1]),
            max(points[index][0], points[index + 1][0]),
            max(points[index][1], points[index + 1][1]),
        )
        for index in range(segment_count)
    ]

    def cross(first, second, third) -> float:
        return (second[0] - first[0]) * (third[1] - first[1]) - (
            second[1] - first[1]
        ) * (third[0] - first[0])

    for first_index in range(segment_count):
        first_start = points[first_index]
        first_end = points[first_index + 1]
        if math.dist(first_start, first_end) < 1e-12:
            continue
        for second_index in range(first_index + 1, segment_count):
            if second_index == first_index + 1 or (
                first_index == 0 and second_index == segment_count - 1
            ):
                continue
            second_start = points[second_index]
            second_end = points[second_index + 1]
            if math.dist(second_start, second_end) < 1e-12:
                continue
            first_bounds = bounds[first_index]
            second_bounds = bounds[second_index]
            if (
                first_bounds[2] <= second_bounds[0]
                or second_bounds[2] <= first_bounds[0]
                or first_bounds[3] <= second_bounds[1]
                or second_bounds[3] <= first_bounds[1]
            ):
                continue
            first_side = cross(first_start, first_end, second_start)
            second_side = cross(first_start, first_end, second_end)
            third_side = cross(second_start, second_end, first_start)
            fourth_side = cross(second_start, second_end, first_end)
            if first_side * second_side < -1e-9 and third_side * fourth_side < -1e-9:
                return True
    return False


def contours_have_proper_intersection(
    first: List[Tuple[float, float]],
    second: List[Tuple[float, float]],
) -> bool:
    """Return whether two flattened contours cross through one another."""

    if len(first) < 2 or len(second) < 2:
        return False

    def contour_bounds(points):
        return (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )

    first_bounds = contour_bounds(first)
    second_bounds = contour_bounds(second)
    if (
        first_bounds[2] <= second_bounds[0]
        or second_bounds[2] <= first_bounds[0]
        or first_bounds[3] <= second_bounds[1]
        or second_bounds[3] <= first_bounds[1]
    ):
        return False

    def cross(start, end, point) -> float:
        return (end[0] - start[0]) * (point[1] - start[1]) - (
            end[1] - start[1]
        ) * (point[0] - start[0])

    for first_start, first_end in zip(first, first[1:]):
        if math.dist(first_start, first_end) < 1e-12:
            continue
        first_segment_bounds = (
            min(first_start[0], first_end[0]),
            min(first_start[1], first_end[1]),
            max(first_start[0], first_end[0]),
            max(first_start[1], first_end[1]),
        )
        for second_start, second_end in zip(second, second[1:]):
            if math.dist(second_start, second_end) < 1e-12:
                continue
            second_segment_bounds = (
                min(second_start[0], second_end[0]),
                min(second_start[1], second_end[1]),
                max(second_start[0], second_end[0]),
                max(second_start[1], second_end[1]),
            )
            if (
                first_segment_bounds[2] <= second_segment_bounds[0]
                or second_segment_bounds[2] <= first_segment_bounds[0]
                or first_segment_bounds[3] <= second_segment_bounds[1]
                or second_segment_bounds[3] <= first_segment_bounds[1]
            ):
                continue
            first_side = cross(first_start, first_end, second_start)
            second_side = cross(first_start, first_end, second_end)
            third_side = cross(second_start, second_end, first_start)
            fourth_side = cross(second_start, second_end, first_end)
            if first_side * second_side < -1e-9 and third_side * fourth_side < -1e-9:
                return True
    return False


def point_contour_relation(
    point: Tuple[float, float], contour: List[Tuple[float, float]]
) -> int:
    """Return -1 outside, 0 on the boundary, or 1 inside a contour."""

    x, y = point
    inside = False
    for start, end in zip(contour, contour[1:]):
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        if length < 1e-12:
            continue
        cross = direction[0] * (y - start[1]) - direction[1] * (x - start[0])
        dot = (x - start[0]) * direction[0] + (y - start[1]) * direction[1]
        if abs(cross) <= 1e-7 * max(1.0, length) and -1e-7 <= dot <= length**2 + 1e-7:
            return 0
        if (start[1] > y) != (end[1] > y):
            intersection_x = start[0] + (y - start[1]) * direction[0] / direction[1]
            if intersection_x > x:
                inside = not inside
    return 1 if inside else -1


def glyph_topology_signature(
    glyph,
) -> Tuple[object, ...]:
    """Describe winding, PathOps topology, and proper self-intersections."""

    def winding(area: float) -> int:
        if area > 1e-7:
            return 1
        if area < -1e-7:
            return -1
        return 0

    path = pathops.Path()
    glyph.draw(path.getPen(), None)
    signature: List[Tuple[int, Tuple[int, ...]]] = []
    for contour in path.contours:
        simplified = pathops.simplify(contour)
        signature.append(
            (
                winding(contour.area),
                tuple(winding(part.area) for part in simplified.contours),
            )
        )
    flatten_pen = TopologyFlattenPen()
    glyph.draw(flatten_pen, None)
    flattened = flatten_pen.contours
    combined = pathops.simplify(path)
    pair_relations = []
    for first_index in range(len(flattened)):
        for second_index in range(first_index + 1, len(flattened)):
            first = flattened[first_index]
            second = flattened[second_index]
            pair_relations.append(
                (
                    contours_have_proper_intersection(first, second),
                    point_contour_relation(first[0], second),
                    point_contour_relation(second[0], first),
                )
            )
    return (
        tuple(signature),
        tuple(
            contour_has_proper_self_intersection(contour)
            for contour in flattened
        ),
        tuple(sorted(winding(contour.area) for contour in combined.contours)),
        tuple(pair_relations),
    )


def collapse_duplicate_oncurve_points(glyph) -> int:
    """Remove zero-length line points without changing the rendered outline."""
    coordinates = [tuple(point) for point in glyph.coordinates]
    flags = list(glyph.flags)
    new_coordinates: List[Tuple[float, float]] = []
    new_flags: List[int] = []
    new_end_points: List[int] = []
    removed = 0
    start = 0
    for end in glyph.endPtsOfContours:
        kept: List[int] = []
        for index in range(start, end + 1):
            if (
                kept
                and flags[kept[-1]] & 1
                and flags[index] & 1
                and math.dist(coordinates[kept[-1]], coordinates[index]) < 1e-9
            ):
                removed += 1
                continue
            kept.append(index)
        if (
            len(kept) > 1
            and flags[kept[0]] & 1
            and flags[kept[-1]] & 1
            and math.dist(coordinates[kept[0]], coordinates[kept[-1]]) < 1e-9
        ):
            kept.pop()
            removed += 1
        for index in kept:
            new_coordinates.append(coordinates[index])
            new_flags.append(flags[index])
        new_end_points.append(len(new_coordinates) - 1)
        start = end + 1

    if removed:
        glyph.coordinates = GlyphCoordinates(new_coordinates)
        glyph.flags = array("B", new_flags)
        glyph.endPtsOfContours = new_end_points
    return removed


def source_smooth_join_records(infos):
    records: List[SourceSmoothJoinRecord] = []
    for contour_index, info in enumerate(infos):
        segments = info.segments
        for index, segment in enumerate(segments):
            following = segments[(index + 1) % len(segments)]
            if (
                (segment.kind != "line" or following.kind != "line")
                and vector_angle_degrees(
                    segment.tangent_end(), following.tangent_start()
                )
                <= SMOOTH_JOIN_SOURCE_LIMIT
            ):
                records.append(
                    SourceSmoothJoinRecord(
                        contour_index,
                        tuple(map(float, segment.end)),
                        len(records),
                        min(
                            tangent_handle_length(segment, False),
                            tangent_handle_length(following, True),
                        ),
                    )
                )
    return tuple(records), len(records)


def source_records_at_point(
    smooth_records: Tuple[SourceSmoothJoinRecord, ...],
    contour_index: int,
    point: Tuple[float, float],
) -> Tuple[SourceSmoothJoinRecord, ...]:
    """Match one Cu2Qu point to its full-precision source endpoint.

    Cu2Qu can emit then remove a numerically duplicate contour-closure point.
    Decimal rounding is not a stable identity in that case: values separated
    by machine epsilon can land on opposite sides of a rounding tie. Resolve
    by geometry on the same contour and reject genuinely ambiguous nearby
    source locations.
    """

    candidates = [
        (math.dist(record.point, point), record)
        for record in smooth_records
        if record.contour == contour_index
        and math.dist(record.point, point) <= SOURCE_JOIN_MATCH_TOLERANCE
    ]
    if not candidates:
        return ()
    _, nearest = min(
        candidates,
        key=lambda item: (
            item[0],
            item[1].source_join,
        ),
    )
    if any(
        math.dist(record.point, nearest.point) > SOURCE_JOIN_IDENTITY_TOLERANCE
        for _, record in candidates
    ):
        raise RuntimeError(
            "Ambiguous source smooth joins lie within the Cu2Qu match tolerance"
        )
    return tuple(record for _, record in candidates)


def remove_glyph_point(glyph, point_index: int) -> None:
    coordinates = [tuple(point) for point in glyph.coordinates]
    flags = list(glyph.flags)
    coordinates.pop(point_index)
    flags.pop(point_index)
    glyph.coordinates = GlyphCoordinates(coordinates)
    glyph.flags = array("B", flags)
    glyph.endPtsOfContours = [
        end - 1 if end >= point_index else end for end in glyph.endPtsOfContours
    ]


def reduce_exact_lattice_quadratics(glyph, smooth_records):
    """Degree-reduce quadratics whose integer outline is already a line.

    When a quadratic control rounds to exactly one of its explicit on-curve
    endpoints, the quantized Bezier traces the same line segment as a TrueType
    line. Removing that control is therefore render-exact at the target lattice.
    A reduction is admitted only if every affected source-smooth endpoint stays
    inside the same 0.6-degree output contract.
    """

    relaxed_source_joins: Set[int] = set()
    provenance: List[ReductionProvenance] = []
    point_identities = list(range(len(glyph.coordinates)))
    while True:
        coordinates = [tuple(map(float, point)) for point in glyph.coordinates]
        flags = list(glyph.flags)
        selected = None
        start = 0
        for contour_index, end in enumerate(glyph.endPtsOfContours):
            contour_size = end - start + 1
            for control in range(start, end + 1):
                if flags[control] & 1 or flags[control] & flagCubic:
                    continue
                offset = control - start
                previous = start + (offset - 1) % contour_size
                following = start + (offset + 1) % contour_size
                if not (flags[previous] & 1 and flags[following] & 1):
                    continue
                rounded_control = tuple(otRound(value) for value in coordinates[control])
                rounded_previous = tuple(otRound(value) for value in coordinates[previous])
                rounded_following = tuple(otRound(value) for value in coordinates[following])
                if rounded_previous == rounded_following:
                    continue
                matches_previous = rounded_control == rounded_previous
                matches_following = rounded_control == rounded_following
                if matches_previous == matches_following:
                    continue

                previous_previous = start + (offset - 2) % contour_size
                following_following = start + (offset + 2) % contour_size
                endpoint_checks = (
                    (
                        previous,
                        previous_previous,
                        following,
                    ),
                    (
                        following,
                        previous,
                        following_following,
                    ),
                )
                affected: Set[int] = set()
                safe = True
                for central, incoming_point, outgoing_point in endpoint_checks:
                    source_records = source_records_at_point(
                        smooth_records,
                        contour_index,
                        coordinates[central],
                    )
                    if not source_records:
                        continue
                    incoming = (
                        coordinates[central][0] - coordinates[incoming_point][0],
                        coordinates[central][1] - coordinates[incoming_point][1],
                    )
                    outgoing = (
                        coordinates[outgoing_point][0] - coordinates[central][0],
                        coordinates[outgoing_point][1] - coordinates[central][1],
                    )
                    if (
                        incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
                        <= 0.0
                        or vector_angle_degrees(incoming, outgoing)
                        > SMOOTH_JOIN_OUTPUT_LIMIT + 1e-9
                    ):
                        safe = False
                        break
                    affected.update(
                        record.source_join for record in source_records
                    )
                if not safe or not affected:
                    continue

                survivor = previous if matches_previous else following
                selected = (
                    control,
                    affected,
                    ReductionProvenance(
                        coordinates[control], point_identities[survivor]
                    ),
                )
                break
            if selected is not None:
                break
            start = end + 1
        if selected is None:
            break
        control, affected, reduction = selected
        relaxed_source_joins.update(affected)
        provenance.append(reduction)
        remove_glyph_point(glyph, control)
        point_identities.pop(control)
    return tuple(provenance), relaxed_source_joins, tuple(point_identities)


def map_source_smooth_joins(
    glyph,
    smooth_records,
    source_join_count: int,
    relaxed_source_joins: Set[int],
) -> Tuple[Tuple[SmoothJoin, ...], Tuple[int, ...]]:
    coordinates = [tuple(map(float, point)) for point in glyph.coordinates]
    contour_ranges: List[Tuple[int, int, int]] = []
    start = 0
    for contour_index, end in enumerate(glyph.endPtsOfContours):
        contour_ranges.extend([(contour_index, start, end)] * (end - start + 1))
        start = end + 1

    # Assign source joins globally before inspecting their neighbours.  A
    # first-match walk can let one numerically-near point steal a record from
    # its actual Cu2Qu survivor.  Requiring exactly one same-contour on-curve
    # candidate makes the provenance transfer one-to-one and fail-closed.
    source_assignments: Dict[int, SourceSmoothJoinRecord] = {}
    for record in smooth_records:
        candidates = [
            index
            for index, point in enumerate(coordinates)
            if glyph.flags[index] & 1
            and contour_ranges[index][0] == record.contour
            and math.dist(point, record.point) <= SOURCE_JOIN_MATCH_TOLERANCE
        ]
        if not candidates:
            raise RuntimeError(
                f"Could not map source smooth join {record.source_join} into "
                "the TrueType contour"
            )
        if len(candidates) != 1:
            raise RuntimeError(
                f"Source smooth join {record.source_join} maps ambiguously to "
                f"{len(candidates)} retained on-curve points"
            )
        point_index = candidates[0]
        if point_index in source_assignments:
            raise RuntimeError(
                "Multiple source smooth joins map to one retained on-curve point"
            )
        source_assignments[point_index] = record

    joins: List[SmoothJoin] = []
    lattice_degenerate_joins: List[int] = []
    matched_source_joins: Set[int] = set()
    for index, point in enumerate(coordinates):
        if not (glyph.flags[index] & 1):
            continue
        selected_record = source_assignments.get(index)
        if selected_record is None:
            continue
        contour_index, contour_start, contour_end = contour_ranges[index]
        source_join = selected_record.source_join
        matched_source_joins.add(source_join)
        contour_size = contour_end - contour_start + 1
        previous = contour_start + (index - contour_start - 1) % contour_size
        following = contour_start + (index - contour_start + 1) % contour_size
        incoming = (
            point[0] - coordinates[previous][0],
            point[1] - coordinates[previous][1],
        )
        outgoing = (
            coordinates[following][0] - point[0],
            coordinates[following][1] - point[1],
        )
        source_limit = (
            SMOOTH_JOIN_OUTPUT_LIMIT
            if source_join in relaxed_source_joins
            else SMOOTH_JOIN_SOURCE_LIMIT
        )
        minimum_handle_length = min(
            math.hypot(*incoming), math.hypot(*outgoing)
        )
        if (
            incoming[0] * outgoing[0] + incoming[1] * outgoing[1] <= 0.0
            or vector_angle_degrees(incoming, outgoing) > source_limit + 1e-9
        ):
            raise RuntimeError(
                f"Cu2Qu source join {source_join} is not smooth after exact "
                "lattice reduction"
            )
        joins.append(
            SmoothJoin(
                previous=previous,
                point=index,
                following=following,
                label=f"source-{source_join}",
                source_angle_limit=(
                    source_limit if source_join in relaxed_source_joins else None
                ),
                incoming_direction_shift_limit=(
                    micro_direction_limit(math.hypot(*incoming))
                    if minimum_handle_length < TANGENT_MICRO_HANDLE_LIMIT
                    else None
                ),
                outgoing_direction_shift_limit=(
                    micro_direction_limit(math.hypot(*outgoing))
                    if minimum_handle_length < TANGENT_MICRO_HANDLE_LIMIT
                    else None
                ),
            )
        )
        rounded_previous = tuple(otRound(value) for value in coordinates[previous])
        rounded_point = tuple(otRound(value) for value in point)
        rounded_following = tuple(otRound(value) for value in coordinates[following])
        if rounded_previous == rounded_point or rounded_point == rounded_following:
            lattice_degenerate_joins.append(len(joins) - 1)

    if len(matched_source_joins) != source_join_count:
        raise RuntimeError(
            f"Could not map {source_join_count - len(matched_source_joins)} "
            "source smooth join(s) into the TrueType contour"
        )
    return tuple(joins), tuple(lattice_degenerate_joins)


def tangent_solver_attempts():
    return (
        (
            "locked",
            {
                "lock_on_curve_points": True,
                "off_curve_max_move": TANGENT_MAX_CONTROL_MOVE,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
        (
            "joint",
            {
                "lock_on_curve_points": False,
                "on_curve_max_move": 1.0,
                "off_curve_max_move": TANGENT_MAX_CONTROL_MOVE,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
        *(
            (
                f"sparse-{on_curve_move:g}",
                {
                    "lock_on_curve_points": False,
                    "on_curve_max_move": on_curve_move,
                    "off_curve_max_move": TANGENT_LARGE_CONTROL_MOVE,
                    "sparse_off_curve_candidates": True,
                    "maximum_direction_shift": (
                        TANGENT_REGULAR_DIRECTION_LIMIT
                        if on_curve_move == 4.0
                        else (
                            TANGENT_SPARSE_TWO_DIRECTION_LIMIT
                            if on_curve_move == 2.0
                            else 2.0
                        )
                    ),
                    "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                    "maximum_work": TANGENT_MAX_WORK,
                },
            )
            for on_curve_move in (1.0, 2.0, 4.0)
        ),
        (
            "sparse-5",
            {
                "lock_on_curve_points": False,
                "on_curve_max_move": 5.0,
                "off_curve_max_move": TANGENT_LARGE_CONTROL_MOVE,
                "sparse_off_curve_candidates": True,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
        (
            "sparse-6",
            {
                "lock_on_curve_points": False,
                "on_curve_max_move": 6.0,
                "off_curve_max_move": TANGENT_LARGE_CONTROL_MOVE,
                "sparse_off_curve_candidates": True,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
        (
            "full-4",
            {
                "lock_on_curve_points": False,
                "on_curve_max_move": 4.0,
                "off_curve_max_move": TANGENT_FALLBACK_MAX_CONTROL_MOVE,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
        (
            "sparse-7",
            {
                "lock_on_curve_points": False,
                "on_curve_max_move": 7.0,
                "off_curve_max_move": TANGENT_LARGE_CONTROL_MOVE,
                "sparse_off_curve_candidates": True,
                "maximum_direction_shift": TANGENT_REGULAR_DIRECTION_LIMIT,
                "maximum_factor_entries": TANGENT_MAX_FACTOR_ENTRIES,
                "maximum_work": TANGENT_MAX_WORK,
            },
        ),
    )


def explicit_line_violations(
    glyph,
    baseline_coordinates: List[Tuple[int, int]],
    candidate_coordinates: List[Tuple[int, int]],
) -> Set[int]:
    """Return endpoints of explicit lines collapsed or reversed by a candidate.

    Tangent components do not necessarily contain the other endpoint of a
    neighboring line.  Preserve every line that survives ordinary TrueType
    rounding, including a bounded direction guard for very short segments.
    """

    flags = list(glyph.flags)
    violations: Set[int] = set()
    contour_start = 0
    for contour_end in glyph.endPtsOfContours:
        contour_size = contour_end - contour_start + 1
        for first in range(contour_start, contour_end + 1):
            second = contour_start + (first - contour_start + 1) % contour_size
            if not (flags[first] & 1 and flags[second] & 1):
                continue
            baseline = (
                baseline_coordinates[second][0] - baseline_coordinates[first][0],
                baseline_coordinates[second][1] - baseline_coordinates[first][1],
            )
            baseline_length = math.hypot(*baseline)
            if baseline_length < 1.0:
                continue
            candidate = (
                candidate_coordinates[second][0] - candidate_coordinates[first][0],
                candidate_coordinates[second][1] - candidate_coordinates[first][1],
            )
            direction_shift = vector_angle_degrees(baseline, candidate)
            lateral_shift = abs(
                baseline[0] * candidate[1] - baseline[1] * candidate[0]
            ) / baseline_length
            candidate_length = math.hypot(*candidate)
            forward_projection = (
                baseline[0] * candidate[0] + baseline[1] * candidate[1]
            )
            if candidate_length < 1.0:
                violations.update((first, second))
                continue
            # Direction is not a stable visual property for a line only a few
            # font units long: a one-unit endpoint quantization can rotate it
            # dramatically. Keep such connectors microscopic instead of
            # forcing their noisy nearest-lattice angle onto both adjacent
            # smooth curves.
            if baseline_length <= TANGENT_MICRO_LINE_LIMIT:
                if (
                    forward_projection < 0.0
                    or candidate_length > TANGENT_MICRO_LINE_OUTPUT_LIMIT + 1e-9
                ):
                    violations.update((first, second))
                continue
            if forward_projection <= 0.0:
                violations.update((first, second))
                continue
            if (
                direction_shift > TANGENT_MICRO_DIRECTION_LIMIT + 1e-9
                or (
                    direction_shift > TANGENT_REGULAR_DIRECTION_LIMIT + 1e-9
                    and lateral_shift > TANGENT_MICRO_LATERAL_LIMIT + 1e-9
                )
            ):
                violations.update((first, second))
        contour_start = contour_end + 1
    return violations


def solve_tangent_components(
    glyph,
    joins: Tuple[SmoothJoin, ...],
    provenance: Tuple[ReductionProvenance, ...],
    point_identities: Tuple[int, ...],
    collapsed_joins: Tuple[SmoothJoin, ...] = (),
) -> Tuple[Optional[ComponentConversion], List[object], Tuple[SmoothJoin, ...]]:
    float_coordinates = [tuple(map(float, point)) for point in glyph.coordinates]
    nearest = [tuple(otRound(value) for value in point) for point in float_coordinates]
    final_coordinates = list(nearest)
    baseline_glyph = copy.deepcopy(glyph)
    baseline_glyph.coordinates = GlyphCoordinates(nearest)
    # Topology signatures flatten and compare every contour in the glyph. Most
    # smooth joins already survive ordinary integer rounding unchanged, so
    # computing that global signature before we know that a solver moved a
    # point wastes the overwhelming majority of export time. Keep the same
    # acceptance test, but evaluate it lazily only for a genuinely modified
    # candidate that has already passed the cheaper explicit-line guard.
    baseline_topology = None
    topology_cache: Dict[Tuple[Tuple[int, int], ...], Tuple[object, ...]] = {}
    attempts = tangent_solver_attempts()
    tier_rank = {name: index for index, (name, _) in enumerate(attempts)}
    highest_tier = "locked"
    successful_metrics: List[TangentQuantizationMetrics] = []
    conversion_issues: List[object] = []

    collapsed_constraints = []
    for join in collapsed_joins:
        triple = (join.previous, join.point, join.following)
        left_collapsed = nearest[join.previous] == nearest[join.point]
        right_collapsed = nearest[join.point] == nearest[join.following]
        if not left_collapsed and not right_collapsed:
            raise RuntimeError("A retired join has no collapsed lattice side")
        scope = (
            triple
            if left_collapsed and right_collapsed
            else (join.previous, join.point)
            if left_collapsed
            else (join.point, join.following)
        )
        collapsed_constraints.append((triple, scope))
    collapsed_scopes = tuple(scope for _, scope in collapsed_constraints)
    for component_indices in smooth_join_components(
        joins, connecting_point_scopes=collapsed_scopes
    ):
        component_joins = tuple(joins[index] for index in component_indices)
        component_points = {
            point
            for join in component_joins
            for point in (join.previous, join.point, join.following)
        }
        component_collapsed: List[Tuple[int, int, int]] = []
        remaining_collapsed = list(collapsed_constraints)
        while True:
            connected = [
                constraint
                for constraint in remaining_collapsed
                if component_points.intersection(constraint[1])
            ]
            if not connected:
                break
            for constraint in connected:
                triple, scope = constraint
                remaining_collapsed.remove(constraint)
                component_collapsed.append(triple)
                component_points.update(scope)
        conversion = None
        component_tier = ""
        component_accepted = False
        for component_tier, options in attempts:
            pending_locks = [frozenset()]
            seen_locks = {frozenset()}
            accepted_candidates = []
            accepted_lock_count: Optional[int] = None
            evaluated_lock_variants = 0
            while (
                pending_locks
                and evaluated_lock_variants < TANGENT_MAX_LOCK_VARIANTS
            ):
                pending_locks.sort(key=lambda item: (len(item), tuple(sorted(item))))
                locked_points = pending_locks.pop(0)
                if (
                    accepted_lock_count is not None
                    and len(locked_points) > accepted_lock_count
                ):
                    break
                candidate_conversion = quantize_glyph_tangents(
                    glyph,
                    component_joins,
                    output_angle_limit=SMOOTH_JOIN_OUTPUT_LIMIT,
                    source_angle_limit=SMOOTH_JOIN_SOURCE_LIMIT,
                    locked_points=locked_points,
                    collapsed_point_triples=component_collapsed,
                    **options,
                )
                evaluated_lock_variants += 1
                conversion_issues.extend(candidate_conversion.issues)
                if not candidate_conversion.success:
                    if any(
                        issue.kind == "work-limit"
                        for issue in candidate_conversion.issues
                    ):
                        return None, conversion_issues, component_joins
                    continue
                candidate_coordinates = list(final_coordinates)
                for point in component_points:
                    candidate_coordinates[point] = tuple(
                        candidate_conversion.glyph.coordinates[point]
                    )
                line_violations = explicit_line_violations(
                    glyph, nearest, candidate_coordinates
                )
                topology_changed = False
                coordinates_changed = any(
                    candidate_coordinates[point] != nearest[point]
                    for point in component_points
                )
                if coordinates_changed and not line_violations:
                    if baseline_topology is None:
                        baseline_topology = glyph_topology_signature(baseline_glyph)
                        topology_cache[tuple(nearest)] = baseline_topology
                    coordinate_key = tuple(candidate_coordinates)
                    candidate_topology = topology_cache.get(coordinate_key)
                    if candidate_topology is None:
                        candidate_glyph = copy.deepcopy(glyph)
                        candidate_glyph.coordinates = GlyphCoordinates(
                            candidate_coordinates
                        )
                        candidate_topology = glyph_topology_signature(candidate_glyph)
                        topology_cache[coordinate_key] = candidate_topology
                    topology_changed = candidate_topology != baseline_topology
                if not topology_changed and not line_violations:
                    movement_cost = sum(
                        (4.0 if glyph.flags[point] & 1 else 1.0)
                        * (
                            (candidate_coordinates[point][0] - float_coordinates[point][0])
                            ** 2
                            + (
                                candidate_coordinates[point][1]
                                - float_coordinates[point][1]
                            )
                            ** 2
                        )
                        for point in component_points
                    )
                    accepted_lock_count = len(locked_points)
                    accepted_candidates.append(
                        (
                            movement_cost,
                            tuple(sorted(locked_points)),
                            candidate_conversion,
                            candidate_coordinates,
                        )
                    )
                    continue
                if topology_changed:
                    conversion_issues.append(
                        QuantizationIssue(
                            "topology-change",
                            "candidate tangent quantization changes contour topology",
                        )
                    )
                if line_violations:
                    conversion_issues.append(
                        QuantizationIssue(
                            "explicit-line-change",
                            "candidate tangent quantization collapses, reverses, or "
                            "over-rotates an explicit line",
                        )
                    )
                retry_points: Set[int] = set()
                if topology_changed:
                    retry_points.update(component_points)
                retry_points.update(line_violations & component_points)
                newly_lockable = {
                    point
                    for point in retry_points - locked_points
                    if candidate_coordinates[point] != nearest[point]
                }
                # Explore the smallest lock subsets independently. Greedily
                # freezing every moved attachment can over-constrain the next
                # tier even when either attachment alone is sufficient.
                for point in sorted(newly_lockable):
                    retry_locks = frozenset((*locked_points, point))
                    if retry_locks not in seen_locks:
                        seen_locks.add(retry_locks)
                        pending_locks.append(retry_locks)
            if accepted_candidates:
                (
                    _,
                    _,
                    conversion,
                    accepted_coordinates,
                ) = min(accepted_candidates, key=lambda item: (item[0], item[1]))
                final_coordinates = accepted_coordinates
                component_accepted = True
                break
        if not component_accepted:
            return None, conversion_issues, component_joins
        assert conversion is not None
        highest_tier = max(
            (highest_tier, component_tier), key=lambda tier: tier_rank[tier]
        )
        for point in component_points:
            final_coordinates[point] = tuple(conversion.glyph.coordinates[point])
        successful_metrics.append(conversion.metrics)

    output_angles: List[float] = []
    direction_shifts: List[float] = []
    regular_direction_shifts: List[float] = []
    micro_direction_shifts: List[float] = []
    for join in joins:
        source_incoming = (
            float_coordinates[join.point][0] - float_coordinates[join.previous][0],
            float_coordinates[join.point][1] - float_coordinates[join.previous][1],
        )
        source_outgoing = (
            float_coordinates[join.following][0] - float_coordinates[join.point][0],
            float_coordinates[join.following][1] - float_coordinates[join.point][1],
        )
        output_incoming = (
            final_coordinates[join.point][0] - final_coordinates[join.previous][0],
            final_coordinates[join.point][1] - final_coordinates[join.previous][1],
        )
        output_outgoing = (
            final_coordinates[join.following][0] - final_coordinates[join.point][0],
            final_coordinates[join.following][1] - final_coordinates[join.point][1],
        )
        output_angle = vector_angle_degrees(output_incoming, output_outgoing)
        if (
            output_incoming[0] * output_outgoing[0]
            + output_incoming[1] * output_outgoing[1]
            <= 0.0
            or source_incoming[0] * output_incoming[0]
            + source_incoming[1] * output_incoming[1]
            <= 0.0
            or source_outgoing[0] * output_outgoing[0]
            + source_outgoing[1] * output_outgoing[1]
            <= 0.0
            or output_angle > SMOOTH_JOIN_OUTPUT_LIMIT + 1e-9
        ):
            raise RuntimeError("Merged component output failed tangent postconditions")
        output_angles.append(output_angle)
        join_direction_shifts = (
            vector_angle_degrees(source_incoming, output_incoming),
            vector_angle_degrees(source_outgoing, output_outgoing),
        )
        direction_shifts.extend(join_direction_shifts)
        for shift, limit in zip(
            join_direction_shifts,
            (
                join.incoming_direction_shift_limit,
                join.outgoing_direction_shift_limit,
            ),
        ):
            (
                micro_direction_shifts
                if limit is not None
                and limit > TANGENT_REGULAR_DIRECTION_LIMIT + 1e-9
                else regular_direction_shifts
            ).append(shift)

    if len(point_identities) != len(float_coordinates):
        raise RuntimeError("Point identity accounting does not match the glyph")
    survivor_indices = {
        identity: index for index, identity in enumerate(point_identities)
    }
    removed_control_moves = []
    for reduction in provenance:
        survivor = survivor_indices.get(reduction.survivor_identity)
        if survivor is None:
            raise RuntimeError("Could not account for a reduced quadratic control")
        removed_control_moves.append(
            math.dist(reduction.source, final_coordinates[survivor])
        )

    flags = list(glyph.flags)
    adjusted_indices = [
        index
        for index, (point, baseline) in enumerate(zip(final_coordinates, nearest))
        if point != baseline
    ]
    on_moves = [
        math.dist(final_coordinates[index], float_coordinates[index])
        for index in range(len(final_coordinates))
        if flags[index] & 1
    ]
    off_moves = [
        math.dist(final_coordinates[index], float_coordinates[index])
        for index in range(len(final_coordinates))
        if not (flags[index] & 1)
    ]
    metrics = TangentQuantizationMetrics(
        joins=len(joins),
        components=len(successful_metrics),
        solved_components=sum(metric.solved_components for metric in successful_metrics),
        adjusted_points=len(adjusted_indices),
        adjusted_on_curve_points=sum(
            bool(flags[index] & 1) for index in adjusted_indices
        ),
        adjusted_off_curve_points=sum(
            not bool(flags[index] & 1) for index in adjusted_indices
        ),
        max_on_curve_move=max(on_moves, default=0.0),
        max_off_curve_move=max(off_moves + removed_control_moves, default=0.0),
        max_output_angle=max(output_angles, default=0.0),
        max_tangent_direction_shift=max(direction_shifts, default=0.0),
        candidate_states=sum(metric.candidate_states for metric in successful_metrics),
        valid_join_states=sum(metric.valid_join_states for metric in successful_metrics),
        elimination_rows=sum(metric.elimination_rows for metric in successful_metrics),
        unresolved_joins=0,
    )
    output_glyph = copy.deepcopy(glyph)
    output_glyph.coordinates = GlyphCoordinates(final_coordinates)
    return (
        ComponentConversion(
            output_glyph,
            metrics,
            highest_tier,
            max(regular_direction_shifts, default=0.0),
            max(micro_direction_shifts, default=0.0),
        ),
        conversion_issues,
        (),
    )


def contour_infos_to_glyph(infos):
    target = TTGlyphPen(None)
    # The rounding core emits PostScript-style contour winding. TrueType uses
    # the opposite convention. Leaving the direction unchanged makes rebuilt
    # glyphs subtract from untouched components at overlaps (for example ø and
    # œ), even though each standalone glyph still looks correct.
    pen = Cu2QuPen(target, max_err=0.75, reverse_direction=True)
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

    # Keep the converted points fractional until neighboring controls at every
    # source-smooth join can be quantized together. Independent x/y rounding
    # can turn a perfectly tangent join into a visible peak when the handles
    # are short, as in the heavy lowercase e terminal.
    glyph = target.glyph(round=lambda value: value)
    collapse_duplicate_oncurve_points(glyph)
    smooth_records, source_curve_joins = source_smooth_join_records(infos)
    provenance, relaxed_source_joins, point_identities = reduce_exact_lattice_quadratics(
        glyph, smooth_records
    )
    joins, lattice_degenerate_indices = map_source_smooth_joins(
        glyph,
        smooth_records,
        source_curve_joins,
        relaxed_source_joins,
    )
    all_joins = joins
    degenerate_labels = {
        joins[index].label for index in lattice_degenerate_indices
    }
    retired_labels: Set[str] = set()
    conversion = None
    conversion_issues: List[object] = []
    while True:
        retired_joins = tuple(
            join for join in all_joins if join.label in retired_labels
        )
        conversion, attempt_issues, failed_component = solve_tangent_components(
            glyph,
            joins,
            provenance,
            point_identities,
            retired_joins,
        )
        conversion_issues.extend(attempt_issues)
        if conversion is not None:
            break
        newly_retired = {
            join.label
            for join in failed_component
            if join.label in degenerate_labels and join.label not in retired_labels
        }
        if not newly_retired:
            break
        retired_labels.update(newly_retired)
        joins = tuple(join for join in joins if join.label not in retired_labels)
    if conversion is None:
        details = "; ".join(issue.message for issue in conversion_issues[-4:])
        raise RuntimeError(
            f"Could not preserve {source_curve_joins} smooth join(s) during TrueType "
            f"quantization: {details}"
        )
    retired_joins = tuple(
        join for join in all_joins if join.label in retired_labels
    )
    final_coordinates = [tuple(point) for point in conversion.glyph.coordinates]
    if any(
        final_coordinates[join.previous] != final_coordinates[join.point]
        and final_coordinates[join.point] != final_coordinates[join.following]
        for join in retired_joins
    ):
        raise RuntimeError(
            "A retired lattice-degenerate join became nondegenerate in the "
            "accepted integer outline"
        )
    lattice_degenerate_records = tuple(
        (join.previous, join.point, join.following) for join in retired_joins
    )
    return GlyphConversion(
        conversion.glyph,
        conversion.metrics,
        source_curve_joins,
        len(provenance),
        len(lattice_degenerate_records),
        lattice_degenerate_records,
        conversion.solver_tier,
        joins,
        conversion.max_regular_direction_shift,
        conversion.max_micro_direction_shift,
    )


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
    tangent_curve_join_candidates = 0
    tangent_joins_checked = 0
    tangent_exact_lattice_reductions = 0
    tangent_lattice_degenerate_joins = 0
    tangent_lattice_degenerate_join_records: Dict[str, List[List[int]]] = {}
    tangent_points_adjusted = 0
    tangent_on_curve_points_adjusted = 0
    tangent_controls_adjusted = 0
    max_smooth_join_angle = 0.0
    max_tangent_direction_shift = 0.0
    max_tangent_regular_direction_shift = 0.0
    max_tangent_micro_direction_shift = 0.0
    max_tangent_on_curve_move = 0.0
    max_tangent_control_move = 0.0
    tangent_solver_tiers = {tier: 0 for tier in TANGENT_SOLVER_TIERS}
    tangent_regression_joins: Dict[str, List[List[int]]] = {}
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
            try:
                conversion = contour_infos_to_glyph(result.rounded)
            except RuntimeError as error:
                # Fail closed at the glyph boundary. A rounded cubic outline
                # that cannot be represented on the TrueType integer lattice
                # without violating tangent or topology postconditions is less
                # safe than the untouched Inter glyph. Keep that original
                # outline, record the reason, and continue the family build.
                # The release audit separately requires successful e/c/s
                # terminal conversions in every face, so this cannot hide the
                # regression this solver exists to prevent.
                reverted.append(glyph_name)
                warnings.setdefault(glyph_name, []).append(
                    f"tangent-quantization-revert: {error}"
                )
                continue
            glyph_table[glyph_name] = conversion.glyph
            changed.append(glyph_name)
            rounded_corners += result.rounded_count
            metrics = conversion.metrics
            if (
                metrics.joins + conversion.lattice_degenerate_joins
                != conversion.curve_join_candidates
            ):
                raise RuntimeError(
                    f"{weight_name} {style_label} glyph {glyph_name}: tangent "
                    "coverage accounting mismatch"
                )
            tangent_curve_join_candidates += conversion.curve_join_candidates
            tangent_joins_checked += metrics.joins
            tangent_exact_lattice_reductions += (
                conversion.exact_lattice_reductions
            )
            tangent_lattice_degenerate_joins += (
                conversion.lattice_degenerate_joins
            )
            if conversion.lattice_degenerate_join_records:
                tangent_lattice_degenerate_join_records[glyph_name] = [
                    list(record)
                    for record in conversion.lattice_degenerate_join_records
                ]
            tangent_solver_tiers[conversion.solver_tier] += 1
            regression_character = character_for.get(glyph_name)
            if regression_character in {"e", "c", "s"}:
                tangent_regression_joins[regression_character] = [
                    [join.previous, join.point, join.following]
                    for join in conversion.joins
                ]
            tangent_points_adjusted += metrics.adjusted_points
            tangent_on_curve_points_adjusted += metrics.adjusted_on_curve_points
            tangent_controls_adjusted += metrics.adjusted_off_curve_points
            max_smooth_join_angle = max(
                max_smooth_join_angle, metrics.max_output_angle
            )
            max_tangent_direction_shift = max(
                max_tangent_direction_shift, metrics.max_tangent_direction_shift
            )
            max_tangent_regular_direction_shift = max(
                max_tangent_regular_direction_shift,
                conversion.max_regular_direction_shift,
            )
            max_tangent_micro_direction_shift = max(
                max_tangent_micro_direction_shift,
                conversion.max_micro_direction_shift,
            )
            max_tangent_on_curve_move = max(
                max_tangent_on_curve_move, metrics.max_on_curve_move
            )
            max_tangent_control_move = max(
                max_tangent_control_move, metrics.max_off_curve_move
            )
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
    proof_path = proof_root / f"{names['postscript']}.png"
    render_proof(ttf_path, proof_path, weight, weight_name, italic)

    print(
        f"Finished {weight_name} {style_label}: changed={len(changed)}, "
        f"corners={rounded_corners}, rails={normalized_rails}, reverted={len(reverted)}",
        flush=True,
    )
    return {
        "style": names["actual_style"],
        "weight": weight,
        "source": source.name,
        "source_sha256": sha256(source),
        "radius_at_90_degrees": round(radius, 6),
        "measured_H_stem": round(stem, 6),
        "glyphs": len(glyph_order),
        "simple_glyphs_processed": simple_count,
        "glyphs_changed": len(changed),
        "rounded_corners": rounded_corners,
        "normalized_rail_pairs": normalized_rails,
        "tangent_curve_join_candidates": tangent_curve_join_candidates,
        "tangent_joins_checked": tangent_joins_checked,
        "tangent_exact_lattice_reductions": tangent_exact_lattice_reductions,
        "tangent_lattice_degenerate_joins": tangent_lattice_degenerate_joins,
        "tangent_lattice_degenerate_join_records": (
            tangent_lattice_degenerate_join_records
        ),
        "tangent_points_adjusted": tangent_points_adjusted,
        "tangent_on_curve_points_adjusted": tangent_on_curve_points_adjusted,
        "tangent_controls_adjusted": tangent_controls_adjusted,
        "max_smooth_join_angle": round(max_smooth_join_angle, 6),
        "max_tangent_direction_shift": round(max_tangent_direction_shift, 6),
        "max_tangent_regular_direction_shift": round(
            max_tangent_regular_direction_shift, 6
        ),
        "max_tangent_micro_direction_shift": round(
            max_tangent_micro_direction_shift, 6
        ),
        "max_tangent_on_curve_move": round(max_tangent_on_curve_move, 6),
        "max_tangent_control_move": round(max_tangent_control_move, 6),
        "tangent_conversion_failures": 0,
        "tangent_solver_tiers": tangent_solver_tiers,
        "tangent_regression_joins": tangent_regression_joins,
        "reverted_glyphs": reverted,
        "warnings": warnings,
        "ttf": str(ttf_path),
        "ttf_sha256": sha256(ttf_path),
        "woff2": str(woff2_path),
        "woff2_sha256": sha256(woff2_path),
        "proof": str(proof_path),
        "proof_sha256": sha256(proof_path),
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


def render_winding_proof(
    roman_source: Path,
    italic_source: Path,
    desktop_root: Path,
    output: Path,
) -> None:
    """Render every face beside Inter for overlap and winding inspection."""
    width, height, supersample = 2600, 2500, 2
    canvas = Image.new("RGB", (width * supersample, height * supersample), "white")
    draw = ImageDraw.Draw(canvas)
    navy = (15, 23, 42)
    muted = (86, 96, 113)
    grid = (226, 232, 240)
    sample = "Ø ø Ǿ ǿ  Œ œ  Ǫ ǫ"
    regular_path = desktop_root / "OpenRunde-Regular.ttf"
    title_font = ImageFont.truetype(str(regular_path), size=62 * supersample)
    draw.text(
        (70 * supersample, 38 * supersample),
        "Composite overlap and winding proof",
        font=title_font,
        fill=navy,
    )
    draw.text(
        (72 * supersample, 116 * supersample),
        "Inter 4.001 source reference followed by every Open Runde 2.000 face",
        font=ImageFont.truetype(str(regular_path), size=27 * supersample),
        fill=muted,
    )

    rows: List[Tuple[str, Path, str, Path]] = [
        ("Inter Regular source", roman_source, "Inter Italic source", italic_source)
    ]
    rows.extend(
        (
            f"Open Runde {weight_name}",
            desktop_root / f"{face_names(weight, weight_name, False)['postscript']}.ttf",
            f"Open Runde {weight_name} Italic",
            desktop_root / f"{face_names(weight, weight_name, True)['postscript']}.ttf",
        )
        for weight, weight_name, _ in WEIGHTS
    )

    top, row_height, column_width = 185, 228, width // 2
    draw.line(
        (column_width * supersample, top * supersample, column_width * supersample, height * supersample),
        fill=grid,
        width=2 * supersample,
    )
    for row_index, (left_label, left_path, right_label, right_path) in enumerate(rows):
        y = top + row_index * row_height
        if row_index:
            draw.line(
                (70 * supersample, y * supersample, (width - 70) * supersample, y * supersample),
                fill=grid,
                width=2 * supersample,
            )
        for column, (label, path) in enumerate(
            ((left_label, left_path), (right_label, right_path))
        ):
            x = 72 + column * column_width
            label_font = ImageFont.truetype(str(path), size=25 * supersample)
            sample_font = fit_font(
                path,
                sample,
                112 * supersample,
                (column_width - 145) * supersample,
            )
            draw.text((x * supersample, (y + 18) * supersample), label, font=label_font, fill=muted)
            draw.text((x * supersample, (y + 62) * supersample), sample, font=sample_font, fill=navy)

    canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, "PNG", optimize=True)


def render_heavy_terminal_proof(desktop_root: Path, output: Path) -> None:
    """Render the heaviest terminal shapes at inspection scale."""
    width, height, supersample = 2600, 1900, 2
    canvas = Image.new("RGB", (width * supersample, height * supersample), "white")
    draw = ImageDraw.Draw(canvas)
    navy = (15, 23, 42)
    muted = (86, 96, 113)
    grid = (226, 232, 240)
    regular_path = desktop_root / "OpenRunde-Regular.ttf"
    title_font = ImageFont.truetype(str(regular_path), size=62 * supersample)
    meta_font = ImageFont.truetype(str(regular_path), size=27 * supersample)
    draw.text(
        (70 * supersample, 38 * supersample),
        "Heavy terminal regression proof",
        font=title_font,
        fill=navy,
    )
    draw.text(
        (72 * supersample, 116 * supersample),
        "ExtraBold and Black · upright and italic · enlarged e, c, and s terminals",
        font=meta_font,
        fill=muted,
    )

    panel_top = 180
    panel_width = width // 2
    panel_height = (height - panel_top) // 2
    draw.line(
        (
            panel_width * supersample,
            panel_top * supersample,
            panel_width * supersample,
            height * supersample,
        ),
        fill=grid,
        width=2 * supersample,
    )
    draw.line(
        (
            0,
            (panel_top + panel_height) * supersample,
            width * supersample,
            (panel_top + panel_height) * supersample,
        ),
        fill=grid,
        width=2 * supersample,
    )

    for row, (weight, weight_name) in enumerate(((800, "ExtraBold"), (900, "Black"))):
        for column, italic in enumerate((False, True)):
            names = face_names(weight, weight_name, italic)
            path = desktop_root / f"{names['postscript']}.ttf"
            x = column * panel_width + 58
            y = panel_top + row * panel_height + 24
            label = f"{weight_name}{' Italic' if italic else ''} · wght {weight}"
            label_font = ImageFont.truetype(str(path), size=30 * supersample)
            sample_font = fit_font(
                path,
                "e c s",
                520 * supersample,
                (panel_width - 116) * supersample,
            )
            text_font = fit_font(
                path,
                "cease  excess  success  fierce",
                82 * supersample,
                (panel_width - 116) * supersample,
            )
            draw.text(
                (x * supersample, y * supersample),
                label,
                font=label_font,
                fill=muted,
            )
            draw.text(
                (x * supersample, (y + 62) * supersample),
                "e c s",
                font=sample_font,
                fill=navy,
            )
            draw.text(
                (x * supersample, (y + 650) * supersample),
                "cease  excess  success  fierce",
                font=text_font,
                fill=navy,
            )

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


def prepare_staging_root(destination: Path, *, copy_existing: bool) -> Path:
    """Create a disposable sibling, optionally seeded from an existing tree."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}-openrunde-build-",
            dir=destination.parent,
        )
    )
    if copy_existing and destination.exists():
        shutil.copytree(destination, staging, dirs_exist_ok=True)
    return staging


def build_fingerprints(rounding_core: Path) -> Dict[str, str]:
    """Return the code fingerprints that must agree across every release face."""

    return {
        "builder_sha256": sha256(Path(__file__).resolve()),
        "tangent_quantization_sha256": sha256(
            Path(__file__).with_name("tangent_quantization.py").resolve()
        ),
        "rounding_core_sha256": sha256(rounding_core.resolve()),
        "python": sys.version.split()[0],
        "fonttools": importlib.metadata.version("fonttools"),
        "pillow": importlib.metadata.version("pillow"),
        "freetype": str(features.version_module("freetype2")),
        "skia_pathops": importlib.metadata.version("skia-pathops"),
    }


def family_input_sha256(records: List[Dict[str, object]]) -> str:
    """Bind family-level diagnostic proofs to their ordered font inputs."""

    payload = [
        {
            "style": record["style"],
            "ttf_sha256": record["ttf_sha256"],
            "source_sha256": record["source_sha256"],
            "proof_sha256": record["proof_sha256"],
        }
        for record in records
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def promote_staging_roots(pairs: Tuple[Tuple[Path, Path], ...]) -> None:
    """Replace release directories only after every staged artifact exists.

    Each staging directory is a sibling of its destination, so the individual
    renames are atomic.  Existing trees are retained as backups until all
    promotions succeed; any failure restores every destination before it is
    re-raised.
    """
    promoted: List[Tuple[Path, Path, Optional[Path]]] = []
    try:
        for staging, destination in pairs:
            backup: Optional[Path] = None
            if destination.exists():
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=f".{destination.name}-openrunde-backup-",
                        dir=destination.parent,
                    )
                )
                backup.rmdir()
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup is not None:
                    os.replace(backup, destination)
                raise
            promoted.append((staging, destination, backup))
    except Exception:
        for staging, destination, backup in reversed(promoted):
            if destination.exists():
                os.replace(destination, staging)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        raise
    else:
        for _, _, backup in promoted:
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)


def main() -> int:
    args = parse_args()
    for required in (args.roman_font, args.italic_font):
        if not required.exists():
            raise SystemExit(f"Missing input font: {required}")

    final_output_root = args.output_root
    final_proof_root = args.proof_root
    fingerprints = build_fingerprints(args.rounding_core)
    source_fingerprints = {
        False: sha256(args.roman_font),
        True: sha256(args.italic_font),
    }
    core = load_rounding_core(args.rounding_core)
    bold = core.load_font(args.roman_font, 700, 14.0)
    base_stem = measure_h_stem(core, bold)
    if args.style != "all":
        previous_manifest_path = final_output_root / "export-manifest.json"
        if not previous_manifest_path.exists():
            raise SystemExit("Partial builds require an existing complete release manifest")
        previous_manifest = json.loads(
            previous_manifest_path.read_text(encoding="utf-8")
        )
        if previous_manifest.get("build_fingerprints") != fingerprints:
            raise SystemExit(
                "Partial build refused: the existing release was produced by "
                "different build code; rebuild --style all"
            )
        if any(
            face.get("build_fingerprints") != fingerprints
            for face in previous_manifest.get("faces", [])
        ):
            raise SystemExit(
                "Partial build refused: existing face fingerprints are incomplete "
                "or inconsistent; rebuild --style all"
            )
        if (
            previous_manifest.get("optical_size") != 14
            or abs(previous_manifest.get("base_bold_H_stem", -1) - base_stem)
            > 1e-6
        ):
            raise SystemExit(
                "Partial build refused: family configuration differs; rebuild "
                "--style all"
            )
        previous_faces = {
            face.get("style"): face
            for face in previous_manifest.get("faces", [])
            if isinstance(face, dict)
        }
        for italic in (False, True):
            for weight, weight_name, _ in WEIGHTS:
                names = face_names(weight, weight_name, italic)
                face = previous_faces.get(names["actual_style"])
                if (
                    face is None
                    or face.get("source_sha256") != source_fingerprints[italic]
                ):
                    raise SystemExit(
                        "Partial build refused: source fonts differ or the existing "
                        "family is incomplete; rebuild --style all"
                    )
                artifact_paths = (
                    (
                        final_output_root / "desktop" / f"{names['postscript']}.ttf",
                        face.get("ttf_sha256"),
                    ),
                    (
                        final_output_root / "web" / f"{names['postscript']}.woff2",
                        face.get("woff2_sha256"),
                    ),
                    (
                        final_proof_root / f"{names['postscript']}.png",
                        face.get("proof_sha256"),
                    ),
                )
                if any(
                    not path.exists() or sha256(path) != expected_sha
                    for path, expected_sha in artifact_paths
                ):
                    raise SystemExit(
                        "Partial build refused: an existing release artifact does "
                        "not match its manifest; rebuild --style all"
                    )
        previous_diagnostics = previous_manifest.get("diagnostics", {})
        for filename in ("winding-overlaps.png", "heavy-terminals.png"):
            path = final_proof_root / "diagnostics" / filename
            if (
                not path.exists()
                or sha256(path)
                != previous_diagnostics.get(filename, {}).get("sha256")
            ):
                raise SystemExit(
                    "Partial build refused: an existing diagnostic proof does not "
                    "match its manifest; rebuild --style all"
                )
    copy_existing = args.style != "all"
    args.output_root = prepare_staging_root(
        final_output_root, copy_existing=copy_existing
    )
    args.proof_root = prepare_staging_root(
        final_proof_root, copy_existing=copy_existing
    )
    atexit.register(shutil.rmtree, args.output_root, True)
    atexit.register(shutil.rmtree, args.proof_root, True)

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
        records = []
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=worker_count)
        futures = {executor.submit(build_face_job, job): job for job in jobs}
        try:
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    records.append(future.result())
                except Exception as error:
                    label = (
                        f"{job['weight_name']} "
                        f"{'Italic' if job['italic'] else 'Upright'}"
                    )
                    print(f"Failed {label}: {error}", flush=True)
                    for pending in futures:
                        pending.cancel()
                    raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        record_order = {
            face_names(
                int(job["weight"]),
                str(job["weight_name"]),
                bool(job["italic"]),
            )["actual_style"]: index
            for index, job in enumerate(jobs)
        }
        records.sort(key=lambda record: record_order[record["style"]])

    for record in records:
        record["build_fingerprints"] = fingerprints

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
    for record in records:
        record["ttf"] = str(
            final_output_root / "desktop" / Path(str(record["ttf"])).name
        )
        record["woff2"] = str(
            final_output_root / "web" / Path(str(record["woff2"])).name
        )
        record["proof"] = str(
            final_proof_root / Path(str(record["proof"])).name
        )
    render_winding_proof(
        args.roman_font,
        args.italic_font,
        args.output_root / "desktop",
        args.proof_root / "diagnostics" / "winding-overlaps.png",
    )
    render_heavy_terminal_proof(
        args.output_root / "desktop",
        args.proof_root / "diagnostics" / "heavy-terminals.png",
    )
    family_fingerprint = family_input_sha256(records)
    diagnostic_records = {}
    for filename in ("winding-overlaps.png", "heavy-terminals.png"):
        staged_path = args.proof_root / "diagnostics" / filename
        diagnostic_records[filename] = {
            "path": str(final_proof_root / "diagnostics" / filename),
            "sha256": sha256(staged_path),
            "family_input_sha256": family_fingerprint,
        }
    manifest = {
        "family": "Open Runde",
        "version": VERSION,
        "algorithm": "openrunde-reference-fit-v11-family-support-intersection-unfillet",
        "optical_size": 14,
        "base_bold_H_stem": round(base_stem, 6),
        "build_fingerprints": fingerprints,
        "proof_text_sha256": hashlib.sha256(
            "\n".join(PROOF_LINES).encode("utf-8")
        ).hexdigest(),
        "family_input_sha256": family_fingerprint,
        "diagnostics": diagnostic_records,
        "faces": records,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    promote_staging_roots(
        (
            (args.output_root, final_output_root),
            (args.proof_root, final_proof_root),
        )
    )
    print(f"Wrote {len(records)} faces and {len(records)} proofs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
