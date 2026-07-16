#!/usr/bin/env python3
"""Jointly quantize smooth TrueType joins without creating visible kinks.

TrueType ``glyf`` coordinates are integers.  Rounding an on-curve point and
its neighboring controls independently can turn a geometrically smooth float
outline into a visibly angled integer outline, especially when one of the
handles is short.  This module treats every tracked smooth join as a joint
constraint and solves overlapping joins together.

The public entry point, :func:`quantize_glyph_tangents`, accepts a simple
``Glyph`` whose coordinates are still floats plus join metadata expressed as
point indices.  It returns a copied glyph; the input glyph is never modified.
On failure, the returned glyph contains ordinary nearest-integer coordinates
and ``success`` is false, so callers cannot accidentally consume a partially
solved outline without explicitly ignoring the result.
"""

from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass
from numbers import Integral
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from fontTools.misc.roundTools import otRound
from fontTools.ttLib.tables._g_l_y_f import (
    GlyphCoordinates,
    flagCubic,
    flagOnCurve,
)


Point = Tuple[float, float]
IntPoint = Tuple[int, int]
StateKey = Tuple[int, ...]


@dataclass(frozen=True)
class SmoothJoin:
    """A tracked smooth join in a float TrueType contour.

    ``previous`` and ``following`` must be the cyclic neighbors of the
    explicit on-curve ``point``.  ``continuity`` may be ``"g1"`` (collinear,
    same-direction tangents) or ``"c1"`` (equal parametric derivatives in
    addition to G1).  G1 is the appropriate default for font outlines.
    ``source_angle_limit`` may widen the caller's global source-angle limit for
    this join only, for example after a proven target-lattice degree reduction.
    The incoming/outgoing direction limits may likewise provide side-specific
    shape guards. This is useful for sub-grid handles whose direction changes
    greatly after a physically tiny movement even though their output tangent
    remains smooth.
    """

    previous: int
    point: int
    following: int
    continuity: str = "g1"
    label: str = ""
    source_angle_limit: Optional[float] = None
    incoming_direction_shift_limit: Optional[float] = None
    outgoing_direction_shift_limit: Optional[float] = None


@dataclass(frozen=True)
class QuantizationIssue:
    kind: str
    message: str
    join_index: Optional[int] = None
    point: Optional[int] = None


@dataclass(frozen=True)
class TangentQuantizationMetrics:
    joins: int
    components: int
    solved_components: int
    adjusted_points: int
    adjusted_on_curve_points: int
    adjusted_off_curve_points: int
    max_on_curve_move: float
    max_off_curve_move: float
    max_output_angle: float
    max_tangent_direction_shift: float
    candidate_states: int
    valid_join_states: int
    elimination_rows: int
    unresolved_joins: int


@dataclass(frozen=True)
class TangentQuantizationResult:
    glyph: object
    success: bool
    metrics: TangentQuantizationMetrics
    issues: Tuple[QuantizationIssue, ...]


@dataclass(frozen=True)
class _JoinGeometry:
    metadata: SmoothJoin
    incoming: Point
    outgoing: Point
    source_angle: float


@dataclass(frozen=True)
class _Factor:
    scope: Tuple[int, ...]
    table: Mapping[StateKey, float]


@dataclass(frozen=True)
class _EliminationRecord:
    variable: int
    separator: Tuple[int, ...]
    choices: Mapping[StateKey, int]


@dataclass(frozen=True)
class _BlockEliminationRecord:
    variables: Tuple[int, ...]
    separator: Tuple[int, ...]
    choices: Mapping[StateKey, StateKey]


class _WorkLimitExceeded(RuntimeError):
    """Raised when one glyph exhausts its deterministic solver budget."""


@dataclass
class _WorkBudget:
    limit: int
    used: int = 0

    def consume(self, amount: int = 1, *, stage: str) -> None:
        if amount < 0:
            raise ValueError("work amount cannot be negative")
        if self.used + amount > self.limit:
            self.used = self.limit
            raise _WorkLimitExceeded(
                f"solver exceeded {self.limit} work units while {stage}"
            )
        self.used += amount


def _vector(start: Point, end: Point) -> Point:
    return (end[0] - start[0], end[1] - start[1])


def _length(vector: Point) -> float:
    return math.hypot(vector[0], vector[1])


def _dot(first: Point, second: Point) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _cross(first: Point, second: Point) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _angle_degrees(first: Point, second: Point) -> float:
    """Return the directed-vector angle using stable small-angle arithmetic."""

    first_length = _length(first)
    second_length = _length(second)
    if first_length < 1e-12 or second_length < 1e-12:
        return math.inf
    return math.degrees(
        math.atan2(abs(_cross(first, second)), _dot(first, second))
    )


def _angle_is_within_limit(
    first: Point,
    second: Point,
    limit_degrees: float,
) -> bool:
    """Cheaply test an acute angle limit before evaluating ``atan2``.

    Every production tangent and direction limit is below 90 degrees. In
    that range ``atan2(|cross|, dot) <= limit`` is exactly equivalent to
    ``|cross| <= tan(limit) * dot`` with a positive dot product. The fallback
    keeps this helper correct if a caller ever supplies a wider limit.
    """

    dot = _dot(first, second)
    if limit_degrees >= 90.0:
        return _angle_degrees(first, second) <= limit_degrees + 1e-9
    if dot <= 0.0:
        return False
    tangent = math.tan(math.radians(limit_degrees + 1e-9))
    return abs(_cross(first, second)) <= tangent * dot


def _join_vectors(
    coordinates: Sequence[Point], join: SmoothJoin
) -> Tuple[Point, Point]:
    previous = coordinates[join.previous]
    point = coordinates[join.point]
    following = coordinates[join.following]
    return _vector(previous, point), _vector(point, following)


def join_angle_degrees(
    coordinates: Sequence[Point], join: SmoothJoin
) -> float:
    """Measure the geometric discontinuity at ``join`` in degrees."""

    incoming, outgoing = _join_vectors(coordinates, join)
    return _angle_degrees(incoming, outgoing)


def _segment_degree(neighbor_flag: int) -> int:
    if neighbor_flag & flagOnCurve:
        return 1
    if neighbor_flag & flagCubic:
        return 3
    return 2


def _continuity_is_valid(
    incoming: Point,
    outgoing: Point,
    join: SmoothJoin,
    flags: Sequence[int],
) -> bool:
    if _dot(incoming, incoming) < 1.0 or _dot(outgoing, outgoing) < 1.0:
        return False
    if _dot(incoming, outgoing) <= 0.0:
        return False
    if join.continuity == "g1":
        return True
    if join.continuity != "c1":
        return False

    incoming_degree = _segment_degree(flags[join.previous])
    outgoing_degree = _segment_degree(flags[join.following])
    return (
        incoming_degree * incoming[0] == outgoing_degree * outgoing[0]
        and incoming_degree * incoming[1] == outgoing_degree * outgoing[1]
    )


def _integer_candidates(point: Point, maximum_move: float) -> Tuple[IntPoint, ...]:
    minimum_x = math.ceil(point[0] - maximum_move)
    maximum_x = math.floor(point[0] + maximum_move)
    minimum_y = math.ceil(point[1] - maximum_move)
    maximum_y = math.floor(point[1] + maximum_move)
    candidates = [
        (x, y)
        for x in range(minimum_x, maximum_x + 1)
        for y in range(minimum_y, maximum_y + 1)
        if math.dist((x, y), point) <= maximum_move + 1e-9
    ]
    candidates.sort(
        key=lambda candidate: (
            (candidate[0] - point[0]) ** 2 + (candidate[1] - point[1]) ** 2,
            candidate[0],
            candidate[1],
        )
    )
    return tuple(candidates)


def _candidate_sort_key(source: Point, candidate: IntPoint) -> Tuple[float, int, int]:
    return (
        (candidate[0] - source[0]) ** 2 + (candidate[1] - source[1]) ** 2,
        candidate[0],
        candidate[1],
    )


def _sparse_ray_candidates(
    point_index: int,
    float_coordinates: Sequence[Point],
    flags: Sequence[int],
    joins: Sequence[SmoothJoin],
    geometries: Sequence[_JoinGeometry],
    component: Sequence[int],
    domains: Mapping[int, Sequence[IntPoint]],
    maximum_move: float,
) -> Tuple[IntPoint, ...]:
    """Return a sparse large-radius domain for one off-curve control.

    A full integer disk grows quadratically with ``maximum_move`` and makes a
    shared-control component prohibitively expensive to eliminate.  Smooth
    joins constrain useful control positions much more tightly: they lie near
    a ray from the central on-curve in the source tangent direction.  Sample
    that one-dimensional locus at half-unit intervals, then add exact lattice
    rays for curve-to-line joins.  Candidate proposals from every incident
    join are unioned, so a control shared by two smooth joins is still decided
    by the exact global solver rather than greedily by either join.

    The complete three-unit neighborhood is retained.  It covers ordinary
    quantization repairs (including controls whose nearest exact solution is
    2.85 units away), while ray candidates provide bounded access to a much
    larger fallback radius without enumerating its disk.
    """

    source = float_coordinates[point_index]
    local_move = min(maximum_move, 3.0)
    candidates: Set[IntPoint] = set(_integer_candidates(source, local_move))

    def add(candidate: IntPoint) -> None:
        if math.dist(candidate, source) <= maximum_move + 1e-9:
            candidates.add(candidate)

    for join_index in component:
        join = joins[join_index]
        if point_index == join.previous:
            # incoming = central - previous, hence the control ray from the
            # central point is the negated source incoming tangent.
            ray = (
                -geometries[join_index].incoming[0],
                -geometries[join_index].incoming[1],
            )
            line_endpoint = (
                join.following if flags[join.following] & flagOnCurve else None
            )
            exact_sign = -1
        elif point_index == join.following:
            ray = geometries[join_index].outgoing
            line_endpoint = (
                join.previous if flags[join.previous] & flagOnCurve else None
            )
            exact_sign = 1
        else:
            continue

        ray_length = _length(ray)
        if ray_length < 1e-12:
            continue
        unit = (ray[0] / ray_length, ray[1] / ray_length)
        central_domain = domains[join.point]

        # Rounding a continuous tangent ray at half-unit intervals visits a
        # compact digital-line neighborhood.  Anchor the sampled length range
        # to the projection of the source control for each possible central
        # on-curve, so its size is O(maximum_move), independent of handle
        # length.
        for central in central_domain:
            source_offset = _vector(central, source)
            projected_length = _dot(source_offset, unit)
            lower = max(1.0, projected_length - maximum_move - 1.0)
            upper = max(lower, projected_length + maximum_move + 1.0)
            first_tick = math.floor(lower * 2.0)
            last_tick = math.ceil(upper * 2.0)
            for tick in range(first_tick, last_tick + 1):
                length = max(1.0, tick / 2.0)
                add(
                    (
                        otRound(central[0] + unit[0] * length),
                        otRound(central[1] + unit[1] * length),
                    )
                )

        if line_endpoint is None:
            continue

        # When the opposite segment is a line, every integer central/endpoint
        # pair defines an exact primitive lattice ray.  These candidates can
        # make the join exactly collinear; moving either on-curve by one unit
        # often reduces a long primitive vector to a short usable one.
        for central in central_domain:
            for endpoint in domains[line_endpoint]:
                if point_index == join.previous:
                    tangent = _vector(central, endpoint)
                else:
                    tangent = _vector(endpoint, central)
                divisor = math.gcd(abs(int(tangent[0])), abs(int(tangent[1])))
                if divisor == 0:
                    continue
                primitive = (
                    exact_sign * int(tangent[0]) // divisor,
                    exact_sign * int(tangent[1]) // divisor,
                )
                primitive_length = _length(primitive)
                if primitive_length < 1e-12:
                    continue
                source_step = _vector(central, source)
                projected_steps = _dot(source_step, primitive) / (
                    primitive_length * primitive_length
                )
                step_radius = maximum_move / primitive_length + 2.0
                first_step = max(1, math.floor(projected_steps - step_radius))
                last_step = max(first_step, math.ceil(projected_steps + step_radius))
                for step in range(first_step, last_step + 1):
                    add(
                        (
                            central[0] + step * primitive[0],
                            central[1] + step * primitive[1],
                        )
                    )

    return tuple(sorted(candidates, key=lambda candidate: _candidate_sort_key(source, candidate)))


def _contour_neighbors(end_points: Sequence[int], point_count: int) -> Dict[int, Tuple[int, int]]:
    neighbors: Dict[int, Tuple[int, int]] = {}
    start = 0
    for end in end_points:
        if end < start or end >= point_count:
            raise ValueError("invalid contour end point")
        size = end - start + 1
        for index in range(start, end + 1):
            offset = index - start
            neighbors[index] = (
                start + (offset - 1) % size,
                start + (offset + 1) % size,
            )
        start = end + 1
    if start != point_count:
        raise ValueError("contour end points do not cover all glyph coordinates")
    return neighbors


def _normalize_collapsed_point_triples(
    collapsed_point_triples: Iterable[Iterable[int]],
) -> Tuple[Tuple[int, int, int], ...]:
    """Normalize exact target-lattice collapse constraints.

    Each triple is ``(previous, point, following)`` and means that either the
    incoming side or the outgoing side must remain zero-length after integer
    quantization.  Keeping the disjunction is important: when all three points
    initially coincide, forcing both sides to stay collapsed can reject an
    otherwise valid neighboring tangent solution.
    """

    try:
        raw_triples = tuple(tuple(triple) for triple in collapsed_point_triples)
    except TypeError as error:
        raise TypeError(
            "collapsed_point_triples must contain iterable point triples"
        ) from error
    if any(
        len(triple) != 3
        or any(isinstance(point, bool) or not isinstance(point, Integral) for point in triple)
        for triple in raw_triples
    ):
        raise TypeError(
            "collapsed_point_triples must contain exactly three integer point indices"
        )
    normalized = tuple(tuple(int(point) for point in triple) for triple in raw_triples)
    if any(len(set(triple)) != 3 for triple in normalized):
        raise ValueError("collapsed point triples must contain three distinct indices")
    return normalized


def _normalize_connecting_point_scopes(
    connecting_point_scopes: Iterable[Iterable[int]],
) -> Tuple[Tuple[int, ...], ...]:
    try:
        raw_scopes = tuple(tuple(scope) for scope in connecting_point_scopes)
    except TypeError as error:
        raise TypeError("connecting point scopes must be iterable") from error
    if any(
        len(scope) < 2
        or any(isinstance(point, bool) or not isinstance(point, Integral) for point in scope)
        for scope in raw_scopes
    ):
        raise TypeError(
            "connecting point scopes must contain at least two integer indices"
        )
    normalized = tuple(tuple(int(point) for point in scope) for scope in raw_scopes)
    if any(len(set(scope)) != len(scope) for scope in normalized):
        raise ValueError("connecting point scopes must contain distinct indices")
    return normalized


def _join_components(
    joins: Sequence[SmoothJoin],
    collapsed_point_triples: Sequence[Sequence[int]] = (),
) -> List[List[int]]:
    """Group join factors connected by tangent or collapsed-side scopes."""

    point_to_joins: Dict[int, List[int]] = {}
    for join_index, join in enumerate(joins):
        for point in (join.previous, join.point, join.following):
            point_to_joins.setdefault(point, []).append(join_index)

    point_to_collapses: Dict[int, List[int]] = {}
    for collapse_index, triple in enumerate(collapsed_point_triples):
        for point in triple:
            point_to_collapses.setdefault(point, []).append(collapse_index)

    unseen = set(range(len(joins)))
    unseen_collapses = set(range(len(collapsed_point_triples)))
    components: List[List[int]] = []
    while unseen:
        seed = min(unseen)
        unseen.remove(seed)
        component: List[int] = []
        pending_joins = [seed]
        pending_collapses: List[int] = []
        while pending_joins or pending_collapses:
            if pending_collapses:
                collapse_index = pending_collapses.pop()
                points = collapsed_point_triples[collapse_index]
            else:
                join_index = pending_joins.pop()
                component.append(join_index)
                join = joins[join_index]
                points = (join.previous, join.point, join.following)
            for point in points:
                for neighbor in point_to_joins.get(point, ()):
                    if neighbor in unseen:
                        unseen.remove(neighbor)
                        pending_joins.append(neighbor)
                for neighbor in point_to_collapses.get(point, ()):
                    if neighbor in unseen_collapses:
                        unseen_collapses.remove(neighbor)
                        pending_collapses.append(neighbor)
        components.append(sorted(component))
    return components


def _join_state_cost(
    geometry: _JoinGeometry,
    domains: Mapping[int, Sequence[IntPoint]],
    flags: Sequence[int],
    states: StateKey,
    output_angle_limit: float,
    maximum_direction_shift: Optional[float],
    angle_cost_weight: float,
    direction_cost_weight: float,
) -> Optional[float]:
    join = geometry.metadata
    incoming_direction_shift_limit = (
        maximum_direction_shift
        if join.incoming_direction_shift_limit is None
        else join.incoming_direction_shift_limit
    )
    outgoing_direction_shift_limit = (
        maximum_direction_shift
        if join.outgoing_direction_shift_limit is None
        else join.outgoing_direction_shift_limit
    )
    previous = domains[join.previous][states[0]]
    point = domains[join.point][states[1]]
    following = domains[join.following][states[2]]
    incoming = _vector(previous, point)
    outgoing = _vector(point, following)
    if not _continuity_is_valid(incoming, outgoing, join, flags):
        return None
    if not _angle_is_within_limit(incoming, outgoing, output_angle_limit):
        return None
    # A small join angle alone is not sufficient: both candidate tangent
    # vectors could point in the opposite direction from the source and still
    # be mutually collinear.  That folds the curve back through its join.
    if (
        _dot(geometry.incoming, incoming) <= 0.0
        or _dot(geometry.outgoing, outgoing) <= 0.0
    ):
        return None
    if (
        incoming_direction_shift_limit is not None
        and not _angle_is_within_limit(
            geometry.incoming,
            incoming,
            incoming_direction_shift_limit,
        )
    ) or (
        outgoing_direction_shift_limit is not None
        and not _angle_is_within_limit(
            geometry.outgoing,
            outgoing,
            outgoing_direction_shift_limit,
        )
    ):
        return None
    angle = _angle_degrees(incoming, outgoing)
    incoming_shift = _angle_degrees(geometry.incoming, incoming)
    outgoing_shift = _angle_degrees(geometry.outgoing, outgoing)
    return (
        angle_cost_weight * angle * angle
        + direction_cost_weight
        * (incoming_shift * incoming_shift + outgoing_shift * outgoing_shift)
    )


def _valid_join_factor(
    join_index: int,
    geometry: _JoinGeometry,
    domains: Mapping[int, Sequence[IntPoint]],
    flags: Sequence[int],
    output_angle_limit: float,
    maximum_direction_shift: Optional[float],
    angle_cost_weight: float,
    direction_cost_weight: float,
    maximum_factor_entries: int,
    work_budget: _WorkBudget,
) -> Tuple[Optional[_Factor], int, bool]:
    join = geometry.metadata
    scope = (join.previous, join.point, join.following)
    table: Dict[StateKey, float] = {}
    evaluated = 0
    work_budget.consume(
        math.prod(len(domains[point]) for point in scope),
        stage="building a smooth-join factor",
    )
    for states in itertools.product(*(range(len(domains[point])) for point in scope)):
        evaluated += 1
        cost = _join_state_cost(
            geometry,
            domains,
            flags,
            states,
            output_angle_limit,
            maximum_direction_shift,
            angle_cost_weight,
            direction_cost_weight,
        )
        if cost is None:
            continue
        if len(table) >= maximum_factor_entries:
            return None, evaluated, True
        table[states] = cost
    if not table:
        return None, evaluated, False
    return _Factor(scope, table), evaluated, False


def _collapsed_join_factor(
    triple: Tuple[int, int, int],
    left_collapsed: bool,
    right_collapsed: bool,
    domains: Mapping[int, Sequence[IntPoint]],
    maximum_factor_entries: int,
    work_budget: _WorkBudget,
) -> Tuple[Optional[_Factor], bool]:
    """Build the sparse relation for the originally collapsed side(s)."""

    previous, point, following = triple
    if left_collapsed != right_collapsed:
        first, second = (
            (previous, point) if left_collapsed else (point, following)
        )
        second_states: Dict[IntPoint, List[int]] = {}
        for state, coordinate in enumerate(domains[second]):
            second_states.setdefault(coordinate, []).append(state)
        matching_states = sum(
            len(second_states.get(coordinate, ())) for coordinate in domains[first]
        )
        work_budget.consume(
            matching_states,
            stage="building a collapsed-side factor",
        )
        table = {
            (first_state, second_state): 0.0
            for first_state, coordinate in enumerate(domains[first])
            for second_state in second_states.get(coordinate, ())
        }
        if not table:
            return None, False
        if len(table) > maximum_factor_entries:
            return None, True
        return _Factor((first, second), table), False
    if not left_collapsed:
        return None, False

    previous_states: Dict[IntPoint, List[int]] = {}
    following_states: Dict[IntPoint, List[int]] = {}
    for state, coordinate in enumerate(domains[previous]):
        previous_states.setdefault(coordinate, []).append(state)
    for state, coordinate in enumerate(domains[following]):
        following_states.setdefault(coordinate, []).append(state)

    table: Dict[StateKey, float] = {}

    def add(states: StateKey) -> bool:
        work_budget.consume(stage="building a collapsed-side factor")
        if states in table:
            return True
        if len(table) >= maximum_factor_entries:
            return False
        table[states] = 0.0
        return True

    for point_state, coordinate in enumerate(domains[point]):
        for previous_state in previous_states.get(coordinate, ()):
            for following_state in range(len(domains[following])):
                if not add((previous_state, point_state, following_state)):
                    return None, True
        for following_state in following_states.get(coordinate, ()):
            for previous_state in range(len(domains[previous])):
                if not add((previous_state, point_state, following_state)):
                    return None, True
    if not table:
        return None, False
    return _Factor(triple, table), False


def _eliminate_join_center_factor(
    geometry: _JoinGeometry,
    domains: Mapping[int, Sequence[IntPoint]],
    flags: Sequence[int],
    float_coordinates: Sequence[Point],
    output_angle_limit: float,
    maximum_direction_shift: Optional[float],
    on_curve_cost_weight: float,
    angle_cost_weight: float,
    direction_cost_weight: float,
    maximum_factor_entries: int,
    work_budget: _WorkBudget,
) -> Tuple[Optional[_Factor], Optional[_EliminationRecord], int, int, bool]:
    """Eliminate a join-local on-curve without storing its ternary relation.

    A quadratic contour's explicit on-curve usually belongs to exactly one
    smooth-join constraint.  Enumerate that constraint once and immediately
    minimize over the central state, producing a compact pairwise factor over
    its neighboring controls.  The central unary movement cost is included
    here and a backpointer reconstructs the exact globally optimal state after
    the remaining component has been solved.
    """

    join = geometry.metadata
    scope = (join.previous, join.point, join.following)
    separator = (join.previous, join.following)
    source = float_coordinates[join.point]
    table: Dict[StateKey, float] = {}
    choices: Dict[StateKey, int] = {}
    evaluated = 0
    valid = 0
    work_budget.consume(
        math.prod(len(domains[point]) for point in scope),
        stage="eliminating a smooth-join center",
    )
    for states in itertools.product(*(range(len(domains[point])) for point in scope)):
        evaluated += 1
        join_cost = _join_state_cost(
            geometry,
            domains,
            flags,
            states,
            output_angle_limit,
            maximum_direction_shift,
            angle_cost_weight,
            direction_cost_weight,
        )
        if join_cost is None:
            continue
        valid += 1
        central = domains[join.point][states[1]]
        total = join_cost + on_curve_cost_weight * (
            (central[0] - source[0]) ** 2 + (central[1] - source[1]) ** 2
        )
        separator_state = (states[0], states[2])
        previous = table.get(separator_state)
        if previous is None:
            if len(table) >= maximum_factor_entries:
                return None, None, evaluated, valid, True
            table[separator_state] = total
            choices[separator_state] = states[1]
        elif total < previous - 1e-12 or (
            abs(total - previous) <= 1e-12
            and states[1] < choices[separator_state]
        ):
            table[separator_state] = total
            choices[separator_state] = states[1]
    if not table:
        return None, None, evaluated, valid, False
    return (
        _Factor(separator, table),
        _EliminationRecord(join.point, separator, choices),
        evaluated,
        valid,
        False,
    )


def _eliminate_two_center_line_block(
    left_geometry: _JoinGeometry,
    right_geometry: _JoinGeometry,
    domains: Mapping[int, Sequence[IntPoint]],
    flags: Sequence[int],
    float_coordinates: Sequence[Point],
    output_angle_limit: float,
    maximum_direction_shift: Optional[float],
    on_curve_cost_weight: float,
    angle_cost_weight: float,
    direction_cost_weight: float,
    maximum_factor_entries: int,
    work_budget: _WorkBudget,
) -> Tuple[
    Optional[_Factor],
    Optional[_BlockEliminationRecord],
    int,
    int,
    bool,
]:
    """Eliminate both endpoints of a join-local explicit line exactly.

    Two adjacent smooth constraints around an explicit line have scopes
    ``(left_control, first_center, second_center)`` and
    ``(first_center, second_center, right_control)``.  If the two centers occur
    in no other join, eliminating either center alone creates a large ternary
    intermediate even though eliminating the pair leaves only a compact factor
    over the outside controls.

    Condition on the two center states, join the two sparse relations there,
    and minimize directly into the outside-control pair.  This is ordinary
    min-sum variable elimination with the wasteful intermediate skipped; it is
    exact and records both center states for deterministic reconstruction.
    """

    left = left_geometry.metadata
    right = right_geometry.metadata
    if not (
        left.following == right.point
        and right.previous == left.point
        and flags[left.point] & flagOnCurve
        and flags[right.point] & flagOnCurve
    ):
        raise ValueError("joins do not form an oriented two-center line block")

    first_center = left.point
    second_center = right.point
    left_control = left.previous
    right_control = right.following
    if len({left_control, first_center, second_center, right_control}) != 4:
        raise ValueError("two-center line block must contain four distinct points")

    separator = (left_control, right_control)
    table: Dict[StateKey, float] = {}
    choices: Dict[StateKey, StateKey] = {}
    evaluated = 0
    valid = 0
    first_source = float_coordinates[first_center]
    second_source = float_coordinates[second_center]
    center_state_count = len(domains[first_center]) * len(domains[second_center])
    work_budget.consume(
        center_state_count
        * (len(domains[left_control]) + len(domains[right_control])),
        stage="evaluating a two-center line block",
    )

    for first_state in range(len(domains[first_center])):
        first_candidate = domains[first_center][first_state]
        first_movement_cost = on_curve_cost_weight * (
            (first_candidate[0] - first_source[0]) ** 2
            + (first_candidate[1] - first_source[1]) ** 2
        )
        for second_state in range(len(domains[second_center])):
            second_candidate = domains[second_center][second_state]
            center_cost = first_movement_cost + on_curve_cost_weight * (
                (second_candidate[0] - second_source[0]) ** 2
                + (second_candidate[1] - second_source[1]) ** 2
            )

            left_states: List[Tuple[int, float]] = []
            for left_state in range(len(domains[left_control])):
                evaluated += 1
                cost = _join_state_cost(
                    left_geometry,
                    domains,
                    flags,
                    (left_state, first_state, second_state),
                    output_angle_limit,
                    maximum_direction_shift,
                    angle_cost_weight,
                    direction_cost_weight,
                )
                if cost is not None:
                    valid += 1
                    left_states.append((left_state, cost))
            right_states: List[Tuple[int, float]] = []
            for right_state in range(len(domains[right_control])):
                evaluated += 1
                cost = _join_state_cost(
                    right_geometry,
                    domains,
                    flags,
                    (first_state, second_state, right_state),
                    output_angle_limit,
                    maximum_direction_shift,
                    angle_cost_weight,
                    direction_cost_weight,
                )
                if cost is not None:
                    valid += 1
                    right_states.append((right_state, cost))
            if not left_states or not right_states:
                continue

            center_choice = (first_state, second_state)
            work_budget.consume(
                len(left_states) * len(right_states),
                stage="joining a two-center line block",
            )
            for left_state, left_cost in left_states:
                for right_state, right_cost in right_states:
                    separator_state = (left_state, right_state)
                    total = left_cost + center_cost + right_cost
                    previous = table.get(separator_state)
                    if previous is None:
                        if len(table) >= maximum_factor_entries:
                            return None, None, evaluated, valid, True
                        table[separator_state] = total
                        choices[separator_state] = center_choice
                    elif total < previous - 1e-12 or (
                        abs(total - previous) <= 1e-12
                        and center_choice < choices[separator_state]
                    ):
                        table[separator_state] = total
                        choices[separator_state] = center_choice

    if not table:
        return None, None, evaluated, valid, False
    return (
        _Factor(separator, table),
        _BlockEliminationRecord(
            (first_center, second_center), separator, choices
        ),
        evaluated,
        valid,
        False,
    )


def _solve_factors(
    variables: Set[int],
    domains: Mapping[int, Sequence[IntPoint]],
    factors: Sequence[_Factor],
    maximum_factor_entries: int,
    work_budget: _WorkBudget,
) -> Tuple[Optional[Dict[int, int]], int, Optional[str]]:
    """Globally minimize sparse factors using exact variable elimination.

    Join factors are deliberately sparse: most triples of integer point
    candidates do not meet the tangent constraints.  Enumerating the complete
    Cartesian product of a factor bucket throws that sparsity away and becomes
    prohibitively expensive for long, closed quadratic contours.  Instead,
    condition every related factor on the eliminated variable and perform an
    exact relational join of only its populated rows.  The result is identical
    to dense min-sum elimination, while large cyclic contours stay practical.
    """

    working = list(factors)
    remaining = set(variables)
    records: List[_EliminationRecord] = []
    rows = 0

    while remaining:
        choices = []
        for variable in remaining:
            related = [factor for factor in working if variable in factor.scope]
            union = set().union(*(factor.scope for factor in related))
            entry_count = math.prod(len(domains[item]) for item in union)
            choices.append((entry_count, len(union), variable, related, union))
        _, _, variable, related, union = min(
            choices, key=lambda choice: (choice[0], choice[1], choice[2])
        )
        union_scope = tuple(sorted(union))
        separator = tuple(item for item in union_scope if item != variable)
        best: Dict[StateKey, float] = {}
        back: Dict[StateKey, int] = {}

        conditioned_factors: List[
            Tuple[Tuple[int, ...], List[Dict[StateKey, float]]]
        ] = []
        for factor in related:
            variable_position = factor.scope.index(variable)
            conditioned_scope = tuple(item for item in factor.scope if item != variable)
            state_tables: List[Dict[StateKey, float]] = [
                {} for _ in range(len(domains[variable]))
            ]
            work_budget.consume(
                len(factor.table), stage="conditioning sparse factors"
            )
            for key, cost in factor.table.items():
                conditioned_key = tuple(
                    state
                    for position, state in enumerate(key)
                    if position != variable_position
                )
                state_tables[key[variable_position]][conditioned_key] = cost
            conditioned_factors.append((conditioned_scope, state_tables))

        for variable_state in range(len(domains[variable])):
            conditioned: List[Tuple[Tuple[int, ...], Dict[StateKey, float]]] = []
            impossible_state = False
            for conditioned_scope, state_tables in conditioned_factors:
                conditioned_table = state_tables[variable_state]
                if not conditioned_table:
                    impossible_state = True
                    break
                conditioned.append((conditioned_scope, conditioned_table))
            if impossible_state:
                continue

            # Start with the smallest relation, then greedily prefer the next
            # relation sharing the most variables.  This changes performance,
            # never the exact minimum represented by the natural join.
            conditioned.sort(key=lambda item: (len(item[1]), len(item[0]), item[0]))
            partial_scope, partial_table = conditioned.pop(0)
            while conditioned:
                next_index = max(
                    range(len(conditioned)),
                    key=lambda index: (
                        len(set(partial_scope) & set(conditioned[index][0])),
                        -len(conditioned[index][1]),
                        -len(conditioned[index][0]),
                    ),
                )
                factor_scope, factor_table = conditioned.pop(next_index)
                common = tuple(item for item in partial_scope if item in factor_scope)
                partial_positions = tuple(partial_scope.index(item) for item in common)
                factor_positions = tuple(factor_scope.index(item) for item in common)
                factor_index: Dict[StateKey, List[Tuple[StateKey, float]]] = {}
                for key, cost in factor_table.items():
                    common_key = tuple(key[position] for position in factor_positions)
                    factor_index.setdefault(common_key, []).append((key, cost))

                combined_scope = partial_scope + tuple(
                    item for item in factor_scope if item not in partial_scope
                )
                appended_positions = tuple(
                    factor_scope.index(item)
                    for item in factor_scope
                    if item not in partial_scope
                )
                combined_table: Dict[StateKey, float] = {}
                for partial_key, partial_cost in partial_table.items():
                    common_key = tuple(
                        partial_key[position] for position in partial_positions
                    )
                    compatible_rows = factor_index.get(common_key, ())
                    work_budget.consume(
                        len(compatible_rows), stage="joining sparse factors"
                    )
                    for factor_key, factor_cost in compatible_rows:
                        rows += 1
                        combined_key = partial_key + tuple(
                            factor_key[position] for position in appended_positions
                        )
                        combined_cost = partial_cost + factor_cost
                        previous_cost = combined_table.get(combined_key)
                        if previous_cost is None:
                            if len(combined_table) >= maximum_factor_entries:
                                return (
                                    None,
                                    rows,
                                    "sparse join while eliminating point %d "
                                    "produced more than %d states"
                                    % (variable, maximum_factor_entries),
                                )
                            combined_table[combined_key] = combined_cost
                        elif combined_cost < previous_cost - 1e-12:
                            combined_table[combined_key] = combined_cost
                if not combined_table:
                    impossible_state = True
                    break
                partial_scope, partial_table = combined_scope, combined_table
            if impossible_state:
                continue

            partial_positions = tuple(partial_scope.index(item) for item in separator)
            for partial_key, total in partial_table.items():
                separator_key = tuple(
                    partial_key[position] for position in partial_positions
                )
                previous = best.get(separator_key)
                if previous is None or total < previous - 1e-12 or (
                    abs(total - previous) <= 1e-12
                    and variable_state < back[separator_key]
                ):
                    best[separator_key] = total
                    back[separator_key] = variable_state
            if len(best) > maximum_factor_entries:
                return (
                    None,
                    rows,
                    "eliminating point %d produced more than %d separator states"
                    % (variable, maximum_factor_entries),
                )

        if not best:
            return None, rows, "joint constraints have no compatible assignment"
        working = [factor for factor in working if factor not in related]
        working.append(_Factor(separator, best))
        records.append(_EliminationRecord(variable, separator, back))
        remaining.remove(variable)

    assignment: Dict[int, int] = {}
    for record in reversed(records):
        separator_key = tuple(assignment[variable] for variable in record.separator)
        state = record.choices.get(separator_key)
        if state is None:
            return None, rows, "could not reconstruct the minimum-cost assignment"
        assignment[record.variable] = state
    return assignment, rows, None


def _normalize_join(join: object) -> SmoothJoin:
    def point_index(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise TypeError("smooth-join point indices must be integers")
        return int(value)

    try:
        return SmoothJoin(
            previous=point_index(getattr(join, "previous")),
            point=point_index(getattr(join, "point")),
            following=point_index(getattr(join, "following")),
            continuity=str(getattr(join, "continuity", "g1")),
            label=str(getattr(join, "label", "")),
            source_angle_limit=(
                None
                if getattr(join, "source_angle_limit", None) is None
                else float(getattr(join, "source_angle_limit"))
            ),
            incoming_direction_shift_limit=(
                None
                if getattr(join, "incoming_direction_shift_limit", None) is None
                else float(getattr(join, "incoming_direction_shift_limit"))
            ),
            outgoing_direction_shift_limit=(
                None
                if getattr(join, "outgoing_direction_shift_limit", None) is None
                else float(getattr(join, "outgoing_direction_shift_limit"))
            ),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise TypeError(
            "smooth joins must be SmoothJoin instances or expose previous, point, "
            "and following attributes"
        ) from error


def smooth_join_components(
    smooth_joins: Iterable[object],
    collapsed_point_triples: Iterable[Iterable[int]] = (),
    *,
    connecting_point_scopes: Iterable[Iterable[int]] = (),
) -> Tuple[Tuple[int, ...], ...]:
    """Return independent join-index components in deterministic order.

    Two smooth joins belong to the same component whenever their three-point
    constraint scopes overlap.  Components therefore have disjoint stored
    point sets and may be quantized independently, which lets callers retain a
    conservative solution for one component while escalating only another to
    a wider candidate tier.

    The returned integers index the input iteration order.  Join-like objects
    are normalized exactly as they are by :func:`quantize_glyph_tangents`.
    """

    joins = tuple(_normalize_join(join) for join in smooth_joins)
    collapsed = _normalize_collapsed_point_triples(collapsed_point_triples)
    connecting = _normalize_connecting_point_scopes(connecting_point_scopes)
    return tuple(
        tuple(component)
        for component in _join_components(joins, (*collapsed, *connecting))
    )


def quantize_glyph_tangents(
    glyph: object,
    smooth_joins: Iterable[object],
    *,
    output_angle_limit: float = 0.25,
    source_angle_limit: float = 0.01,
    lock_on_curve_points: bool = True,
    on_curve_max_move: float = 1.0,
    off_curve_max_move: float = 3.0,
    sparse_off_curve_candidates: bool = False,
    maximum_direction_shift: Optional[float] = None,
    on_curve_cost_weight: float = 4.0,
    off_curve_cost_weight: float = 1.0,
    angle_cost_weight: float = 0.05,
    direction_cost_weight: float = 0.01,
    maximum_factor_entries: int = 2_000_000,
    maximum_work: int = 2_000_000,
    locked_points: Iterable[int] = (),
    collapsed_point_triples: Iterable[Iterable[int]] = (),
) -> TangentQuantizationResult:
    """Return an integer-coordinate copy of ``glyph`` with smooth joins retained.

    Candidate coordinates are bounded in Euclidean font units.  By default,
    on-curves are locked to ordinary ``otRound`` output; this is the most
    conservative mode because all visible endpoints and straight segments stay
    unchanged while neighboring off-curves are solved globally.  Pass
    ``lock_on_curve_points=False`` to enable the stricter joint lattice mode,
    in which on-curves may move by at most ``on_curve_max_move``.

    ``maximum_direction_shift`` is an optional extra shape guard.  It defaults
    to ``None`` because very short integer handles can rotate substantially
    while moving only one or two font units; the Euclidean movement caps and
    output join angle remain hard constraints in either mode.

    Pass ``sparse_off_curve_candidates=True`` only for a late fallback with a
    large ``off_curve_max_move``.  Instead of enumerating the complete integer
    disk for each control, this retains a three-unit local disk and adds
    orientation-preserving tangent-ray candidates, including exact primitive
    lattice rays at curve-to-line joins.  Candidate unions are still solved
    globally for every shared-point component; movement caps and all output
    postconditions are unchanged.

    The default three-unit off-curve allowance is deliberate: an exact joint
    solution for the Black ``e`` terminal needs 2.85 units, whereas its
    on-curve points remain within one unit.  Costs weight movable on-curves four
    times more heavily because moving an endpoint affects more of a Bezier
    segment than moving one control.

    All joins in a shared-point component are solved at once.  Components that
    already satisfy the requested output limit at ordinary OpenType rounding
    are left untouched.

    ``locked_points`` restricts selected stored points to their ordinary
    nearest-integer coordinates.  Callers can use this for a conservative
    retry after an otherwise valid assignment changes contour topology; the
    remaining component is still solved globally and exactly within the
    restricted domains.

    ``collapsed_point_triples`` preserves a source-smooth join whose handle is
    unrepresentably short on the target integer lattice.  For every
    ``(previous, point, following)`` triple, the exact solver requires
    ``previous == point`` or ``point == following``.  The zero-length side may
    move together with a neighboring active tangent; it is not pinned to its
    ordinary rounded coordinate.
    """

    numeric_options = {
        "output_angle_limit": output_angle_limit,
        "source_angle_limit": source_angle_limit,
        "on_curve_max_move": on_curve_max_move,
        "off_curve_max_move": off_curve_max_move,
        "on_curve_cost_weight": on_curve_cost_weight,
        "off_curve_cost_weight": off_curve_cost_weight,
        "angle_cost_weight": angle_cost_weight,
        "direction_cost_weight": direction_cost_weight,
    }
    if maximum_direction_shift is not None:
        numeric_options["maximum_direction_shift"] = maximum_direction_shift
    for option_name, option_value in numeric_options.items():
        if not math.isfinite(option_value) or option_value < 0.0:
            raise ValueError(f"{option_name} must be finite and non-negative")
    if not lock_on_curve_points and on_curve_max_move < math.sqrt(0.5) - 1e-9:
        raise ValueError("on_curve_max_move cannot always contain a nearest integer")
    if off_curve_max_move < math.sqrt(0.5) - 1e-9:
        raise ValueError("off_curve_max_move cannot always contain a nearest integer")
    if isinstance(maximum_factor_entries, bool) or not isinstance(
        maximum_factor_entries, Integral
    ):
        raise TypeError("maximum_factor_entries must be an integer")
    maximum_factor_entries = int(maximum_factor_entries)
    if maximum_factor_entries < 1:
        raise ValueError("maximum_factor_entries must be positive")
    if isinstance(maximum_work, bool) or not isinstance(maximum_work, Integral):
        raise TypeError("maximum_work must be an integer")
    maximum_work = int(maximum_work)
    if maximum_work < 1:
        raise ValueError("maximum_work must be positive")

    output_glyph = copy.deepcopy(glyph)
    joins = tuple(_normalize_join(join) for join in smooth_joins)
    collapsed = _normalize_collapsed_point_triples(collapsed_point_triples)
    raw_locked = tuple(locked_points)
    if any(
        isinstance(point, bool) or not isinstance(point, Integral)
        for point in raw_locked
    ):
        raise TypeError("locked_points must contain integer point indices")
    locked = frozenset(int(point) for point in raw_locked)
    float_coordinates: List[Point] = [
        (float(point[0]), float(point[1])) for point in glyph.coordinates
    ]
    flags = list(glyph.flags)
    nearest: List[IntPoint] = [
        (otRound(point[0]), otRound(point[1])) for point in float_coordinates
    ]
    coordinates = list(nearest)
    issues: List[QuantizationIssue] = []
    candidate_states = 0
    valid_join_states = 0
    elimination_rows = 0
    solved_components = 0
    max_direction_shift = 0.0
    work_budget = _WorkBudget(maximum_work)

    if getattr(glyph, "numberOfContours", 0) < 0:
        issues.append(
            QuantizationIssue("composite-glyph", "tangent quantization requires a simple glyph")
        )
    if len(float_coordinates) != len(flags):
        issues.append(
            QuantizationIssue("invalid-glyph", "coordinate and flag counts do not match")
        )
    invalid_locked_point = next(
        (
            point
            for point in sorted(locked)
            if point < 0 or point >= len(float_coordinates)
        ),
        None,
    )
    if invalid_locked_point is not None:
        issues.append(
            QuantizationIssue(
                "invalid-locked-point",
                f"locked point {invalid_locked_point} is outside the glyph",
                point=invalid_locked_point,
            )
        )

    neighbors: Dict[int, Tuple[int, int]] = {}
    if not issues:
        try:
            neighbors = _contour_neighbors(
                list(glyph.endPtsOfContours), len(float_coordinates)
            )
        except ValueError as error:
            issues.append(QuantizationIssue("invalid-glyph", str(error)))

    collapsed_constraints: List[
        Tuple[Tuple[int, int, int], bool, bool, Tuple[int, ...]]
    ] = []
    if not issues:
        for collapse_index, triple in enumerate(collapsed):
            previous, point, following = triple
            if any(index < 0 or index >= len(float_coordinates) for index in triple):
                issues.append(
                    QuantizationIssue(
                        "invalid-collapsed-join",
                        f"collapsed join {collapse_index} contains an out-of-range point",
                        point=point,
                    )
                )
                continue
            if not (flags[point] & flagOnCurve):
                issues.append(
                    QuantizationIssue(
                        "invalid-collapsed-join",
                        "the central collapsed point is not on-curve",
                        point=point,
                    )
                )
                continue
            if neighbors.get(point) != (previous, following):
                issues.append(
                    QuantizationIssue(
                        "invalid-collapsed-join",
                        "collapsed previous/following points are not cyclic neighbors",
                        point=point,
                    )
                )
                continue
            left_collapsed = nearest[previous] == nearest[point]
            right_collapsed = nearest[point] == nearest[following]
            if not left_collapsed and not right_collapsed:
                issues.append(
                    QuantizationIssue(
                        "source-not-collapsed",
                        "collapsed join has no zero-length side at ordinary rounding",
                        point=point,
                    )
                )
                continue
            scope = (
                triple
                if left_collapsed and right_collapsed
                else (previous, point)
                if left_collapsed
                else (point, following)
            )
            collapsed_constraints.append(
                (triple, left_collapsed, right_collapsed, scope)
            )

    geometries: List[_JoinGeometry] = []
    if not issues:
        for join_index, join in enumerate(joins):
            indices = (join.previous, join.point, join.following)
            if any(index < 0 or index >= len(float_coordinates) for index in indices):
                issues.append(
                    QuantizationIssue(
                        "invalid-join",
                        "join contains an out-of-range point index",
                        join_index,
                        join.point,
                    )
                )
                continue
            if len(set(indices)) != 3:
                issues.append(
                    QuantizationIssue(
                        "degenerate-join",
                        "join does not contain three distinct stored points",
                        join_index,
                        join.point,
                    )
                )
                continue
            if not (flags[join.point] & flagOnCurve):
                issues.append(
                    QuantizationIssue(
                        "invalid-join",
                        "the central smooth point is not on-curve",
                        join_index,
                        join.point,
                    )
                )
                continue
            expected = neighbors.get(join.point)
            if expected != (join.previous, join.following):
                issues.append(
                    QuantizationIssue(
                        "invalid-join",
                        "previous/following are not the central point's cyclic neighbors",
                        join_index,
                        join.point,
                    )
                )
                continue
            if join.continuity not in ("g1", "c1"):
                issues.append(
                    QuantizationIssue(
                        "invalid-continuity",
                        "continuity must be 'g1' or 'c1'",
                        join_index,
                        join.point,
                    )
                )
                continue
            join_source_angle_limit = (
                source_angle_limit
                if join.source_angle_limit is None
                else join.source_angle_limit
            )
            if (
                not math.isfinite(join_source_angle_limit)
                or join_source_angle_limit < 0.0
            ):
                issues.append(
                    QuantizationIssue(
                        "invalid-source-angle-limit",
                        "source angle limit must be finite and non-negative",
                        join_index,
                        join.point,
                    )
                )
                continue
            invalid_direction_limit = next(
                (
                    (side, limit)
                    for side, limit in (
                        ("incoming", join.incoming_direction_shift_limit),
                        ("outgoing", join.outgoing_direction_shift_limit),
                    )
                    if limit is not None
                    and (not math.isfinite(limit) or limit < 0.0)
                ),
                None,
            )
            if invalid_direction_limit is not None:
                issues.append(
                    QuantizationIssue(
                        "invalid-direction-shift-limit",
                        f"{invalid_direction_limit[0]} direction shift limit must "
                        "be finite and non-negative",
                        join_index,
                        join.point,
                    )
                )
                continue
            incoming, outgoing = _join_vectors(float_coordinates, join)
            source_angle = _angle_degrees(incoming, outgoing)
            if (
                not math.isfinite(source_angle)
                or _dot(incoming, outgoing) <= 0.0
                or source_angle > join_source_angle_limit + 1e-9
            ):
                issues.append(
                    QuantizationIssue(
                        "source-not-smooth",
                        "tracked source join is degenerate, reversed, or %.6f degrees"
                        % source_angle,
                        join_index,
                        join.point,
                    )
                )
                continue
            geometries.append(_JoinGeometry(join, incoming, outgoing, source_angle))

    # Geometry indices and caller join indices stay aligned unless validation
    # failed.  Abort before solving if any metadata is unsafe.
    if issues:
        output_glyph.coordinates = GlyphCoordinates(nearest)
        on_moves = [
            math.dist(nearest[index], float_coordinates[index])
            for index in range(min(len(nearest), len(flags)))
            if flags[index] & flagOnCurve
        ]
        off_moves = [
            math.dist(nearest[index], float_coordinates[index])
            for index in range(min(len(nearest), len(flags)))
            if not (flags[index] & flagOnCurve)
        ]
        metrics = TangentQuantizationMetrics(
            joins=len(joins),
            components=0,
            solved_components=0,
            adjusted_points=0,
            adjusted_on_curve_points=0,
            adjusted_off_curve_points=0,
            max_on_curve_move=max(on_moves, default=0.0),
            max_off_curve_move=max(off_moves, default=0.0),
            max_output_angle=math.inf if joins else 0.0,
            max_tangent_direction_shift=0.0,
            candidate_states=0,
            valid_join_states=0,
            elimination_rows=0,
            unresolved_joins=len(joins),
        )
        return TangentQuantizationResult(output_glyph, False, metrics, tuple(issues))

    collapsed_scopes = tuple(
        constraint[3] for constraint in collapsed_constraints
    )
    components = _join_components(joins, collapsed_scopes)
    domains: Dict[int, Tuple[IntPoint, ...]] = {}
    component_failure = False
    for component_number, component in enumerate(components):
        variables = {
            point
            for join_index in component
            for point in (
                joins[join_index].previous,
                joins[join_index].point,
                joins[join_index].following,
            )
        }
        component_collapsed: List[
            Tuple[Tuple[int, int, int], bool, bool, Tuple[int, ...]]
        ] = []
        remaining_collapsed = list(collapsed_constraints)
        while True:
            connected = [
                constraint
                for constraint in remaining_collapsed
                if variables.intersection(constraint[3])
            ]
            if not connected:
                break
            for constraint in connected:
                remaining_collapsed.remove(constraint)
                component_collapsed.append(constraint)
                variables.update(constraint[3])

        needs_solving = False
        for join_index in component:
            join = joins[join_index]
            incoming_direction_shift_limit = (
                maximum_direction_shift
                if join.incoming_direction_shift_limit is None
                else join.incoming_direction_shift_limit
            )
            outgoing_direction_shift_limit = (
                maximum_direction_shift
                if join.outgoing_direction_shift_limit is None
                else join.outgoing_direction_shift_limit
            )
            incoming, outgoing = _join_vectors(coordinates, join)
            geometry = geometries[join_index]
            incoming_shift = _angle_degrees(geometry.incoming, incoming)
            outgoing_shift = _angle_degrees(geometry.outgoing, outgoing)
            if (
                not _continuity_is_valid(incoming, outgoing, join, flags)
                or _angle_degrees(incoming, outgoing) > output_angle_limit + 1e-9
                or _dot(geometry.incoming, incoming) <= 0.0
                or _dot(geometry.outgoing, outgoing) <= 0.0
                or (
                    incoming_direction_shift_limit is not None
                    and incoming_shift > incoming_direction_shift_limit + 1e-9
                )
                or (
                    outgoing_direction_shift_limit is not None
                    and outgoing_shift > outgoing_direction_shift_limit + 1e-9
                )
            ):
                needs_solving = True
                break
        if not needs_solving:
            needs_solving = any(
                not (
                    (left and coordinates[previous] == coordinates[point])
                    or (right and coordinates[point] == coordinates[following])
                )
                for (previous, point, following), left, right, _ in component_collapsed
            )
        if not needs_solving:
            continue
        # Sparse off-curve ray domains depend on every bounded on-curve state,
        # so construct all on-curves first regardless of set iteration order.
        for point in sorted(variables):
            if point in domains or not (flags[point] & flagOnCurve):
                continue
            domains[point] = (
                (nearest[point],)
                if lock_on_curve_points or point in locked
                else _integer_candidates(float_coordinates[point], on_curve_max_move)
            )
        for point in sorted(variables):
            if point in domains:
                continue
            domains[point] = (
                (nearest[point],)
                if point in locked
                else (
                    _sparse_ray_candidates(
                        point,
                        float_coordinates,
                        flags,
                        joins,
                        geometries,
                        component,
                        domains,
                        off_curve_max_move,
                    )
                    if sparse_off_curve_candidates
                    else _integer_candidates(
                        float_coordinates[point], off_curve_max_move
                    )
                )
            )

        # A point used only by a retired collapse has no active tangent ray of
        # its own.  Share candidate coordinates across each potentially
        # collapsed side, while retaining every member's independent movement
        # cap and lock.  This lets an inactive zero-length mate follow an
        # adjacent active control without admitting an otherwise out-of-bounds
        # coordinate.
        domains_changed = True
        while domains_changed:
            domains_changed = False
            for triple, left_collapsed, right_collapsed, _ in component_collapsed:
                previous, point, following = triple
                collapsed_sides = []
                if left_collapsed:
                    collapsed_sides.append((previous, point))
                if right_collapsed:
                    collapsed_sides.append((point, following))
                for first, second in collapsed_sides:
                    shared_candidates = set(domains[first]) | set(domains[second])
                    for member in (first, second):
                        if member in locked or (
                            lock_on_curve_points and flags[member] & flagOnCurve
                        ):
                            continue
                        maximum_move = (
                            on_curve_max_move
                            if flags[member] & flagOnCurve
                            else off_curve_max_move
                        )
                        expanded = set(domains[member])
                        expanded.update(
                            candidate
                            for candidate in shared_candidates
                            if math.dist(candidate, float_coordinates[member])
                            <= maximum_move + 1e-9
                        )
                        if len(expanded) != len(domains[member]):
                            domains[member] = tuple(
                                sorted(
                                    expanded,
                                    key=lambda candidate: _candidate_sort_key(
                                        float_coordinates[member], candidate
                                    ),
                                )
                            )
                            domains_changed = True
        candidate_states += sum(len(domains[point]) for point in variables)

        point_join_counts: Dict[int, int] = {}
        point_join_incidence: Dict[int, Set[int]] = {}
        for join_index in component:
            join = joins[join_index]
            for point in (join.previous, join.point, join.following):
                point_join_counts[point] = point_join_counts.get(point, 0) + 1
                point_join_incidence.setdefault(point, set()).add(join_index)
        collapsed_points = {
            point
            for _, _, _, scope in component_collapsed
            for point in scope
        }

        # An explicit line between two tracked smooth centers creates the two
        # adjacent scopes (left_control, first_center, second_center) and
        # (first_center, second_center, right_control).  When both centers occur
        # only in those scopes, eliminate them as one exact block.  Eliminating
        # either center alone first creates a needlessly large ternary
        # intermediate on long closed contours.
        right_join_for_edge: Dict[Tuple[int, int], List[int]] = {}
        for join_index in component:
            join = joins[join_index]
            right_join_for_edge.setdefault((join.previous, join.point), []).append(
                join_index
            )
        line_blocks: List[Tuple[int, int]] = []
        block_joins: Set[int] = set()
        block_centers: Set[int] = set()
        for left_index in sorted(component):
            if left_index in block_joins:
                continue
            left = joins[left_index]
            first_center = left.point
            second_center = left.following
            if not (flags[second_center] & flagOnCurve):
                continue
            for right_index in sorted(
                right_join_for_edge.get((first_center, second_center), ())
            ):
                if right_index == left_index or right_index in block_joins:
                    continue
                right = joins[right_index]
                centers = {first_center, second_center}
                if (
                    point_join_incidence.get(first_center)
                    != {left_index, right_index}
                    or point_join_incidence.get(second_center)
                    != {left_index, right_index}
                    or centers & collapsed_points
                    or centers & block_centers
                    or len(
                        {
                            left.previous,
                            first_center,
                            second_center,
                            right.following,
                        }
                    )
                    != 4
                ):
                    continue
                line_blocks.append((left_index, right_index))
                block_joins.update((left_index, right_index))
                block_centers.update(centers)
                break
        local_centers = {
            joins[join_index].point
            for join_index in component
            if point_join_counts[joins[join_index].point] == 1
            and joins[join_index].point not in collapsed_points
        }

        factors: List[_Factor] = []
        eliminated_centers = local_centers | block_centers
        for point in sorted(variables - eliminated_centers):
            weight = (
                on_curve_cost_weight
                if flags[point] & flagOnCurve
                else off_curve_cost_weight
            )
            source = float_coordinates[point]
            factors.append(
                _Factor(
                    (point,),
                    {
                        (state,): weight
                        * (
                            (candidate[0] - source[0]) ** 2
                            + (candidate[1] - source[1]) ** 2
                        )
                        for state, candidate in enumerate(domains[point])
                    },
                )
            )

        factor_error = False
        local_records: List[object] = []
        for join_index in component:
            if join_index in block_joins:
                continue
            join = joins[join_index]
            try:
                if join.point in local_centers:
                    (
                        factor,
                        local_record,
                        evaluated,
                        valid_states,
                        factor_limited,
                    ) = _eliminate_join_center_factor(
                        geometries[join_index],
                        domains,
                        flags,
                        float_coordinates,
                        output_angle_limit,
                        maximum_direction_shift,
                        on_curve_cost_weight,
                        angle_cost_weight,
                        direction_cost_weight,
                        maximum_factor_entries,
                        work_budget,
                    )
                else:
                    factor, evaluated, factor_limited = _valid_join_factor(
                        join_index,
                        geometries[join_index],
                        domains,
                        flags,
                        output_angle_limit,
                        maximum_direction_shift,
                        angle_cost_weight,
                        direction_cost_weight,
                        maximum_factor_entries,
                        work_budget,
                    )
                    local_record = None
                    valid_states = len(factor.table) if factor is not None else 0
            except _WorkLimitExceeded as error:
                issues.append(
                    QuantizationIssue(
                        "work-limit",
                        str(error),
                        join_index,
                        join.point,
                    )
                )
                factor_error = True
                break
            if factor_limited:
                issues.append(
                    QuantizationIssue(
                        "factor-limit",
                        "join factor exceeded the populated-state safety limit",
                        join_index,
                        joins[join_index].point,
                    )
                )
                factor_error = True
            elif factor is None:
                issues.append(
                    QuantizationIssue(
                        "no-bounded-solution",
                        "join has no candidate within movement, angle, and direction limits",
                        join_index,
                        joins[join_index].point,
                    )
                )
                factor_error = True
            else:
                factors.append(factor)
                valid_join_states += valid_states
                if local_record is not None:
                    local_records.append(local_record)
            if factor_error:
                break
        if not factor_error:
            for left_index, right_index in line_blocks:
                try:
                    (
                        factor,
                        block_record,
                        evaluated,
                        valid_states,
                        factor_limited,
                    ) = _eliminate_two_center_line_block(
                        geometries[left_index],
                        geometries[right_index],
                        domains,
                        flags,
                        float_coordinates,
                        output_angle_limit,
                        maximum_direction_shift,
                        on_curve_cost_weight,
                        angle_cost_weight,
                        direction_cost_weight,
                        maximum_factor_entries,
                        work_budget,
                    )
                except _WorkLimitExceeded as error:
                    issues.append(
                        QuantizationIssue(
                            "work-limit",
                            str(error),
                            left_index,
                            joins[left_index].point,
                        )
                    )
                    factor_error = True
                    break
                if factor_limited:
                    issues.append(
                        QuantizationIssue(
                            "factor-limit",
                            "two-center line-block factor exceeded the populated-state "
                            "safety limit",
                            left_index,
                            joins[left_index].point,
                        )
                    )
                    factor_error = True
                elif factor is None or block_record is None:
                    issues.append(
                        QuantizationIssue(
                            "no-bounded-solution",
                            "two-center line block has no candidate within movement, "
                            "angle, and direction limits",
                            left_index,
                            joins[left_index].point,
                        )
                    )
                    factor_error = True
                else:
                    factors.append(factor)
                    valid_join_states += valid_states
                    local_records.append(block_record)
                if factor_error:
                    break
        if not factor_error:
            for collapse_index, constraint in enumerate(component_collapsed):
                triple, left_collapsed, right_collapsed, _ = constraint
                try:
                    factor, factor_limited = _collapsed_join_factor(
                        triple,
                        left_collapsed,
                        right_collapsed,
                        domains,
                        maximum_factor_entries,
                        work_budget,
                    )
                except _WorkLimitExceeded as error:
                    issues.append(
                        QuantizationIssue(
                            "work-limit",
                            str(error),
                            point=triple[1],
                        )
                    )
                    factor_error = True
                    break
                if factor_limited:
                    issues.append(
                        QuantizationIssue(
                            "factor-limit",
                            "collapsed-side factor exceeded the populated-state "
                            "safety limit",
                            point=triple[1],
                        )
                    )
                    factor_error = True
                elif factor is None:
                    issues.append(
                        QuantizationIssue(
                            "no-bounded-collapse",
                            f"collapsed join {collapse_index} has no bounded "
                            "zero-length side",
                            point=triple[1],
                        )
                    )
                    factor_error = True
                else:
                    factors.append(factor)
                if factor_error:
                    break
        if factor_error:
            component_failure = True
            continue

        try:
            assignment, rows, solver_error = _solve_factors(
                variables - eliminated_centers,
                domains,
                factors,
                maximum_factor_entries,
                work_budget,
            )
        except _WorkLimitExceeded as error:
            issues.append(QuantizationIssue("work-limit", str(error)))
            component_failure = True
            continue
        elimination_rows += rows
        if assignment is None:
            issues.append(
                QuantizationIssue(
                    "no-joint-solution",
                    "component %d: %s" % (component_number, solver_error),
                )
            )
            component_failure = True
            continue
        for record in local_records:
            if isinstance(record, _BlockEliminationRecord):
                separator_key = tuple(
                    assignment[point] for point in record.separator
                )
                states = record.choices.get(separator_key)
                if states is None:
                    issues.append(
                        QuantizationIssue(
                            "no-joint-solution",
                            "could not reconstruct a locally eliminated line block",
                        )
                    )
                    component_failure = True
                    break
                assignment.update(zip(record.variables, states))
                continue
            assert isinstance(record, _EliminationRecord)
            separator_key = tuple(assignment[point] for point in record.separator)
            state = record.choices.get(separator_key)
            if state is None:
                issues.append(
                    QuantizationIssue(
                        "no-joint-solution",
                        "could not reconstruct a locally eliminated join center",
                    )
                )
                component_failure = True
                break
            assignment[record.variable] = state
        if component_failure:
            continue
        for point, state in assignment.items():
            coordinates[point] = domains[point][state]
        solved_components += 1

    if not component_failure:
        for triple, left_collapsed, right_collapsed, _ in collapsed_constraints:
            previous, point, following = triple
            if not (
                (left_collapsed and coordinates[previous] == coordinates[point])
                or (
                    right_collapsed
                    and coordinates[point] == coordinates[following]
                )
            ):
                issues.append(
                    QuantizationIssue(
                        "collapsed-postcondition-failed",
                        "integer output reopened both sides of a collapsed join",
                        point=point,
                    )
                )
                component_failure = True
                break

    output_angles: List[float] = []
    unresolved = 0
    if not component_failure:
        for join_index, geometry in enumerate(geometries):
            join = geometry.metadata
            incoming_direction_shift_limit = (
                maximum_direction_shift
                if join.incoming_direction_shift_limit is None
                else join.incoming_direction_shift_limit
            )
            outgoing_direction_shift_limit = (
                maximum_direction_shift
                if join.outgoing_direction_shift_limit is None
                else join.outgoing_direction_shift_limit
            )
            incoming, outgoing = _join_vectors(coordinates, join)
            angle = _angle_degrees(incoming, outgoing)
            output_angles.append(angle)
            incoming_shift = _angle_degrees(geometry.incoming, incoming)
            outgoing_shift = _angle_degrees(geometry.outgoing, outgoing)
            max_direction_shift = max(
                max_direction_shift,
                incoming_shift,
                outgoing_shift,
            )
            if (
                not _continuity_is_valid(incoming, outgoing, join, flags)
                or angle > output_angle_limit + 1e-9
                or _dot(geometry.incoming, incoming) <= 0.0
                or _dot(geometry.outgoing, outgoing) <= 0.0
                or (
                    incoming_direction_shift_limit is not None
                    and incoming_shift > incoming_direction_shift_limit + 1e-9
                )
                or (
                    outgoing_direction_shift_limit is not None
                    and outgoing_shift > outgoing_direction_shift_limit + 1e-9
                )
            ):
                unresolved += 1
                issues.append(
                    QuantizationIssue(
                        "postcondition-failed",
                        "output join violates continuity, orientation, or direction limits "
                        "(%.6f degrees)" % angle,
                        join_index,
                        join.point,
                    )
                )
    else:
        unresolved = len(joins)

    success = not issues and unresolved == 0
    final_coordinates = coordinates if success else nearest
    output_glyph.coordinates = GlyphCoordinates(final_coordinates)

    adjusted_indices = [
        index
        for index, (point, baseline) in enumerate(zip(final_coordinates, nearest))
        if point != baseline
    ]
    on_moves = [
        math.dist(final_coordinates[index], float_coordinates[index])
        for index in range(len(final_coordinates))
        if flags[index] & flagOnCurve
    ]
    off_moves = [
        math.dist(final_coordinates[index], float_coordinates[index])
        for index in range(len(final_coordinates))
        if not (flags[index] & flagOnCurve)
    ]
    metrics = TangentQuantizationMetrics(
        joins=len(joins),
        components=len(components),
        solved_components=solved_components if success else 0,
        adjusted_points=len(adjusted_indices),
        adjusted_on_curve_points=sum(
            bool(flags[index] & flagOnCurve) for index in adjusted_indices
        ),
        adjusted_off_curve_points=sum(
            not bool(flags[index] & flagOnCurve) for index in adjusted_indices
        ),
        max_on_curve_move=max(on_moves, default=0.0),
        max_off_curve_move=max(off_moves, default=0.0),
        max_output_angle=max(output_angles, default=0.0) if success else math.inf,
        max_tangent_direction_shift=max_direction_shift if success else math.inf,
        candidate_states=candidate_states,
        valid_join_states=valid_join_states,
        elimination_rows=elimination_rows,
        unresolved_joins=unresolved,
    )
    return TangentQuantizationResult(output_glyph, success, metrics, tuple(issues))


__all__ = [
    "QuantizationIssue",
    "SmoothJoin",
    "TangentQuantizationMetrics",
    "TangentQuantizationResult",
    "join_angle_degrees",
    "quantize_glyph_tangents",
    "smooth_join_components",
]
