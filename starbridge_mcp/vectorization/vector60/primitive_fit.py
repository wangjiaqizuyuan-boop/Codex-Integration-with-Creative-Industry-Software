from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

from .geometry_backend import (
    analyze_path,
    geometry_dependencies_available,
    has_self_intersections,
    parse_safe_path,
    sampled_contour_error,
)


class PrimitiveKind(str, Enum):
    """Vector60 primitives that may replace an existing path."""

    LINE = "line"
    RECTANGLE = "rectangle"
    ROUNDED_RECTANGLE = "rounded_rectangle"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    REGULAR_POLYGON = "regular_polygon"


SUPPORTED_PRIMITIVES = frozenset(PrimitiveKind)
_SAFE_PATH_DATA = re.compile(r"[MLCZ0-9eE+.\-\s]+\Z")


@dataclass(frozen=True)
class TopologySignature:
    """Topology evidence that must remain identical across a replacement."""

    closed: bool
    subpaths: int
    holes: int
    parent_ids: tuple[str | None, ...] = ()
    winding: tuple[int, ...] = ()

    def is_valid(self) -> bool:
        if (
            type(self.closed) is not bool
            or type(self.subpaths) is not int
            or type(self.holes) is not int
            or not isinstance(self.parent_ids, tuple)
            or not isinstance(self.winding, tuple)
        ):
            return False
        if self.subpaths < 1 or self.holes < 0 or self.holes >= self.subpaths:
            return False
        if len(self.parent_ids) != self.subpaths:
            return False
        if len(self.winding) != self.subpaths:
            return False
        return all(value in {-1, 1} for value in self.winding)


@dataclass(frozen=True)
class PrimitiveFitLimits:
    maximum_contour_error_px: float = 0.5
    maximum_area_error_ratio: float = 0.01

    def is_valid(self) -> bool:
        try:
            return (
                math.isfinite(self.maximum_contour_error_px)
                and self.maximum_contour_error_px >= 0
                and math.isfinite(self.maximum_area_error_ratio)
                and 0 <= self.maximum_area_error_ratio <= 1
            )
        except TypeError:
            return False


@dataclass(frozen=True)
class PrimitiveProposal:
    """A fitted path plus geometry evidence produced by a fitter.

    The fitter may use svgpathtools or another audited implementation. This
    module deliberately treats it as an injectable producer and owns the
    fail-closed replacement decision.
    """

    kind: PrimitiveKind | str
    path_data: str
    contour_error_px: float
    area_error_ratio: float
    topology: TopologySignature


@dataclass(frozen=True)
class FinalRenderEvidence:
    """Evidence that the proposed path passed a real original-size render gate."""

    source_width: int
    source_height: int
    rendered_width: int
    rendered_height: int
    passed: bool

    @property
    def is_original_resolution(self) -> bool:
        return (
            type(self.source_width) is int
            and type(self.source_height) is int
            and type(self.rendered_width) is int
            and type(self.rendered_height) is int
            and self.source_width > 0
            and self.source_height > 0
            and self.rendered_width == self.source_width
            and self.rendered_height == self.source_height
        )


@dataclass(frozen=True)
class PrimitiveFitResult:
    path_data: str
    replaced: bool
    primitive: PrimitiveKind | None
    gates: Mapping[str, bool]
    rejection_reasons: tuple[str, ...]


RenderGate = Callable[[str], FinalRenderEvidence]


_KAPPA = 0.5522847498307936


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite primitive coordinate")
    if abs(value) < 1e-9:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _point(value: complex) -> str:
    return f"{_number(value.real)} {_number(value.imag)}"


def _rectangle_path(xmin: float, ymin: float, xmax: float, ymax: float) -> str:
    return (
        f"M {_number(xmin)} {_number(ymin)} L {_number(xmax)} {_number(ymin)} "
        f"L {_number(xmax)} {_number(ymax)} L {_number(xmin)} {_number(ymax)} Z"
    )


def _ellipse_path(xmin: float, ymin: float, xmax: float, ymax: float) -> str:
    center_x = (xmin + xmax) / 2
    center_y = (ymin + ymax) / 2
    radius_x = (xmax - xmin) / 2
    radius_y = (ymax - ymin) / 2
    control_x = radius_x * _KAPPA
    control_y = radius_y * _KAPPA
    return (
        f"M {_number(center_x + radius_x)} {_number(center_y)} "
        f"C {_number(center_x + radius_x)} {_number(center_y + control_y)} "
        f"{_number(center_x + control_x)} {_number(center_y + radius_y)} "
        f"{_number(center_x)} {_number(center_y + radius_y)} "
        f"C {_number(center_x - control_x)} {_number(center_y + radius_y)} "
        f"{_number(center_x - radius_x)} {_number(center_y + control_y)} "
        f"{_number(center_x - radius_x)} {_number(center_y)} "
        f"C {_number(center_x - radius_x)} {_number(center_y - control_y)} "
        f"{_number(center_x - control_x)} {_number(center_y - radius_y)} "
        f"{_number(center_x)} {_number(center_y - radius_y)} "
        f"C {_number(center_x + control_x)} {_number(center_y - radius_y)} "
        f"{_number(center_x + radius_x)} {_number(center_y - control_y)} "
        f"{_number(center_x + radius_x)} {_number(center_y)} Z"
    )


def _rounded_rectangle_path(
    xmin: float, ymin: float, xmax: float, ymax: float, radius: float
) -> str:
    radius = min(radius, (xmax - xmin) / 2, (ymax - ymin) / 2)
    control = radius * _KAPPA
    return (
        f"M {_number(xmin + radius)} {_number(ymin)} "
        f"L {_number(xmax - radius)} {_number(ymin)} "
        f"C {_number(xmax - radius + control)} {_number(ymin)} "
        f"{_number(xmax)} {_number(ymin + radius - control)} "
        f"{_number(xmax)} {_number(ymin + radius)} "
        f"L {_number(xmax)} {_number(ymax - radius)} "
        f"C {_number(xmax)} {_number(ymax - radius + control)} "
        f"{_number(xmax - radius + control)} {_number(ymax)} "
        f"{_number(xmax - radius)} {_number(ymax)} "
        f"L {_number(xmin + radius)} {_number(ymax)} "
        f"C {_number(xmin + radius - control)} {_number(ymax)} "
        f"{_number(xmin)} {_number(ymax - radius + control)} "
        f"{_number(xmin)} {_number(ymax - radius)} "
        f"L {_number(xmin)} {_number(ymin + radius)} "
        f"C {_number(xmin)} {_number(ymin + radius - control)} "
        f"{_number(xmin + radius - control)} {_number(ymin)} "
        f"{_number(xmin + radius)} {_number(ymin)} Z"
    )


def _regular_polygon_path(vertices: tuple[complex, ...]) -> str | None:
    if not 3 <= len(vertices) <= 12:
        return None
    center = sum(vertices) / len(vertices)
    radii = tuple(abs(vertex - center) for vertex in vertices)
    sides = tuple(
        abs(vertices[(index + 1) % len(vertices)] - vertex) for index, vertex in enumerate(vertices)
    )
    average_radius = sum(radii) / len(radii)
    average_side = sum(sides) / len(sides)
    if average_radius <= 0 or average_side <= 0:
        return None
    if max(abs(radius - average_radius) for radius in radii) > average_radius * 0.05:
        return None
    if max(abs(side - average_side) for side in sides) > average_side * 0.05:
        return None
    phase = math.atan2((vertices[0] - center).imag, (vertices[0] - center).real)
    fitted = tuple(
        center
        + average_radius
        * complex(
            math.cos(phase + index * math.tau / len(vertices)),
            math.sin(phase + index * math.tau / len(vertices)),
        )
        for index in range(len(vertices))
    )
    return (
        f"M {_point(fitted[0])} " + " ".join(f"L {_point(vertex)}" for vertex in fitted[1:]) + " Z"
    )


def _rounded_radius(parsed, bounds: tuple[float, float, float, float]) -> float | None:
    xmin, ymin, xmax, ymax = bounds
    offsets: list[float] = []
    for segment in parsed:
        for point in (segment.start, segment.end):
            if abs(point.imag - ymin) <= 1e-6 or abs(point.imag - ymax) <= 1e-6:
                offsets.extend((point.real - xmin, xmax - point.real))
            if abs(point.real - xmin) <= 1e-6 or abs(point.real - xmax) <= 1e-6:
                offsets.extend((point.imag - ymin, ymax - point.imag))
    maximum = min(xmax - xmin, ymax - ymin) / 2
    usable = tuple(offset for offset in offsets if 1e-6 < offset < maximum - 1e-6)
    return min(usable) if usable else None


def _area_error(original_path_data: str, candidate_path_data: str) -> float:
    original_area = analyze_path(original_path_data).area
    candidate_area = analyze_path(candidate_path_data).area
    if original_area <= 1e-9:
        return 0.0 if candidate_area <= 1e-9 else math.inf
    return float(abs(candidate_area - original_area) / original_area)


def _proposal(
    *,
    kind: PrimitiveKind,
    original_path_data: str,
    candidate_path_data: str,
    topology: TopologySignature,
    sample_count: int,
) -> PrimitiveProposal:
    return PrimitiveProposal(
        kind=kind,
        path_data=candidate_path_data,
        contour_error_px=sampled_contour_error(
            original_path_data, candidate_path_data, sample_count=sample_count
        ),
        area_error_ratio=_area_error(original_path_data, candidate_path_data),
        topology=topology,
    )


def generate_primitive_proposals(
    *,
    original_path_data: str,
    original_topology: TopologySignature,
    sample_count: int = 128,
) -> tuple[PrimitiveProposal, ...]:
    """Fit audited primitives with svgpathtools, returning no proposal on uncertainty.

    These are geometry proposals only. ``apply_primitive_fit`` still requires
    contour, area, topology, and original-resolution render evidence before a
    proposal can replace the source path.
    """

    if (
        not geometry_dependencies_available()
        or not isinstance(original_topology, TopologySignature)
        or not original_topology.is_valid()
    ):
        return ()
    try:
        parsed = parse_safe_path(original_path_data)
        analysis = analyze_path(original_path_data)
        if analysis.subpaths != 1 or has_self_intersections(original_path_data):
            return ()
        if analysis.closed != original_topology.closed:
            return ()
        xmin, ymin, xmax, ymax = analysis.bounds
        if xmax - xmin <= 1e-9 or ymax - ymin <= 1e-9:
            if original_topology.closed:
                return ()
        candidates: list[tuple[PrimitiveKind, str]] = []
        if not original_topology.closed:
            candidates.append(
                (PrimitiveKind.LINE, f"M {_point(parsed[0].start)} L {_point(parsed[-1].end)}")
            )
        else:
            candidates.append((PrimitiveKind.RECTANGLE, _rectangle_path(xmin, ymin, xmax, ymax)))
            # Axis-boundary endpoints expose the corner radius without trusting
            # path metadata. It is merely a proposal; all four gates remain
            # authoritative.
            radius = _rounded_radius(parsed, analysis.bounds)
            if radius is not None:
                candidates.append(
                    (
                        PrimitiveKind.ROUNDED_RECTANGLE,
                        _rounded_rectangle_path(xmin, ymin, xmax, ymax, radius),
                    )
                )
            ellipse = _ellipse_path(xmin, ymin, xmax, ymax)
            candidates.append((PrimitiveKind.ELLIPSE, ellipse))
            if abs((xmax - xmin) - (ymax - ymin)) <= max(xmax - xmin, ymax - ymin) * 0.01:
                candidates.append((PrimitiveKind.CIRCLE, ellipse))
            if all(segment.__class__.__name__ == "Line" for segment in parsed):
                vertices = tuple(segment.start for segment in parsed)
                polygon = _regular_polygon_path(vertices)
                if polygon is not None:
                    candidates.append((PrimitiveKind.REGULAR_POLYGON, polygon))
        proposals = tuple(
            _proposal(
                kind=kind,
                original_path_data=original_path_data,
                candidate_path_data=path_data,
                topology=original_topology,
                sample_count=sample_count,
            )
            for kind, path_data in candidates
        )
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return ()
    return tuple(
        proposal
        for proposal in proposals
        if _finite_nonnegative(proposal.contour_error_px)
        and _finite_nonnegative(proposal.area_error_ratio)
    )


def fit_best_primitive(
    *,
    original_path_data: str,
    original_topology: TopologySignature,
    render_gate: RenderGate | None,
    limits: PrimitiveFitLimits = PrimitiveFitLimits(),
    sample_count: int = 128,
) -> PrimitiveFitResult:
    """Try fitted proposals in measured-error order and otherwise pass through."""

    proposals = generate_primitive_proposals(
        original_path_data=original_path_data,
        original_topology=original_topology,
        sample_count=sample_count,
    )
    ordered = sorted(
        proposals,
        key=lambda item: (item.contour_error_px, item.area_error_ratio, item.kind.value),
    )
    last_result: PrimitiveFitResult | None = None
    for proposal in ordered:
        result = apply_primitive_fit(
            original_path_data=original_path_data,
            original_topology=original_topology,
            proposal=proposal,
            render_gate=render_gate,
            limits=limits,
        )
        if result.replaced:
            return result
        last_result = result
    if last_result is not None:
        return PrimitiveFitResult(
            original_path_data,
            False,
            None,
            last_result.gates,
            ("no_primitive_passed",),
        )
    return PrimitiveFitResult(
        original_path_data,
        False,
        None,
        {
            "supported_primitive": False,
            "safe_path_data": False,
            "contour_error": False,
            "area_error": False,
            "topology": False,
            "final_render": False,
        },
        ("geometry_backend_unavailable_or_no_safe_proposal",),
    )


def _safe_path_data(path_data: str, topology: TopologySignature) -> bool:
    if not isinstance(path_data, str):
        return False
    stripped = path_data.strip()
    if not stripped or not _SAFE_PATH_DATA.fullmatch(stripped) or not stripped.startswith("M"):
        return False
    subpaths = stripped.count("M")
    closures = stripped.count("Z")
    if subpaths != topology.subpaths:
        return False
    return closures == subpaths if topology.closed else closures == 0


def _compatible_topology(kind: PrimitiveKind, topology: TopologySignature) -> bool:
    if not topology.is_valid():
        return False
    if kind is PrimitiveKind.LINE:
        return not topology.closed and topology.subpaths == 1 and topology.holes == 0
    return topology.closed


def _finite_nonnegative(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def apply_primitive_fit(
    *,
    original_path_data: str,
    original_topology: TopologySignature,
    proposal: PrimitiveProposal,
    render_gate: RenderGate | None,
    limits: PrimitiveFitLimits = PrimitiveFitLimits(),
) -> PrimitiveFitResult:
    """Return the proposed primitive only when every mandatory gate passes.

    Rendering is intentionally injected so the pipeline can use its verified
    SVG renderer. Missing or malformed evidence never authorizes replacement.
    """

    gates = {
        "supported_primitive": False,
        "safe_path_data": False,
        "contour_error": False,
        "area_error": False,
        "topology": False,
        "final_render": False,
    }
    reasons: list[str] = []

    try:
        kind = PrimitiveKind(proposal.kind)
    except (TypeError, ValueError):
        reasons.append("unsupported_primitive")
        return PrimitiveFitResult(original_path_data, False, None, gates, tuple(reasons))
    gates["supported_primitive"] = kind in SUPPORTED_PRIMITIVES

    gates["safe_path_data"] = isinstance(proposal.topology, TopologySignature) and _safe_path_data(
        proposal.path_data, proposal.topology
    )
    if not gates["safe_path_data"]:
        reasons.append("unsafe_path_data")

    contour_error = proposal.contour_error_px
    gates["contour_error"] = (
        limits.is_valid()
        and _finite_nonnegative(contour_error)
        and contour_error <= limits.maximum_contour_error_px
    )
    if not gates["contour_error"]:
        reasons.append("contour_error_gate")

    area_error = proposal.area_error_ratio
    gates["area_error"] = (
        limits.is_valid()
        and _finite_nonnegative(area_error)
        and area_error <= limits.maximum_area_error_ratio
    )
    if not gates["area_error"]:
        reasons.append("area_error_gate")

    gates["topology"] = (
        isinstance(original_topology, TopologySignature)
        and isinstance(proposal.topology, TopologySignature)
        and original_topology.is_valid()
        and proposal.topology == original_topology
        and _compatible_topology(kind, proposal.topology)
    )
    if not gates["topology"]:
        reasons.append("topology_gate")

    preliminary = all(gates[name] for name in gates if name != "final_render")
    if preliminary and render_gate is not None:
        try:
            render_evidence = render_gate(proposal.path_data)
        except Exception:
            reasons.append("final_render_error")
        else:
            gates["final_render"] = (
                isinstance(render_evidence, FinalRenderEvidence)
                and render_evidence.is_original_resolution
                and render_evidence.passed is True
            )
            if not gates["final_render"]:
                reasons.append("final_render_gate")
    elif preliminary:
        reasons.append("final_render_evidence_missing")

    if not all(gates.values()):
        return PrimitiveFitResult(original_path_data, False, kind, gates, tuple(reasons))
    return PrimitiveFitResult(proposal.path_data, True, kind, gates, ())
