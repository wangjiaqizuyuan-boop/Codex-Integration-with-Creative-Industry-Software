from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

try:  # Optional Vector60 extra; callers must fail closed when it is absent.
    import pathops as _pathops
except (ImportError, OSError):  # pragma: no cover - platform wheel availability
    _pathops = None

try:  # Optional Vector60 extra; callers must fail closed when it is absent.
    from svgpathtools import CubicBezier as _CubicBezier
    from svgpathtools import Line as _Line
    from svgpathtools import Path as _SvgPath
    from svgpathtools import parse_path as _parse_path
except (ImportError, OSError):  # pragma: no cover - platform wheel availability
    _CubicBezier = None
    _Line = None
    _SvgPath = None
    _parse_path = None


_SAFE_PATH_DATA = re.compile(r"[MLCZ0-9eE+.\-\s]+\Z")
_EPSILON = 1e-9


class GeometryDependencyUnavailable(RuntimeError):
    """Raised internally when the pinned Vector60 geometry extra is absent."""


class BooleanOperation(str, Enum):
    UNION = "union"
    INTERSECTION = "intersection"
    DIFFERENCE = "difference"
    XOR = "xor"


@dataclass(frozen=True)
class PathAnalysis:
    area: float
    bounds: tuple[float, float, float, float]
    closed: bool
    subpaths: int
    winding: tuple[int, ...]


def geometry_dependencies_available() -> bool:
    return all(
        dependency is not None
        for dependency in (_pathops, _CubicBezier, _Line, _SvgPath, _parse_path)
    )


def _require_dependencies() -> None:
    if not geometry_dependencies_available():
        raise GeometryDependencyUnavailable("Vector60 geometry extra is unavailable")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite SVG coordinate")
    if abs(value) < _EPSILON:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def parse_safe_path(path_data: str):
    """Parse the production M/L/C/Z dialect, rejecting every other SVG command."""

    _require_dependencies()
    if not isinstance(path_data, str):
        raise ValueError("path data must be text")
    stripped = path_data.strip()
    if not stripped or not _SAFE_PATH_DATA.fullmatch(stripped) or not stripped.startswith("M"):
        raise ValueError("path data is outside the Vector60 safe dialect")
    parsed = _parse_path(stripped)
    if not parsed:
        raise ValueError("path is empty")
    if any(not isinstance(segment, (_Line, _CubicBezier)) for segment in parsed):
        raise ValueError("path contains an unsupported segment")
    coordinates = (
        coordinate
        for segment in parsed
        for point in (
            segment.start,
            segment.end,
            *(
                getattr(segment, name)
                for name in ("control1", "control2")
                if hasattr(segment, name)
            ),
        )
        for coordinate in (point.real, point.imag)
    )
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError("path contains a non-finite coordinate")
    return parsed


def analyze_path(path_data: str) -> PathAnalysis:
    parsed = parse_safe_path(path_data)
    subpaths = parsed.continuous_subpaths()
    if not subpaths:
        raise ValueError("path has no continuous subpaths")
    closed = all(subpath.isclosed() for subpath in subpaths)
    signed_areas = tuple(float(subpath.area()) for subpath in subpaths) if closed else ()
    area = sum(abs(value) for value in signed_areas)
    winding = tuple(1 if value > 0 else -1 if value < 0 else 0 for value in signed_areas)
    xmin, xmax, ymin, ymax = parsed.bbox()
    values = (area, xmin, xmax, ymin, ymax)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("path analysis is non-finite")
    return PathAnalysis(area, (xmin, ymin, xmax, ymax), closed, len(subpaths), winding)


def _svg_to_skia(path_data: str):
    parsed = parse_safe_path(path_data)
    output = _pathops.Path()
    for subpath in parsed.continuous_subpaths():
        output.moveTo(subpath[0].start.real, subpath[0].start.imag)
        for segment in subpath:
            if isinstance(segment, _Line):
                output.lineTo(segment.end.real, segment.end.imag)
            elif isinstance(segment, _CubicBezier):
                output.cubicTo(
                    segment.control1.real,
                    segment.control1.imag,
                    segment.control2.real,
                    segment.control2.imag,
                    segment.end.real,
                    segment.end.imag,
                )
            else:  # Defensive: parse_safe_path already rejects this.
                raise ValueError("unsupported segment")
        if subpath.isclosed():
            output.close()
    return output


def _skia_to_svg(path) -> str:
    commands: list[str] = []
    for verb, points in path.segments:
        if verb == "moveTo":
            (point,) = points
            commands.append(f"M {_number(point[0])} {_number(point[1])}")
        elif verb == "lineTo":
            (point,) = points
            commands.append(f"L {_number(point[0])} {_number(point[1])}")
        elif verb == "curveTo":
            first, second, end = points
            commands.append(
                "C "
                f"{_number(first[0])} {_number(first[1])} "
                f"{_number(second[0])} {_number(second[1])} "
                f"{_number(end[0])} {_number(end[1])}"
            )
        elif verb == "closePath":
            commands.append("Z")
        else:
            raise ValueError("pathops emitted a command outside the safe dialect")
    result = " ".join(commands)
    parse_safe_path(result)
    return result


def pathops_boolean(operation: BooleanOperation | str, path_data: Sequence[str]) -> str:
    """Run one audited skia-pathops boolean and return safe M/L/C/Z data."""

    _require_dependencies()
    try:
        requested = BooleanOperation(operation)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported path boolean") from error
    if not path_data:
        raise ValueError("a path boolean needs input")
    first_analysis = analyze_path(path_data[0])
    clockwise = bool(first_analysis.winding and first_analysis.winding[0] < 0)
    paths = [_svg_to_skia(item) for item in path_data]
    result = paths[0]
    operator = {
        BooleanOperation.UNION: _pathops.PathOp.UNION,
        BooleanOperation.INTERSECTION: _pathops.PathOp.INTERSECTION,
        BooleanOperation.DIFFERENCE: _pathops.PathOp.DIFFERENCE,
        BooleanOperation.XOR: _pathops.PathOp.XOR,
    }[requested]
    for operand in paths[1:]:
        result = _pathops.op(result, operand, operator, clockwise=clockwise)
    if not list(result.segments):
        raise ValueError("path boolean produced an empty path")
    return _skia_to_svg(result)


def sampled_points(path_data: str, sample_count: int = 128) -> tuple[complex, ...]:
    parsed = parse_safe_path(path_data)
    if type(sample_count) is not int or sample_count < 8 or sample_count > 4096:
        raise ValueError("sample count is outside the audited range")
    return tuple(parsed.point(index / sample_count) for index in range(sample_count + 1))


def sampled_contour_error(first: str, second: str, sample_count: int = 128) -> float:
    """Return a conservative symmetric sampled Hausdorff distance in SVG pixels."""

    first_points = sampled_points(first, sample_count)
    second_points = sampled_points(second, sample_count)

    def directed(source: Iterable[complex], target: Sequence[complex]) -> float:
        return max(min(abs(point - other) for other in target) for point in source)

    return max(directed(first_points, second_points), directed(second_points, first_points))


def has_self_intersections(path_data: str) -> bool:
    """Use svgpathtools intersections to reject topology-ambiguous contours."""

    parsed = parse_safe_path(path_data)
    segments = tuple(parsed)
    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            intersections = first.intersect(segments[second_index])
            adjacent = second_index == first_index + 1
            closing_pair = (
                first_index == 0 and second_index == len(segments) - 1 and parsed.isclosed()
            )
            allowed = (
                all(
                    abs(first_time - 1) <= 1e-8 and abs(second_time) <= 1e-8
                    for first_time, second_time in intersections
                )
                if adjacent
                else all(
                    abs(first_time) <= 1e-8 and abs(second_time - 1) <= 1e-8
                    for first_time, second_time in intersections
                )
                if closing_pair
                else not intersections
            )
            if not allowed:
                return True
    return False


def replace_endpoints(
    path_data: str, replacements: Sequence[tuple[complex, complex]], tolerance: float = 1e-7
) -> str:
    """Move only explicitly matched vertices and adjacent controls; never dilate a contour."""

    parsed = parse_safe_path(path_data)

    def replacement(point: complex) -> complex:
        for original, target in replacements:
            if abs(point - original) <= tolerance:
                return target
        return point

    output = _SvgPath()
    for segment in parsed:
        start = replacement(segment.start)
        end = replacement(segment.end)
        start_delta = start - segment.start
        end_delta = end - segment.end
        if isinstance(segment, _Line):
            output.append(_Line(start, end))
        elif isinstance(segment, _CubicBezier):
            output.append(
                _CubicBezier(
                    start,
                    segment.control1 + start_delta,
                    segment.control2 + end_delta,
                    end,
                )
            )
        else:  # Defensive: parse_safe_path already rejects this.
            raise ValueError("unsupported segment")
    commands: list[str] = []
    for subpath in output.continuous_subpaths():
        commands.append(f"M {_number(subpath[0].start.real)} {_number(subpath[0].start.imag)}")
        for index, segment in enumerate(subpath):
            if (
                subpath.isclosed()
                and index == len(subpath) - 1
                and isinstance(segment, _Line)
                and abs(segment.end - subpath[0].start) <= tolerance
            ):
                continue
            if isinstance(segment, _Line):
                commands.append(f"L {_number(segment.end.real)} {_number(segment.end.imag)}")
            else:
                commands.append(
                    "C "
                    f"{_number(segment.control1.real)} {_number(segment.control1.imag)} "
                    f"{_number(segment.control2.real)} {_number(segment.control2.imag)} "
                    f"{_number(segment.end.real)} {_number(segment.end.imag)}"
                )
        if subpath.isclosed():
            commands.append("Z")
    result = " ".join(commands)
    parse_safe_path(result)
    return result
