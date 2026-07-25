from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from starbridge_mcp.vectorization.vector60 import geometry_processor
from starbridge_mcp.vectorization.vector60.geometry_backend import (
    geometry_dependencies_available,
)
from starbridge_mcp.vectorization.vector60.pipeline import run_vector60_pipeline


def write_solid_svg(path: Path, color: str, width: int = 16, height: int = 16) -> None:
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<path d="M 0 0 L {width} 0 L {width} {height} L 0 {height} Z" '
        f'fill="{color}" fill-rule="evenodd" stroke="none"/></svg>\n',
        encoding="utf-8",
    )


class Vector60PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "artisan_baseline.svg"
        write_solid_svg(self.baseline, "#0000ff")
        self.reference = Image.new("RGBA", (16, 16), (255, 0, 0, 255))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def generator(self, _source: Path, _candidate, output: Path) -> None:
        write_solid_svg(output, "#ff0000")

    def redundant_rectangle_generator(self, _source: Path, _candidate, output: Path) -> None:
        output.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
            'viewBox="0 0 16 16">'
            '<path d="M 0 0 L 8 0 L 16 0 L 16 16 L 0 16 Z" fill="#ff0000" '
            'fill-rule="evenodd" stroke="none"/></svg>\n',
            encoding="utf-8",
        )

    def split_rectangle_generator(self, _source: Path, _candidate, output: Path) -> None:
        output.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
            'viewBox="0 0 16 16">'
            '<path d="M 0 0 L 8 0 L 8 16 L 0 16 Z" fill="#ff0000" '
            'fill-rule="evenodd" stroke="none"/>'
            '<path d="M 8 0 L 16 0 L 16 16 L 8 16 Z" fill="#ff0000" '
            'fill-rule="evenodd" stroke="none"/></svg>\n',
            encoding="utf-8",
        )

    def optimizer(self, source: Path, output: Path) -> None:
        shutil.copyfile(source, output)

    def test_supported_scene_selects_only_final_rendered_safe_candidate(self) -> None:
        original = self.reference.tobytes()

        result = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="logo",
            candidate_limit=2,
            candidate_generator=self.generator,
            svg_optimizer=self.optimizer,
        )

        self.assertFalse(result.fallback_used)
        self.assertEqual(result.report.status, "selected")
        self.assertEqual(result.report.selected_candidate, "vtracer_balanced")
        self.assertTrue(result.report.final_render_scored)
        self.assertEqual(result.score.evidence.rendered_at_original_resolution, True)
        self.assertEqual(self.reference.tobytes(), original)
        report_text = (self.root / "vector60_report.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), report_text)
        self.assertEqual(json.loads(report_text)["candidate_count"], 2)

    @unittest.skipUnless(geometry_dependencies_available(), "Vector60 geometry extra unavailable")
    def test_default_geometry_processor_uses_render_gated_primitive_fit(self) -> None:
        result = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="logo",
            candidate_limit=2,
            candidate_generator=self.redundant_rectangle_generator,
            svg_optimizer=self.optimizer,
        )

        self.assertFalse(result.fallback_used)
        self.assertEqual(result.score.complexity.anchors, 4)
        self.assertNotIn("primitive_fit.no_safe_proposal", result.report.warning_codes)
        self.assertIn("seam_repair.no_safe_proposal", result.report.warning_codes)

    @unittest.skipUnless(geometry_dependencies_available(), "Vector60 geometry extra unavailable")
    def test_default_geometry_processor_unions_same_color_siblings_after_render(self) -> None:
        result = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="flat",
            candidate_limit=2,
            candidate_generator=self.split_rectangle_generator,
            svg_optimizer=self.optimizer,
        )

        self.assertFalse(result.fallback_used)
        self.assertEqual(result.score.complexity.subpaths, 1)
        self.assertLessEqual(result.score.complexity.anchors, 4)
        self.assertNotIn("seam_repair.no_safe_proposal", result.report.warning_codes)

    def test_missing_geometry_extra_retains_the_rendered_candidate(self) -> None:
        with mock.patch.object(
            geometry_processor, "geometry_dependencies_available", return_value=False
        ):
            result = run_vector60_pipeline(
                reference=self.reference,
                candidate_source=self.reference,
                baseline_svg=self.baseline,
                staging_dir=self.root,
                scene_preset="logo",
                candidate_limit=2,
                candidate_generator=self.redundant_rectangle_generator,
                svg_optimizer=self.optimizer,
            )

        self.assertFalse(result.fallback_used)
        self.assertEqual(result.score.complexity.anchors, 5)
        self.assertIn("primitive_fit.no_safe_proposal", result.report.warning_codes)
        self.assertIn("seam_repair.no_safe_proposal", result.report.warning_codes)

    def test_unsupported_photo_uses_only_artisan_baseline(self) -> None:
        generator = mock.Mock(side_effect=AssertionError("must not generate"))
        optimizer = mock.Mock(side_effect=AssertionError("must not optimize"))

        result = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="unsupported_photo",
            candidate_generator=generator,
            svg_optimizer=optimizer,
        )

        self.assertTrue(result.fallback_used)
        self.assertEqual(result.svg_path, self.baseline)
        self.assertEqual(result.report.status, "unsupported_photo_fallback")
        self.assertEqual(result.report.candidate_count, 1)
        self.assertEqual(result.report.fallback_reason, "unsupported_photo")
        generator.assert_not_called()
        optimizer.assert_not_called()

    def test_candidate_or_svgo_failure_falls_back_without_error_text(self) -> None:
        def failed_generator(_source: Path, _candidate, _output: Path) -> None:
            raise RuntimeError("C:/private/customer-token-cookie.png")

        failed = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="flat",
            candidate_limit=2,
            candidate_generator=failed_generator,
            svg_optimizer=self.optimizer,
        )
        self.assertTrue(failed.fallback_used)
        self.assertEqual(failed.svg_path, self.baseline)
        failed_text = (self.root / "vector60_report.json").read_text(encoding="utf-8")
        self.assertNotIn("private", failed_text)
        self.assertNotIn("token", failed_text)

        def unsafe_optimizer(_source: Path, output: Path) -> None:
            output.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
                "<script>alert(1)</script></svg>",
                encoding="utf-8",
            )

        unsafe = run_vector60_pipeline(
            reference=self.reference,
            candidate_source=self.reference,
            baseline_svg=self.baseline,
            staging_dir=self.root,
            scene_preset="flat",
            candidate_limit=2,
            candidate_generator=self.generator,
            svg_optimizer=unsafe_optimizer,
        )
        self.assertTrue(unsafe.fallback_used)
        self.assertEqual(unsafe.svg_path, self.baseline)
        self.assertEqual(unsafe.report.fallback_reason, "pipeline_stage_failed")


if __name__ == "__main__":
    unittest.main()
