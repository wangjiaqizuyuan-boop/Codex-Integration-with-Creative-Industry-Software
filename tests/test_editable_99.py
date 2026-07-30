from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace

from starbridge_mcp.backend import VECTOR_MODES, KORYAOBackend
from starbridge_mcp.vectorization import (
    RunConfig,
    VectorizationError,
    adaptive_optimize,
    cli,
    engine,
)
from starbridge_mcp.vectorization.app_model import MODE_CARDS, parameters_for_mode
from starbridge_mcp.vectorization.presets import PRESETS, normalize_mode

PUBLIC_VECTOR_MODES = {"exact", "smart", "lightweight", "artisan"}


class PublicVectorModeBoundaryTests(unittest.TestCase):
    def test_python_public_surfaces_expose_exactly_four_modes(self) -> None:
        self.assertEqual(set(PRESETS), PUBLIC_VECTOR_MODES)
        self.assertEqual(VECTOR_MODES, PUBLIC_VECTOR_MODES)
        self.assertEqual({card.key for card in MODE_CARDS}, PUBLIC_VECTOR_MODES)

    def test_editable_99_is_not_a_public_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "smart, lightweight, exact, or artisan"):
            normalize_mode("editable-99")
        with self.assertRaises(ValueError):
            parameters_for_mode("editable-99")
        with self.assertRaises(VectorizationError) as raised:
            engine._configured(RunConfig("placeholder.png", mode="editable-99"))
        self.assertEqual(raised.exception.code, "invalid_parameters")

    def test_cli_help_lists_only_the_four_public_modes(self) -> None:
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        for mode in PUBLIC_VECTOR_MODES:
            self.assertIn(mode, help_text)
        self.assertIn(
            "smart (default), lightweight, exact, artisan; balanced is accepted",
            " ".join(help_text.split()),
        )
        self.assertNotIn(
            "smart (default), lightweight, exact, editable-99, artisan",
            " ".join(help_text.split()),
        )
        self.assertNotIn("editable-99", help_text)

    def test_cli_rejects_editable_99_before_reading_the_input(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli.main(["--input", "placeholder.png", "--mode", "editable-99"])

        response = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(response["error"]["code"], "invalid_parameters")

        with self.assertRaises(VectorizationError) as quality_preset:
            cli.parse_args(
                [
                    "--input",
                    "placeholder.png",
                    "--quality-preset",
                    "editable-99",
                ]
            )
        self.assertEqual(quality_preset.exception.code, "invalid_arguments")

    def test_backend_rejects_editable_99_and_workflow_lists_four_modes(self) -> None:
        with tempfile.TemporaryDirectory() as app_data_dir:
            backend = KORYAOBackend(app_data_dir=app_data_dir)
            rejected = backend.route(
                "POST",
                "/api/vectorization/jobs",
                json.dumps(
                    {
                        "selection_id": "selection-does-not-need-to-exist",
                        "mode": "editable-99",
                        "parameters": {},
                        "confirm_run": True,
                        "confirm_write": True,
                        "confirm_export": True,
                    }
                ).encode("utf-8"),
            )

            self.assertEqual(rejected.status, 400)
            self.assertEqual(rejected.body["error"]["code"], "invalid_mode")
            workflows = backend.route("GET", "/api/workflows")
            vector_workflow = next(
                item
                for item in workflows.body["data"]["workflows"]
                if item["workflowId"] == "vector-delivery-v1"
            )
            self.assertEqual(set(vector_workflow["drawingModes"]), PUBLIC_VECTOR_MODES)


class Editable99InternalQualityPresetTests(unittest.TestCase):
    def test_editable_99_quality_preset_keeps_acceptance_thresholds(self) -> None:
        self.assertIn("editable-99", adaptive_optimize.QUALITY_PRESETS)
        thresholds = adaptive_optimize.QUALITY_PRESETS["editable-99"]

        self.assertEqual(thresholds.minimum_ssim, 0.990)
        self.assertEqual(thresholds.maximum_difference_percent, 1.0)
        self.assertEqual(thresholds.maximum_normalized_mae, 0.010)
        self.assertEqual(thresholds.minimum_edge_dice, 0.980)
        self.assertEqual(thresholds.maximum_alpha_mae, 0.005)

    def test_editable_99_quality_preset_accepts_one_percent_target(self) -> None:
        options, thresholds = adaptive_optimize.validated_options(
            adaptive_optimize.AdaptiveOptions(
                quality_preset="editable-99",
                target_difference=1.0,
            )
        )

        self.assertEqual(options.quality_preset, "editable-99")
        self.assertEqual(thresholds.maximum_difference_percent, 1.0)

    def test_candidate_color_schedule_is_high_to_low_and_complete(self) -> None:
        self.assertEqual(
            adaptive_optimize.EDITABLE_99_COLOR_CANDIDATES,
            (256, 192, 160, 128, 96, 80, 64, 48, 32),
        )


class Editable99QualityGateTests(unittest.TestCase):
    def test_alpha_mae_is_a_required_quality_gate(self) -> None:
        thresholds = adaptive_optimize.QUALITY_PRESETS["editable-99"]
        metrics = {
            "ssim": 0.999,
            "difference_percent": 0.1,
            "normalized_mae": 0.001,
            "edge_dice": 0.999,
            "alpha_mae": 0.006,
        }
        evidence = {"embedded_raster_count": 0, "external_reference_count": 0}

        gates = adaptive_optimize.quality_gates(metrics, evidence, thresholds)

        self.assertFalse(gates["alpha_mae"])
        self.assertFalse(all(gates.values()))

    def test_all_five_quality_metrics_must_pass(self) -> None:
        thresholds = adaptive_optimize.QUALITY_PRESETS["editable-99"]
        metrics = {
            "ssim": 0.990,
            "difference_percent": 1.0,
            "normalized_mae": 0.010,
            "edge_dice": 0.980,
            "alpha_mae": 0.005,
        }
        evidence = {"embedded_raster_count": 0, "external_reference_count": 0}

        gates = adaptive_optimize.quality_gates(metrics, evidence, thresholds)

        self.assertTrue(all(gates.values()))


class Editable99SelectionTests(unittest.TestCase):
    @staticmethod
    def candidate(
        identifier: str,
        *,
        subpaths: int,
        points: int,
        colors: int,
        size: int,
        elapsed: float = 1.0,
    ) -> dict:
        return {
            "candidate_id": identifier,
            "status": "pass",
            "final_render_metrics": {
                "ssim": 0.995,
                "difference_percent": 0.5,
                "normalized_mae": 0.004,
                "edge_dice": 0.99,
                "alpha_mae": 0.001,
            },
            "vector": {
                "subpaths": subpaths,
                "anchors": points,
                "colors": colors,
                "bytes": size,
            },
            "elapsed_seconds": elapsed,
        }

    def test_selection_minimizes_subpaths_then_points_colors_and_size(self) -> None:
        candidates = [
            self.candidate("few-points", subpaths=20, points=40, colors=32, size=900),
            self.candidate("few-subpaths", subpaths=19, points=200, colors=256, size=2_000),
            self.candidate("failed", subpaths=1, points=4, colors=2, size=100),
        ]
        candidates[-1]["status"] = "preview-only"

        selected = adaptive_optimize.select_editable_99_candidate(candidates)

        self.assertEqual(selected["candidate_id"], "few-subpaths")

    def test_ties_are_broken_by_points_then_colors_then_svg_size(self) -> None:
        candidates = [
            self.candidate("large", subpaths=10, points=30, colors=64, size=800),
            self.candidate("few-colors", subpaths=10, points=30, colors=48, size=900),
            self.candidate("few-points", subpaths=10, points=29, colors=256, size=2_000),
        ]

        selected = adaptive_optimize.select_editable_99_candidate(candidates)

        self.assertEqual(selected["candidate_id"], "few-points")


class IllustratorSafetyTests(unittest.TestCase):
    def test_complexity_policy_allows_only_the_safe_range_to_auto_open(self) -> None:
        preset = replace(
            PRESETS["artisan"],
            preferred_subpaths=30_000,
            warning_subpaths=60_000,
            preferred_points=120_000,
            warning_points=240_000,
            archive_subpaths=300_000,
        )

        safe = adaptive_optimize.assess_illustrator_complexity(30_000, 120_000, preset)
        warning = adaptive_optimize.assess_illustrator_complexity(30_001, 120_001, preset)
        blocked = adaptive_optimize.assess_illustrator_complexity(60_001, 240_001, preset)
        archive = adaptive_optimize.assess_illustrator_complexity(300_001, 1_200_004, preset)

        self.assertEqual(safe["risk_level"], "safe")
        self.assertTrue(safe["auto_open_allowed"])
        self.assertEqual(warning["risk_level"], "warning")
        self.assertFalse(warning["auto_open_allowed"])
        self.assertEqual(blocked["risk_level"], "blocked")
        self.assertFalse(blocked["auto_open_allowed"])
        self.assertEqual(archive["risk_level"], "archive")
        self.assertFalse(archive["auto_open_allowed"])

    def test_result_status_distinguishes_quality_failure_warning_and_conflict(self) -> None:
        status = adaptive_optimize.derive_editable_99_status

        self.assertEqual(status(False, "safe"), "quality_not_met")
        self.assertEqual(status(True, "safe"), "passed_editable_99")
        self.assertEqual(status(True, "warning"), "passed_quality_high_complexity")
        self.assertEqual(status(True, "blocked"), "quality_and_editability_conflict")
        self.assertEqual(status(True, "archive"), "quality_and_editability_conflict")


if __name__ == "__main__":
    unittest.main()
