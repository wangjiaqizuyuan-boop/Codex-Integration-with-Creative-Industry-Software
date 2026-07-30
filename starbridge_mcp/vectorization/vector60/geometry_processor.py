from __future__ import annotations

import math
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path

from PIL import Image

from .geometry_backend import analyze_path, geometry_dependencies_available, parse_safe_path
from .primitive_fit import FinalRenderEvidence, TopologySignature, fit_best_primitive
from .scorer import CandidateScore, QualityGates, score_final_svg_candidate
from .seam_repair import (
    DEFAULT_MAXIMUM_DELTA_E,
    MAXIMUM_VERTEX_SNAP_PX,
    RegionSnapshot,
    RenderMetrics,
    RepairCandidate,
    RepairRenderEvidence,
    SeamOperation,
    SeamRepairProposal,
    apply_seam_repair,
    color_delta_e,
    make_pathops_candidate_builder,
)

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
MAXIMUM_GEOMETRY_RENDER_ATTEMPTS = 8
MAXIMUM_GEOMETRY_REGIONS = 24


@dataclass(frozen=True)
class _Region:
    region_id: str
    fill: str
    path_data: str
    topology: TopologySignature


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _topology(path_data: str) -> TopologySignature:
    analysis = analyze_path(path_data)
    if analysis.subpaths != 1 or (analysis.closed and analysis.winding not in {(-1,), (1,)}):
        raise ValueError("path topology is not an eligible sibling region")
    winding = analysis.winding if analysis.closed else (1,)
    return TopologySignature(
        closed=analysis.closed,
        subpaths=1,
        holes=0,
        parent_ids=(None,),
        winding=winding,
    )


def _read_regions(source_svg: Path) -> tuple[int, int, list[_Region]]:
    root = ET.parse(source_svg).getroot()
    if _tag_name(root.tag) != "svg":
        raise ValueError("invalid SVG root")
    width = int(root.get("width") or "0")
    height = int(root.get("height") or "0")
    if width <= 0 or height <= 0:
        raise ValueError("invalid SVG dimensions")
    regions: list[_Region] = []
    for index, child in enumerate(root):
        if _tag_name(child.tag) != "path" or list(child):
            raise ValueError("geometry processor accepts only path children")
        if set(child.attrib) - {"d", "fill", "fill-rule", "stroke"}:
            raise ValueError("path attributes exceed the safe dialect")
        path_data = child.get("d") or ""
        fill = (child.get("fill") or "").lower()
        if child.get("fill-rule") != "evenodd" or child.get("stroke") != "none":
            raise ValueError("path paint is outside the normalized dialect")
        regions.append(_Region(f"r{index:03d}", fill, path_data, _topology(path_data)))
    if not regions or len(regions) > MAXIMUM_GEOMETRY_REGIONS:
        raise ValueError("geometry region count is outside the audited bound")
    return width, height, regions


def _write_regions(path: Path, width: int, height: int, regions: list[_Region]) -> None:
    lines = [
        f'<svg xmlns="{SVG_NAMESPACE}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    ]
    for region in regions:
        lines.append(
            f'<path d="{region.path_data}" fill="{region.fill}" fill-rule="evenodd" stroke="none"/>'
        )
    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _not_degraded(before: CandidateScore, after: CandidateScore) -> bool:
    epsilon = 1e-12
    return (
        after.visual.ssim + epsilon >= before.visual.ssim
        and after.visual.normalized_mae <= before.visual.normalized_mae + epsilon
        and after.visual.edge_dice + epsilon >= before.visual.edge_dice
        and after.complexity.anchors <= before.complexity.anchors
        and after.complexity.subpaths <= before.complexity.subpaths
    )


def _render_metrics(score: CandidateScore) -> RenderMetrics:
    return RenderMetrics(
        ssim=score.visual.ssim,
        normalized_mae=score.visual.normalized_mae,
        edge_dice=score.visual.edge_dice,
    )


def _bounds_touch(first: str, second: str) -> bool:
    first_bounds = analyze_path(first).bounds
    second_bounds = analyze_path(second).bounds
    return (
        max(first_bounds[0], second_bounds[0]) <= min(first_bounds[2], second_bounds[2]) + 1e-9
        and max(first_bounds[1], second_bounds[1]) <= min(first_bounds[3], second_bounds[3]) + 1e-9
    )


def _minimum_vertex_distance(first: str, second: str) -> float:
    def vertices(path_data: str) -> tuple[complex, ...]:
        parsed = parse_safe_path(path_data)
        return tuple({point for segment in parsed for point in (segment.start, segment.end)})

    distances = (
        abs(first_point - second_point)
        for first_point in vertices(first)
        for second_point in vertices(second)
        if abs(first_point - second_point) > 1e-9
    )
    return min(distances, default=math.inf)


def process_svg_geometry(
    source_svg: Path,
    output_svg: Path,
    *,
    reference: Image.Image,
    staging_dir: Path,
    expected_width: int,
    expected_height: int,
    detail_protection: float,
    quality_gates: QualityGates,
    maximum_render_attempts: int = MAXIMUM_GEOMETRY_RENDER_ATTEMPTS,
) -> tuple[str, ...]:
    """Apply only render-proven primitive fits and local seam repairs.

    Every proposal is rendered at the source resolution. Missing dependencies,
    malformed geometry, exhausted budgets, or any metric regression retain the
    input candidate unchanged.
    """

    if not geometry_dependencies_available() or maximum_render_attempts < 1:
        shutil.copyfile(source_svg, output_svg)
        return ("primitive_fit.no_safe_proposal", "seam_repair.no_safe_proposal")

    temporary_paths: list[Path] = []
    attempt = 0

    def score(regions: list[_Region], label: str) -> CandidateScore:
        nonlocal attempt
        attempt += 1
        if attempt > maximum_render_attempts:
            raise RuntimeError("geometry render budget exhausted")
        svg_path = staging_dir / f".vector60-geometry-{label}-{attempt:02d}.svg"
        render_path = staging_dir / f".vector60-geometry-{label}-{attempt:02d}.png"
        temporary_paths.extend((svg_path, render_path))
        _write_regions(svg_path, expected_width, expected_height, regions)
        return score_final_svg_candidate(
            candidate_id=f"geometry_{label}_{attempt:02d}",
            reference=reference,
            svg_path=svg_path,
            render_path=render_path,
            expected_svg_width=expected_width,
            expected_svg_height=expected_height,
            detail_protection=detail_protection,
        )

    try:
        width, height, regions = _read_regions(source_svg)
        if (width, height) != (expected_width, expected_height):
            raise ValueError("geometry dimensions differ from the source")
        before = score(regions, "before")
        primitive_applied = False
        for index in range(len(regions)):
            if attempt >= maximum_render_attempts:
                break
            original = regions[index]
            accepted: dict[str, CandidateScore] = {}

            def render_gate(
                path_data: str,
                *,
                _index: int = index,
                _original: _Region = original,
                _regions: tuple[_Region, ...] = tuple(regions),
                _before: CandidateScore = before,
                _accepted: dict[str, CandidateScore] = accepted,
            ) -> FinalRenderEvidence:
                candidate_regions = list(_regions)
                candidate_regions[_index] = replace(_original, path_data=path_data)
                after = score(candidate_regions, "primitive")
                passed = (
                    after.passes(quality_gates)
                    and _not_degraded(_before, after)
                    and after.complexity.anchors < _before.complexity.anchors
                )
                if passed:
                    _accepted[path_data] = after
                return FinalRenderEvidence(
                    expected_width,
                    expected_height,
                    after.evidence.render_width,
                    after.evidence.render_height,
                    passed,
                )

            result = fit_best_primitive(
                original_path_data=original.path_data,
                original_topology=original.topology,
                render_gate=render_gate,
            )
            if result.replaced:
                regions[index] = replace(original, path_data=result.path_data)
                before = accepted[result.path_data]
                primitive_applied = True

        seam_applied = False
        snapshots = {
            region.region_id: RegionSnapshot(
                region.region_id, region.fill, region.topology, region.path_data
            )
            for region in regions
        }
        proposals: list[SeamRepairProposal] = []
        for first, second in combinations(regions, 2):
            if attempt >= maximum_render_attempts:
                break
            try:
                delta_e = color_delta_e(first.fill, second.fill)
                if delta_e <= DEFAULT_MAXIMUM_DELTA_E and _bounds_touch(
                    first.path_data, second.path_data
                ):
                    proposals.append(
                        SeamRepairProposal(SeamOperation.UNION, (first.region_id, second.region_id))
                    )
                    continue
                distance = _minimum_vertex_distance(first.path_data, second.path_data)
                if 1e-9 < distance <= MAXIMUM_VERTEX_SNAP_PX:
                    proposals.append(
                        SeamRepairProposal(
                            SeamOperation.SNAP,
                            (first.region_id, second.region_id),
                            snap_distance_px=distance,
                        )
                    )
            except (ArithmeticError, TypeError, ValueError):
                continue

        for proposal in proposals:
            if attempt >= maximum_render_attempts:
                break
            evaluated: dict[int, tuple[list[_Region], CandidateScore]] = {}

            def render_repair(
                candidate: RepairCandidate,
                *,
                _regions: tuple[_Region, ...] = tuple(regions),
                _before: CandidateScore = before,
                _evaluated: dict[int, tuple[list[_Region], CandidateScore]] = evaluated,
            ) -> RepairRenderEvidence:
                removed = set(candidate.removed_region_ids)
                candidate_regions = [
                    replace(
                        region,
                        path_data=candidate.path_data_by_region.get(
                            region.region_id, region.path_data
                        ),
                    )
                    for region in _regions
                    if region.region_id not in removed
                ]
                after = score(candidate_regions, "seam")
                _evaluated[id(candidate)] = (candidate_regions, after)
                return RepairRenderEvidence(
                    expected_width,
                    expected_height,
                    after.evidence.render_width,
                    after.evidence.render_height,
                    _render_metrics(_before),
                    _render_metrics(after),
                )

            result = apply_seam_repair(
                regions=snapshots,
                proposal=proposal,
                candidate_builder=make_pathops_candidate_builder(snapshots),
                render_evaluator=render_repair,
            )
            if result.applied and result.candidate is not None:
                regions, before = evaluated[id(result.candidate)]
                seam_applied = True
                break

        if primitive_applied or seam_applied:
            _write_regions(output_svg, expected_width, expected_height, regions)
        else:
            shutil.copyfile(source_svg, output_svg)
        warnings: list[str] = []
        if not primitive_applied:
            warnings.append("primitive_fit.no_safe_proposal")
        if not seam_applied:
            warnings.append("seam_repair.no_safe_proposal")
        return tuple(warnings)
    except Exception:
        shutil.copyfile(source_svg, output_svg)
        return ("primitive_fit.no_safe_proposal", "seam_repair.no_safe_proposal")
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


__all__ = ["MAXIMUM_GEOMETRY_RENDER_ATTEMPTS", "process_svg_geometry"]
