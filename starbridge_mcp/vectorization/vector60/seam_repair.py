from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from itertools import combinations

from .geometry_backend import (
    BooleanOperation,
    GeometryDependencyUnavailable,
    analyze_path,
    geometry_dependencies_available,
    parse_safe_path,
    pathops_boolean,
    replace_endpoints,
)
from .primitive_fit import TopologySignature

MAXIMUM_VERTEX_SNAP_PX = 0.8
MAXIMUM_SAFE_OVERLAP_PX = 0.4
DEFAULT_MAXIMUM_DELTA_E = 2.3
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_SAFE_PATH_DATA = re.compile(r"[MLCZ0-9eE+.\-\s]+\Z")


class SeamOperation(str, Enum):
    SNAP = "snap"
    UNION = "union"
    SAFE_OVERLAP = "safe_overlap"


@dataclass(frozen=True)
class RegionSnapshot:
    region_id: str
    fill: str
    topology: TopologySignature
    path_data: str = ""


@dataclass(frozen=True)
class SeamRepairProposal:
    operation: SeamOperation | str
    region_ids: tuple[str, ...]
    local: bool = True
    snap_distance_px: float = 0.0
    overlap_px: float = 0.0
    bottom_region_id: str | None = None
    overlap_patch_path_data: str | None = None


@dataclass(frozen=True)
class RepairCandidate:
    """Pure geometry output returned by an injected path operation."""

    path_data_by_region: Mapping[str, str]
    topology_by_region: Mapping[str, TopologySignature]
    affected_region_ids: tuple[str, ...]
    removed_region_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderMetrics:
    ssim: float
    normalized_mae: float
    edge_dice: float

    def is_valid(self) -> bool:
        try:
            return all(
                math.isfinite(value) and 0 <= value <= 1
                for value in (self.ssim, self.normalized_mae, self.edge_dice)
            )
        except TypeError:
            return False


@dataclass(frozen=True)
class RepairRenderEvidence:
    source_width: int
    source_height: int
    rendered_width: int
    rendered_height: int
    before: RenderMetrics
    after: RenderMetrics

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

    @property
    def is_not_degraded(self) -> bool:
        return (
            self.before.is_valid()
            and self.after.is_valid()
            and self.after.ssim >= self.before.ssim
            and self.after.normalized_mae <= self.before.normalized_mae
            and self.after.edge_dice >= self.before.edge_dice
        )


@dataclass(frozen=True)
class SeamRepairResult:
    applied: bool
    candidate: RepairCandidate | None
    operation: SeamOperation | None
    delta_e: float | None
    gates: Mapping[str, bool]
    rejection_reasons: tuple[str, ...]


CandidateBuilder = Callable[[SeamRepairProposal], RepairCandidate]
RenderEvaluator = Callable[[RepairCandidate], RepairRenderEvidence]


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


def _srgb_channel(value: int) -> float:
    channel = value / 255.0
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _lab(fill: str) -> tuple[float, float, float]:
    if not isinstance(fill, str) or not _HEX_COLOR.fullmatch(fill):
        raise ValueError("fill must be an opaque #RRGGBB color")
    red = _srgb_channel(int(fill[1:3], 16))
    green = _srgb_channel(int(fill[3:5], 16))
    blue = _srgb_channel(int(fill[5:7], 16))
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.072175 * blue
    z = (0.0193339 * red + 0.119192 * green + 0.9503041 * blue) / 1.08883

    def pivot(value: float) -> float:
        delta = 6 / 29
        if value > delta**3:
            return value ** (1 / 3)
        return value / (3 * delta**2) + 4 / 29

    x_value, y_value, z_value = pivot(x), pivot(y), pivot(z)
    return 116 * y_value - 16, 500 * (x_value - y_value), 200 * (y_value - z_value)


def color_delta_e(first: str, second: str) -> float:
    """Return deterministic CIE76 Delta-E for two opaque sRGB colors."""

    first_lab = _lab(first)
    second_lab = _lab(second)
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(first_lab, second_lab)))


def _topology_unchanged(
    regions: Mapping[str, RegionSnapshot], candidate: RepairCandidate, operation: SeamOperation
) -> bool:
    removed = set(candidate.removed_region_ids)
    if operation is not SeamOperation.UNION and removed:
        return False
    if removed - set(regions) or set(candidate.topology_by_region) != set(regions) - removed:
        return False
    unchanged = all(
        topology.is_valid() and topology == regions[region_id].topology
        for region_id, topology in candidate.topology_by_region.items()
    )
    if not unchanged:
        return False
    if removed:
        survivors = set(candidate.path_data_by_region)
        if len(survivors) != 1:
            return False
        survivor = regions[next(iter(survivors))]
        # Production union is limited to sibling, hole-free, single-contour
        # regions. This preserves every parent and hole relationship.
        return all(
            region.topology.closed
            and region.topology.subpaths == 1
            and region.topology.holes == 0
            and region.topology.parent_ids == survivor.topology.parent_ids
            and region.topology.winding == survivor.topology.winding
            for region_id, region in regions.items()
            if region_id in removed | survivors
        )
    return True


def _finite_in_range(value: object, minimum: float, maximum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def _candidate_scope(
    operation: SeamOperation,
    proposal: SeamRepairProposal,
    candidate: RepairCandidate,
    involved_regions: set[str],
) -> bool:
    changed_regions = set(candidate.affected_region_ids)
    output_regions = set(candidate.path_data_by_region)
    removed_regions = set(candidate.removed_region_ids)
    if output_regions & removed_regions:
        return False
    if operation is SeamOperation.SAFE_OVERLAP:
        return (
            changed_regions == output_regions == {proposal.bottom_region_id} and not removed_regions
        )
    if operation is SeamOperation.SNAP:
        return (
            bool(changed_regions)
            and changed_regions == output_regions
            and changed_regions <= involved_regions
            and not removed_regions
        )
    return (
        changed_regions == involved_regions and output_regions | removed_regions == involved_regions
    )


def _path_vertices(path_data: str) -> tuple[complex, ...]:
    parsed = parse_safe_path(path_data)
    vertices: list[complex] = []
    for segment in parsed:
        for point in (segment.start, segment.end):
            if not any(abs(point - existing) <= 1e-9 for existing in vertices):
                vertices.append(point)
    return tuple(vertices)


def _analysis_matches_topology(path_data: str, topology: TopologySignature) -> bool:
    analysis = analyze_path(path_data)
    return (
        analysis.closed == topology.closed
        and analysis.subpaths == topology.subpaths
        and (not analysis.closed or analysis.winding == topology.winding)
    )


def _build_snap_candidate(
    regions: Mapping[str, RegionSnapshot], proposal: SeamRepairProposal
) -> RepairCandidate:
    region_ids = tuple(dict.fromkeys(proposal.region_ids))
    vertices = {region_id: _path_vertices(regions[region_id].path_data) for region_id in region_ids}
    choices: list[tuple[float, str, complex, str, complex]] = []
    for first_index, first_id in enumerate(region_ids):
        for second_index in range(first_index, len(region_ids)):
            second_id = region_ids[second_index]
            for first_point in vertices[first_id]:
                for second_point in vertices[second_id]:
                    if first_id == second_id and abs(first_point - second_point) <= 1e-9:
                        continue
                    distance = abs(first_point - second_point)
                    if 1e-9 < distance <= proposal.snap_distance_px:
                        choices.append((distance, first_id, first_point, second_id, second_point))
    if not choices:
        raise ValueError("no local vertices satisfy the snap limit")
    _, first_id, first_point, second_id, second_point = min(
        choices, key=lambda item: (item[0], item[1], item[2].real, item[2].imag, item[3])
    )
    target = (first_point + second_point) / 2
    replacements: dict[str, list[tuple[complex, complex]]] = {first_id: [(first_point, target)]}
    replacements.setdefault(second_id, []).append((second_point, target))
    changed = tuple(sorted(replacements))
    repaired = {
        region_id: replace_endpoints(regions[region_id].path_data, tuple(items))
        for region_id, items in replacements.items()
    }
    if not all(
        _analysis_matches_topology(path_data, regions[region_id].topology)
        for region_id, path_data in repaired.items()
    ):
        raise ValueError("snap would change path topology")
    return RepairCandidate(
        path_data_by_region=repaired,
        topology_by_region={key: value.topology for key, value in regions.items()},
        affected_region_ids=changed,
    )


def _build_union_candidate(
    regions: Mapping[str, RegionSnapshot], proposal: SeamRepairProposal
) -> RepairCandidate:
    region_ids = tuple(dict.fromkeys(proposal.region_ids))
    survivor = region_ids[0]
    merged = pathops_boolean(
        BooleanOperation.UNION, tuple(regions[region_id].path_data for region_id in region_ids)
    )
    analysis = analyze_path(merged)
    if (
        not analysis.closed
        or analysis.subpaths != 1
        or analysis.winding != regions[survivor].topology.winding
    ):
        raise ValueError("union would change component or hole topology")
    removed = tuple(region_ids[1:])
    return RepairCandidate(
        path_data_by_region={survivor: merged},
        topology_by_region={
            key: value.topology for key, value in regions.items() if key not in removed
        },
        affected_region_ids=region_ids,
        removed_region_ids=removed,
    )


def _build_overlap_candidate(
    regions: Mapping[str, RegionSnapshot], proposal: SeamRepairProposal
) -> RepairCandidate:
    bottom_id = proposal.bottom_region_id
    patch = proposal.overlap_patch_path_data
    if bottom_id is None or not patch:
        raise ValueError("safe overlap requires an explicit local patch")
    patch_analysis = analyze_path(patch)
    if not patch_analysis.closed or patch_analysis.subpaths != 1:
        raise ValueError("overlap patch topology is unsafe")
    xmin, ymin, xmax, ymax = patch_analysis.bounds
    if min(xmax - xmin, ymax - ymin) > proposal.overlap_px + 1e-9:
        raise ValueError("overlap patch exceeds the local thickness limit")
    bottom_path = regions[bottom_id].path_data
    # Both calls are evidence: the patch must overlap the bottom and must also
    # extend beyond it. This prevents a no-op or a whole-shape dilation.
    pathops_boolean(BooleanOperation.INTERSECTION, (bottom_path, patch))
    pathops_boolean(BooleanOperation.DIFFERENCE, (patch, bottom_path))
    merged = pathops_boolean(BooleanOperation.UNION, (bottom_path, patch))
    analysis = analyze_path(merged)
    if (
        not analysis.closed
        or analysis.subpaths != regions[bottom_id].topology.subpaths
        or analysis.winding != regions[bottom_id].topology.winding
    ):
        raise ValueError("overlap patch would change topology")
    return RepairCandidate(
        path_data_by_region={bottom_id: merged},
        topology_by_region={key: value.topology for key, value in regions.items()},
        affected_region_ids=(bottom_id,),
    )


def make_pathops_candidate_builder(
    regions: Mapping[str, RegionSnapshot],
) -> CandidateBuilder:
    """Create a fail-closed production builder backed by pinned skia-pathops.

    The returned builder never renders or approves its own output. Approval
    remains in ``apply_seam_repair`` and requires original-resolution metrics.
    """

    snapshots = dict(regions)

    def build(proposal: SeamRepairProposal) -> RepairCandidate:
        if not geometry_dependencies_available():
            raise GeometryDependencyUnavailable("Vector60 geometry extra is unavailable")
        if not snapshots or any(not region.path_data for region in snapshots.values()):
            raise ValueError("region path data is unavailable")
        if not all(
            _analysis_matches_topology(region.path_data, region.topology)
            for region in snapshots.values()
        ):
            raise ValueError("region path topology does not match its evidence")
        operation = SeamOperation(proposal.operation)
        if operation is SeamOperation.SNAP:
            return _build_snap_candidate(snapshots, proposal)
        if operation is SeamOperation.UNION:
            return _build_union_candidate(snapshots, proposal)
        return _build_overlap_candidate(snapshots, proposal)

    return build


def _result(
    *,
    operation: SeamOperation | None,
    delta_e: float | None,
    gates: Mapping[str, bool],
    reasons: list[str],
    candidate: RepairCandidate | None = None,
) -> SeamRepairResult:
    applied = candidate is not None and all(gates.values())
    return SeamRepairResult(
        applied=applied,
        candidate=candidate if applied else None,
        operation=operation,
        delta_e=delta_e,
        gates=dict(gates),
        rejection_reasons=tuple(reasons),
    )


def apply_seam_repair(
    *,
    regions: Mapping[str, RegionSnapshot],
    proposal: SeamRepairProposal,
    candidate_builder: CandidateBuilder | None,
    render_evaluator: RenderEvaluator | None,
    maximum_delta_e: float = DEFAULT_MAXIMUM_DELTA_E,
) -> SeamRepairResult:
    """Apply a local repair only when static, topology, and render gates pass.

    No dilation operation exists in this policy. Geometry is produced by an
    injectable audited implementation (for example skia-pathops), while this
    function prevents it from silently bypassing Vector60 safety rules.
    """

    gates = {
        "supported_operation": False,
        "local_scope": False,
        "operation_limits": False,
        "color": False,
        "candidate_scope": False,
        "safe_path_data": False,
        "topology": False,
        "final_render": False,
        "not_degraded": False,
    }
    reasons: list[str] = []
    delta_e: float | None = None
    try:
        operation = SeamOperation(proposal.operation)
    except (TypeError, ValueError):
        reasons.append("unsupported_operation")
        return _result(operation=None, delta_e=None, gates=gates, reasons=reasons)
    gates["supported_operation"] = True

    region_ids = tuple(dict.fromkeys(proposal.region_ids))
    known_regions = bool(region_ids) and all(region_id in regions for region_id in region_ids)
    gates["local_scope"] = proposal.local and known_regions
    if not gates["local_scope"]:
        reasons.append("local_scope_gate")

    limits_valid = _finite_in_range(maximum_delta_e, 0, math.inf)
    if operation is SeamOperation.SNAP:
        gates["operation_limits"] = (
            len(region_ids) in {1, 2}
            and _finite_in_range(
                proposal.snap_distance_px, math.nextafter(0.0, 1.0), MAXIMUM_VERTEX_SNAP_PX
            )
            and proposal.overlap_px == 0
            and proposal.bottom_region_id is None
            and proposal.overlap_patch_path_data is None
        )
        gates["color"] = True
    elif operation is SeamOperation.UNION:
        gates["operation_limits"] = (
            len(region_ids) >= 2
            and proposal.snap_distance_px == 0
            and proposal.overlap_px == 0
            and proposal.bottom_region_id is None
            and proposal.overlap_patch_path_data is None
            and limits_valid
        )
        if known_regions:
            fills = [regions[region_id].fill for region_id in region_ids]
            try:
                pair_deltas = [
                    color_delta_e(first, second) for first, second in combinations(fills, 2)
                ]
            except (TypeError, ValueError):
                pair_deltas = []
            if pair_deltas:
                delta_e = max(pair_deltas)
                gates["color"] = delta_e <= maximum_delta_e
    else:
        gates["operation_limits"] = (
            len(region_ids) == 2
            and _finite_in_range(
                proposal.overlap_px, math.nextafter(0.0, 1.0), MAXIMUM_SAFE_OVERLAP_PX
            )
            and proposal.snap_distance_px == 0
            and proposal.bottom_region_id in region_ids
        )
        gates["color"] = True
    if not gates["operation_limits"]:
        reasons.append("operation_limit_gate")
    if not gates["color"]:
        reasons.append("color_gate")

    preliminary = all(
        gates[name] for name in ("supported_operation", "local_scope", "operation_limits", "color")
    )
    if not preliminary or candidate_builder is None:
        if preliminary:
            reasons.append("candidate_builder_missing")
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)

    try:
        candidate = candidate_builder(proposal)
    except Exception:
        reasons.append("candidate_builder_error")
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)
    if not isinstance(candidate, RepairCandidate):
        reasons.append("candidate_builder_invalid")
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)

    expected_scope = set(region_ids)
    try:
        gates["candidate_scope"] = _candidate_scope(operation, proposal, candidate, expected_scope)
    except (AttributeError, TypeError):
        gates["candidate_scope"] = False
    if not gates["candidate_scope"]:
        reasons.append("candidate_scope_gate")
    try:
        gates["safe_path_data"] = bool(candidate.path_data_by_region) and all(
            _safe_path_data(path_data, candidate.topology_by_region[region_id])
            for region_id, path_data in candidate.path_data_by_region.items()
        )
    except (AttributeError, KeyError, TypeError):
        gates["safe_path_data"] = False
    if not gates["safe_path_data"]:
        reasons.append("unsafe_path_data")
    try:
        gates["topology"] = _topology_unchanged(regions, candidate, operation)
    except (AttributeError, KeyError, TypeError):
        gates["topology"] = False
    if not gates["topology"]:
        reasons.append("topology_gate")

    if not all(gates[name] for name in ("candidate_scope", "safe_path_data", "topology")):
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)
    if render_evaluator is None:
        reasons.append("final_render_evidence_missing")
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)

    try:
        render_evidence = render_evaluator(candidate)
    except Exception:
        reasons.append("final_render_error")
        return _result(operation=operation, delta_e=delta_e, gates=gates, reasons=reasons)
    gates["final_render"] = (
        isinstance(render_evidence, RepairRenderEvidence)
        and render_evidence.is_original_resolution
        and isinstance(render_evidence.before, RenderMetrics)
        and isinstance(render_evidence.after, RenderMetrics)
    )
    gates["not_degraded"] = gates["final_render"] and render_evidence.is_not_degraded
    if not gates["final_render"]:
        reasons.append("final_render_gate")
    elif not gates["not_degraded"]:
        reasons.append("metric_degraded")
    return _result(
        operation=operation,
        delta_e=delta_e,
        gates=gates,
        reasons=reasons,
        candidate=candidate,
    )
