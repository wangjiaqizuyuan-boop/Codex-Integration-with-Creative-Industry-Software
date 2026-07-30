from __future__ import annotations

import unittest
from unittest import mock

from starbridge_mcp.vectorization.vector60 import geometry_backend
from starbridge_mcp.vectorization.vector60 import primitive_fit as primitive_backend
from starbridge_mcp.vectorization.vector60.geometry_backend import (
    BooleanOperation,
    analyze_path,
    geometry_dependencies_available,
    pathops_boolean,
)
from starbridge_mcp.vectorization.vector60.primitive_fit import (
    FinalRenderEvidence,
    PrimitiveKind,
    TopologySignature,
    fit_best_primitive,
    generate_primitive_proposals,
)
from starbridge_mcp.vectorization.vector60.seam_repair import (
    RegionSnapshot,
    RenderMetrics,
    RepairRenderEvidence,
    SeamOperation,
    SeamRepairProposal,
    apply_seam_repair,
    make_pathops_candidate_builder,
)

CLOSED = TopologySignature(True, 1, 0, (None,), (1,))
OPEN = TopologySignature(False, 1, 0, (None,), (1,))
RECTANGLE = "M 0 0 L 10 0 L 10 10 L 0 10 Z"


@unittest.skipUnless(geometry_dependencies_available(), "Vector60 geometry extra unavailable")
class PrimitiveBackendTests(unittest.TestCase):
    @staticmethod
    def _render(_: str) -> FinalRenderEvidence:
        return FinalRenderEvidence(10, 10, 10, 10, True)

    def test_svgpathtools_fits_rectangle_and_line_with_measured_geometry(self) -> None:
        rectangle = generate_primitive_proposals(
            original_path_data=RECTANGLE,
            original_topology=CLOSED,
        )
        line = generate_primitive_proposals(
            original_path_data="M 0 0 L 10 0",
            original_topology=OPEN,
        )
        rectangle_fit = next(item for item in rectangle if item.kind is PrimitiveKind.RECTANGLE)
        self.assertEqual(rectangle_fit.contour_error_px, 0)
        self.assertEqual(rectangle_fit.area_error_ratio, 0)
        self.assertEqual(line[0].kind, PrimitiveKind.LINE)
        self.assertEqual(line[0].contour_error_px, 0)

    def test_all_six_rule_graphics_have_deterministic_geometry_proposals(self) -> None:
        shapes = (
            primitive_backend._rounded_rectangle_path(0, 0, 20, 10, 2),
            primitive_backend._ellipse_path(0, 0, 20, 10),
            primitive_backend._ellipse_path(0, 0, 10, 10),
            "M 5 0 L 10 5 L 5 10 L 0 5 Z",
        )
        kinds = {PrimitiveKind.LINE, PrimitiveKind.RECTANGLE}
        for path_data in shapes:
            kinds.update(
                proposal.kind
                for proposal in generate_primitive_proposals(
                    original_path_data=path_data,
                    original_topology=CLOSED,
                )
                if proposal.contour_error_px == 0 and proposal.area_error_ratio == 0
            )
        self.assertEqual(kinds, set(PrimitiveKind))

    def test_best_fit_still_requires_original_resolution_render(self) -> None:
        accepted = fit_best_primitive(
            original_path_data=RECTANGLE,
            original_topology=CLOSED,
            render_gate=self._render,
        )
        rejected = fit_best_primitive(
            original_path_data=RECTANGLE,
            original_topology=CLOSED,
            render_gate=lambda _: FinalRenderEvidence(10, 10, 20, 20, True),
        )
        self.assertTrue(accepted.replaced)
        self.assertEqual(accepted.primitive, PrimitiveKind.RECTANGLE)
        self.assertFalse(rejected.replaced)
        self.assertEqual(rejected.path_data, RECTANGLE)

    def test_self_intersection_and_unsafe_commands_produce_no_proposal(self) -> None:
        self.assertEqual(
            generate_primitive_proposals(
                original_path_data="M 0 0 L 10 10 L 0 10 L 10 0 Z",
                original_topology=CLOSED,
            ),
            (),
        )
        self.assertEqual(
            generate_primitive_proposals(
                original_path_data="M 0 0 A 5 5 0 0 0 10 10 Z",
                original_topology=CLOSED,
            ),
            (),
        )


@unittest.skipUnless(geometry_dependencies_available(), "Vector60 geometry extra unavailable")
class PathopsBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.left = "M 0 0 L 6 0 L 6 6 L 0 6 Z"
        self.right = "M 4 0 L 10 0 L 10 6 L 4 6 Z"

    def test_all_pinned_boolean_operations_return_safe_analyzable_paths(self) -> None:
        expected_area = {
            BooleanOperation.UNION: 60,
            BooleanOperation.INTERSECTION: 12,
            BooleanOperation.DIFFERENCE: 24,
            BooleanOperation.XOR: 48,
        }
        for operation, area in expected_area.items():
            with self.subTest(operation=operation):
                output = pathops_boolean(operation, (self.left, self.right))
                self.assertNotIn("A", output)
                self.assertNotIn("Q", output)
                self.assertAlmostEqual(analyze_path(output).area, area)

    @staticmethod
    def _render(_):
        metrics = RenderMetrics(0.92, 0.05, 0.93)
        return RepairRenderEvidence(10, 10, 10, 10, metrics, metrics)

    def test_same_color_union_coalesces_siblings_without_hole_or_parent_change(self) -> None:
        regions = {
            "left": RegionSnapshot("left", "#336699", CLOSED, "M 0 0 L 5 0 L 5 10 L 0 10 Z"),
            "right": RegionSnapshot("right", "#336699", CLOSED, "M 5 0 L 10 0 L 10 10 L 5 10 Z"),
        }
        proposal = SeamRepairProposal(SeamOperation.UNION, ("left", "right"))
        result = apply_seam_repair(
            regions=regions,
            proposal=proposal,
            candidate_builder=make_pathops_candidate_builder(regions),
            render_evaluator=self._render,
        )
        self.assertTrue(result.applied)
        assert result.candidate is not None
        self.assertEqual(result.candidate.removed_region_ids, ("right",))
        self.assertEqual(analyze_path(result.candidate.path_data_by_region["left"]).area, 100)

    def test_snap_moves_only_a_local_vertex_and_obeys_point_eight_limit(self) -> None:
        regions = {
            "left": RegionSnapshot("left", "#336699", CLOSED, "M 0 0 L 5 0 L 5 5 L 0 5 Z"),
            "right": RegionSnapshot("right", "#663399", CLOSED, "M 5.6 0 L 10 0 L 10 5 L 5.6 5 Z"),
        }
        proposal = SeamRepairProposal(SeamOperation.SNAP, ("left", "right"), snap_distance_px=0.8)
        candidate = make_pathops_candidate_builder(regions)(proposal)
        self.assertIn("5.3 0", candidate.path_data_by_region["left"])
        self.assertIn("5.3 0", candidate.path_data_by_region["right"])
        too_small = SeamRepairProposal(SeamOperation.SNAP, ("left", "right"), snap_distance_px=0.5)
        with self.assertRaises(ValueError):
            make_pathops_candidate_builder(regions)(too_small)

    def test_safe_overlap_requires_explicit_thin_local_patch_on_bottom_only(self) -> None:
        regions = {
            "bottom": RegionSnapshot("bottom", "#336699", CLOSED, "M 0 0 L 5 0 L 5 5 L 0 5 Z"),
            "top": RegionSnapshot("top", "#663399", CLOSED, "M 5.2 0 L 10 0 L 10 5 L 5.2 5 Z"),
        }
        proposal = SeamRepairProposal(
            SeamOperation.SAFE_OVERLAP,
            ("bottom", "top"),
            overlap_px=0.4,
            bottom_region_id="bottom",
            overlap_patch_path_data="M 4.8 1 L 5.2 1 L 5.2 4 L 4.8 4 Z",
        )
        result = apply_seam_repair(
            regions=regions,
            proposal=proposal,
            candidate_builder=make_pathops_candidate_builder(regions),
            render_evaluator=self._render,
        )
        self.assertTrue(result.applied)
        assert result.candidate is not None
        self.assertEqual(result.candidate.affected_region_ids, ("bottom",))

        wide_patch = SeamRepairProposal(
            SeamOperation.SAFE_OVERLAP,
            ("bottom", "top"),
            overlap_px=0.4,
            bottom_region_id="bottom",
            overlap_patch_path_data="M 4 1 L 5.2 1 L 5.2 4 L 4 4 Z",
        )
        blocked = apply_seam_repair(
            regions=regions,
            proposal=wide_patch,
            candidate_builder=make_pathops_candidate_builder(regions),
            render_evaluator=self._render,
        )
        self.assertFalse(blocked.applied)
        self.assertIn("candidate_builder_error", blocked.rejection_reasons)


class OptionalDependencyTests(unittest.TestCase):
    def test_missing_geometry_extra_retains_original_path(self) -> None:
        with mock.patch.object(geometry_backend, "_pathops", None):
            result = fit_best_primitive(
                original_path_data=RECTANGLE,
                original_topology=CLOSED,
                render_gate=lambda _: FinalRenderEvidence(10, 10, 10, 10, True),
            )
        self.assertFalse(result.replaced)
        self.assertEqual(result.path_data, RECTANGLE)
        self.assertIn("geometry_backend_unavailable", result.rejection_reasons[0])

    def test_missing_pathops_rolls_back_seam_candidate(self) -> None:
        regions = {
            "left": RegionSnapshot("left", "#336699", CLOSED, RECTANGLE),
            "right": RegionSnapshot("right", "#336699", CLOSED, "M 10 0 L 20 0 L 20 10 L 10 10 Z"),
        }
        proposal = SeamRepairProposal(SeamOperation.UNION, ("left", "right"))
        with mock.patch.object(geometry_backend, "_pathops", None):
            result = apply_seam_repair(
                regions=regions,
                proposal=proposal,
                candidate_builder=make_pathops_candidate_builder(regions),
                render_evaluator=lambda _: RepairRenderEvidence(
                    20,
                    10,
                    20,
                    10,
                    RenderMetrics(1, 0, 1),
                    RenderMetrics(1, 0, 1),
                ),
            )
        self.assertFalse(result.applied)
        self.assertIsNone(result.candidate)
        self.assertIn("candidate_builder_error", result.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
