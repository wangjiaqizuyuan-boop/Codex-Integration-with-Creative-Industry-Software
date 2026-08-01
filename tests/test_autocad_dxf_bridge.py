from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from importlib.util import find_spec
from pathlib import Path

import starbridge_mcp.bridges.autocad_dxf as autocad_dxf
from starbridge_mcp.bridges.autocad_dxf import (
    create_dxf_plan,
    status,
    summarize_plan,
    validate_cad_plan,
    write_dxf,
)

BANNED_OUTPUT_FRAGMENTS = ("C:\\Users\\", "/Users/", "/home/", "Desktop", "Documents", "AppData")


def minimal_plan() -> dict:
    return {
        "units": "mm",
        "layers": [{"name": "OUTLINE", "color": 7}, {"name": "TEXT", "color": 2}],
        "entities": [
            {"type": "rectangle", "layer": "OUTLINE", "x": 0, "y": 0, "width": 1000, "height": 500},
            {"type": "line", "layer": "OUTLINE", "start": [0, 0], "end": [1000, 500]},
            {"type": "circle", "layer": "OUTLINE", "center": [500, 250], "radius": 80},
            {"type": "polyline", "layer": "OUTLINE", "points": [[0, 0], [100, 0], [100, 100]]},
            {
                "type": "text",
                "layer": "TEXT",
                "position": [20, 620],
                "height": 120,
                "value": "demo",
            },
        ],
        "output": "demo.dxf",
    }


class AutoCadDxfBridgeTests(unittest.TestCase):
    def assert_schema(self, result: dict, action: str) -> None:
        self.assertEqual(
            {"ok", "bridge", "action", "message", "details", "warnings", "next_steps"}, set(result)
        )
        self.assertEqual("autocad_dxf", result["bridge"])
        self.assertEqual(action, result["action"])
        text = json.dumps(result, ensure_ascii=False)
        for fragment in BANNED_OUTPUT_FRAGMENTS:
            self.assertNotIn(fragment, text)

    def test_status_does_not_require_autocad_or_ezdxf(self) -> None:
        result = status()
        self.assert_schema(result, "status")
        self.assertTrue(result["ok"])
        self.assertFalse(result["details"]["requires_autocad"])

    def test_validate_cad_plan_rejects_invalid_inputs(self) -> None:
        for bad_plan in (
            "not a dict",
            {},
            {"units": "mm"},
            {"units": "mm", "entities": [{"type": "unknown"}]},
        ):
            with self.subTest(plan=bad_plan):
                result = validate_cad_plan(bad_plan)
                self.assert_schema(result, "validate_cad_plan")
                self.assertFalse(result["ok"])

    def test_validate_cad_plan_rejects_unsafe_output_paths(self) -> None:
        unsafe_outputs = [
            "../escape.dxf",
            "C:\\Users\\private\\Desktop\\escape.dxf",
            "/tmp/escape.dxf",
            "not_a_dxf.txt",
        ]
        for output in unsafe_outputs:
            with self.subTest(output=output):
                plan = minimal_plan()
                plan["output"] = output
                result = validate_cad_plan(plan)
                self.assert_schema(result, "validate_cad_plan")
                self.assertFalse(result["ok"])

    def test_validate_cad_plan_rejects_out_of_range_geometry(self) -> None:
        plan = minimal_plan()
        plan["entities"] = [{"type": "line", "start": [0, 0], "end": [2_000_000, 0]}]
        result = validate_cad_plan(plan)
        self.assert_schema(result, "validate_cad_plan")
        self.assertFalse(result["ok"])

    def test_validate_cad_plan_accepts_legal_minimal_plan(self) -> None:
        result = validate_cad_plan(minimal_plan())
        self.assert_schema(result, "validate_cad_plan")
        self.assertTrue(result["ok"])
        self.assertEqual(5, result["details"]["entity_count"])

    def test_create_dxf_plan_accepts_prompt_and_spec(self) -> None:
        prompt_result = create_dxf_plan("生成一个矩形边框和标题文字")
        self.assert_schema(prompt_result, "create_dxf_plan")
        self.assertTrue(prompt_result["ok"])

        spec_result = create_dxf_plan(minimal_plan())
        self.assert_schema(spec_result, "create_dxf_plan")
        self.assertTrue(spec_result["ok"])

    def test_summarize_plan_counts_entities(self) -> None:
        result = summarize_plan(minimal_plan())
        self.assert_schema(result, "summarize_plan")
        self.assertTrue(result["ok"])
        self.assertEqual(5, result["details"]["entity_count"])
        self.assertEqual(1, result["details"]["entity_types"]["line"])
        self.assertEqual(1, result["details"]["entity_types"]["rectangle"])
        self.assertEqual(["0", "OUTLINE", "TEXT"], result["details"]["layers"])
        self.assertEqual(
            {"min_x": 0.0, "min_y": 0.0, "max_x": 1000.0, "max_y": 620.0}, result["details"]["bbox"]
        )

    def test_write_dxf_dry_run_does_not_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "dry_run.dxf"
            result = write_dxf(minimal_plan(), output, dry_run=True)
            self.assert_schema(result, "write_dxf")
            self.assertTrue(result["ok"])
            self.assertFalse(output.exists())

    def test_write_dxf_requires_confirm_write_for_real_write(self) -> None:
        output = autocad_dxf.OUTPUT_ROOT / "blocked_without_confirm.dxf"
        result = write_dxf(minimal_plan(), output, dry_run=False, confirm_write=False)
        self.assert_schema(result, "write_dxf")
        self.assertFalse(result["ok"])
        self.assertFalse(output.exists())

    def test_write_dxf_rejects_output_outside_examples_cad_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "blocked.dxf"
            result = write_dxf(minimal_plan(), output, dry_run=False, confirm_write=True)
            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertFalse(output.exists())

    def test_write_dxf_rejects_non_dxf_inside_sandbox(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "blocked.txt"
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertFalse(output.exists())

    def test_declared_sandbox_prefix_is_not_duplicated(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            try:
                resolved = bridge._resolve_output_path("examples/cad/output/prefixed.dxf")
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assertEqual((Path(tmp) / "prefixed.dxf").resolve(), resolved)

    def test_write_dxf_reports_unavailable_without_ezdxf(self) -> None:
        original = autocad_dxf._ezdxf_available
        autocad_dxf._ezdxf_available = lambda: False
        try:
            output = autocad_dxf.OUTPUT_ROOT / "missing_ezdxf.dxf"
            result = write_dxf(minimal_plan(), output, dry_run=False, confirm_write=True)
        finally:
            autocad_dxf._ezdxf_available = original
        self.assert_schema(result, "write_dxf")
        self.assertFalse(result["ok"])
        self.assertEqual("unavailable", result["details"]["status"])
        self.assertFalse(output.exists())

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_creates_audited_dxf_and_manifest(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "real_generation.dxf"
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertTrue(result["ok"])
            self.assertEqual("completed", result["details"]["state"])
            self.assertTrue(result["details"]["terminal"])
            self.assertTrue(result["details"]["result_ready"])
            self.assertTrue(output.is_file())

            manifest_path = output.with_suffix(".manifest.json")
            preview_path = output.with_suffix(".preview.svg")
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(preview_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("1.0", manifest["schema_version"])
            generation_id = result["details"]["generation_id"]
            self.assertRegex(generation_id, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(generation_id, manifest["generation_id"])
            self.assertEqual(
                {
                    "stale_staging_recovered": False,
                    "recovered_file_count": 0,
                    "minimum_age_seconds": bridge.STAGING_RECOVERY_MIN_AGE_SECONDS,
                },
                manifest["recovery"],
            )
            self.assertEqual(0, manifest["verification"]["audit_errors"])
            self.assertEqual(5, manifest["verification"]["entity_count"])
            self.assertTrue(manifest["verification"]["content_match"])
            self.assertRegex(
                manifest["verification"]["content_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(2, len(manifest["artifacts"]))
            self.assertTrue(manifest["preview_verification"]["verified"])
            self.assertTrue(manifest["preview_verification"]["entity_metadata_match"])
            self.assertRegex(
                manifest["preview_verification"]["entity_mapping_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertGreater(manifest["preview_verification"]["path_count"], 0)
            self.assertEqual(
                "#ffffff",
                manifest["preview_verification"]["background_color"],
            )
            self.assertEqual(
                "#000000",
                manifest["preview_verification"]["foreground_color"],
            )
            self.assertEqual(
                "black_on_white",
                manifest["preview_verification"]["color_policy"],
            )
            self.assertEqual(2, manifest["preview_verification"]["layer_count"])
            self.assertEqual(
                {"OUTLINE": 4, "TEXT": 1},
                manifest["preview_verification"]["layer_entity_counts"],
            )
            self.assertEqual(
                {
                    "CIRCLE": 1,
                    "LINE": 1,
                    "LWPOLYLINE": 2,
                    "TEXT": 1,
                },
                manifest["preview_verification"]["entity_type_counts"],
            )
            self.assertEqual(
                0,
                manifest["preview_verification"]["external_reference_count"],
            )
            self.assertEqual(3, len(result["details"]["artifacts"]))
            self.assertTrue(all(item["sha256"] for item in result["details"]["artifacts"]))
            manifest_artifact = next(
                item
                for item in result["details"]["artifacts"]
                if item["role"] == "generation_manifest"
            )
            manifest_verification = result["details"]["manifest_verification"]
            self.assertTrue(manifest_verification["verified"])
            self.assertTrue(manifest_verification["artifact_digests_verified"])
            self.assertEqual(generation_id, manifest_verification["generation_id"])
            self.assertEqual(manifest["recovery"], manifest_verification["recovery"])
            self.assertEqual("1.0", manifest_verification["schema_version"])
            self.assertEqual(2, manifest_verification["artifact_count"])
            self.assertEqual(
                manifest_artifact["size_bytes"],
                manifest_verification["size_bytes"],
            )
            self.assertEqual(
                manifest_artifact["sha256"],
                manifest_verification["sha256"],
            )
            delivery_verification = result["details"]["delivery_verification"]
            self.assertTrue(delivery_verification["verified"])
            self.assertTrue(delivery_verification["artifact_digests_verified"])
            self.assertTrue(delivery_verification["promoted_artifacts_verified"])
            self.assertEqual(generation_id, delivery_verification["generation_id"])
            self.assertEqual(manifest["recovery"], delivery_verification["recovery"])
            self.assertEqual(
                manifest_artifact["sha256"],
                delivery_verification["sha256"],
            )
            self.assertIn("<svg", preview_path.read_text(encoding="utf-8"))
            preview_root = ET.fromstring(preview_path.read_text(encoding="utf-8"))
            preview_paths = [
                element
                for element in preview_root.iter()
                if element.tag.rsplit("}", 1)[-1] == "path"
            ]
            self.assertEqual(
                [
                    ("dxf-entity-0001", "OUTLINE", "LWPOLYLINE"),
                    ("dxf-entity-0002", "OUTLINE", "LINE"),
                    ("dxf-entity-0003", "OUTLINE", "CIRCLE"),
                    ("dxf-entity-0004", "OUTLINE", "LWPOLYLINE"),
                    ("dxf-entity-0005", "TEXT", "TEXT"),
                ],
                [
                    (
                        element.get("id"),
                        element.get("data-dxf-layer"),
                        element.get("data-dxf-type"),
                    )
                    for element in preview_paths
                ],
            )

            import ezdxf

            document = ezdxf.readfile(output)
            self.assertFalse(document.audit().has_errors)
            self.assertEqual(5, len(document.modelspace()))

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_never_overwrites_existing_batch(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "existing.dxf"
            output.write_text("preserve-me", encoding="utf-8")
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("output_batch_exists", result["details"]["status"])
            self.assertEqual("preserve-me", output.read_text(encoding="utf-8"))
            self.assertFalse(output.with_suffix(".manifest.json").exists())

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_never_overwrites_existing_preview(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "existing_preview.dxf"
            preview = output.with_suffix(".preview.svg")
            preview.write_text("preserve-preview", encoding="utf-8")
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("output_batch_exists", result["details"]["status"])
            self.assertEqual("preserve-preview", preview.read_text(encoding="utf-8"))
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_recovers_regular_staging_only_batch(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "recover_staging.dxf"
            staging_paths = [
                output.with_name(f".{output.name}.staging"),
                output.with_name(f".{output.stem}.preview.svg.staging"),
                output.with_name(f".{output.stem}.manifest.json.staging"),
            ]
            stale_time = time.time() - bridge.STAGING_RECOVERY_MIN_AGE_SECONDS - 1
            for path in staging_paths:
                path.write_text("interrupted batch", encoding="utf-8")
                os.utime(path, (stale_time, stale_time))
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertTrue(result["ok"])
            self.assertTrue(result["details"]["recovered_staging_batch"])
            self.assertEqual(
                {
                    "stale_staging_recovered": True,
                    "recovered_file_count": 3,
                    "minimum_age_seconds": bridge.STAGING_RECOVERY_MIN_AGE_SECONDS,
                },
                result["details"]["recovery"],
            )
            recovered_manifest = json.loads(
                output.with_suffix(".manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["details"]["recovery"], recovered_manifest["recovery"])
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".preview.svg").is_file())
            self.assertTrue(output.with_suffix(".manifest.json").is_file())
            self.assertTrue(all(not path.exists() for path in staging_paths))

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_never_removes_fresh_staging_file(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "active_staging.dxf"
            staging_file = output.with_name(f".{output.name}.staging")
            staging_file.write_text("active generation", encoding="utf-8")
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("generation_in_progress", result["details"]["status"])
            self.assertEqual("in_progress", result["details"]["state"])
            self.assertFalse(result["details"]["terminal"])
            self.assertFalse(result["details"]["result_ready"])
            self.assertGreaterEqual(result["details"]["retry_after_seconds"], 1)
            self.assertLessEqual(
                result["details"]["retry_after_seconds"],
                bridge.STAGING_RECOVERY_MIN_AGE_SECONDS,
            )
            self.assertEqual("active generation", staging_file.read_text(encoding="utf-8"))
            self.assertFalse(output.exists())

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_confirmed_write_never_removes_staging_directory(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "unsafe_staging.dxf"
            staging_directory = output.with_name(f".{output.name}.staging")
            staging_directory.mkdir()
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("output_batch_exists", result["details"]["status"])
            self.assertTrue(staging_directory.is_dir())
            self.assertFalse(output.exists())

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_preview_verification_failure_rolls_back_current_batch(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        original_verify = bridge._verify_svg_preview
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "rollback_preview.dxf"

            def fail_preview(
                _: Path,
                *,
                expected_entity_metadata: list[dict[str, str]] | None = None,
            ) -> dict:
                self.assertIsNotNone(expected_entity_metadata)
                raise ValueError("simulated preview verification failure")

            bridge._verify_svg_preview = fail_preview
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge._verify_svg_preview = original_verify
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("generation_failed", result["details"]["status"])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".preview.svg").exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())
            self.assertEqual([], list(Path(tmp).glob(".*.staging")))

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_manifest_verification_failure_rolls_back_current_batch(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        original_verify = bridge._verify_generation_manifest
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "rollback_manifest.dxf"

            def fail_manifest(
                _: Path,
                *,
                expected_artifacts: list[dict],
                artifact_paths: list[Path],
                expected_recovery: dict,
                expected_content_sha256: str,
                expected_mapping_sha256: str,
            ) -> dict:
                self.assertEqual(2, len(expected_artifacts))
                self.assertEqual(2, len(artifact_paths))
                self.assertFalse(expected_recovery["stale_staging_recovered"])
                self.assertRegex(expected_content_sha256, r"^[0-9a-f]{64}$")
                self.assertRegex(expected_mapping_sha256, r"^[0-9a-f]{64}$")
                raise ValueError("simulated manifest verification failure")

            bridge._verify_generation_manifest = fail_manifest
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge._verify_generation_manifest = original_verify
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("generation_failed", result["details"]["status"])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".preview.svg").exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())
            self.assertEqual([], list(Path(tmp).glob(".*.staging")))

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_post_promotion_verification_failure_rolls_back_delivered_batch(self) -> None:
        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        original_verify = bridge._verify_generation_manifest
        verification_calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "rollback_delivered_batch.dxf"

            def fail_after_promotion(
                manifest_path: Path,
                *,
                expected_artifacts: list[dict],
                artifact_paths: list[Path],
                expected_recovery: dict,
                expected_content_sha256: str,
                expected_mapping_sha256: str,
            ) -> dict:
                nonlocal verification_calls
                verification_calls += 1
                verified = original_verify(
                    manifest_path,
                    expected_artifacts=expected_artifacts,
                    artifact_paths=artifact_paths,
                    expected_recovery=expected_recovery,
                    expected_content_sha256=expected_content_sha256,
                    expected_mapping_sha256=expected_mapping_sha256,
                )
                if verification_calls == 2:
                    raise ValueError("simulated promoted batch verification failure")
                return verified

            bridge._verify_generation_manifest = fail_after_promotion
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                bridge._verify_generation_manifest = original_verify
                bridge.OUTPUT_ROOT = original_root

            self.assertEqual(2, verification_calls)
            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("generation_failed", result["details"]["status"])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".preview.svg").exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())
            self.assertEqual([], list(Path(tmp).glob(".*.staging")))

    @unittest.skipUnless(find_spec("ezdxf"), "ezdxf is not installed")
    def test_readback_geometry_mismatch_rolls_back_current_batch(self) -> None:
        import ezdxf

        bridge = autocad_dxf._bridge_instance
        original_root = bridge.OUTPUT_ROOT
        original_readfile = ezdxf.readfile
        with tempfile.TemporaryDirectory() as tmp:
            bridge.OUTPUT_ROOT = Path(tmp)
            output = Path(tmp) / "rollback_geometry.dxf"

            def tampered_readfile(path: Path):
                document = original_readfile(path)
                line = next(iter(document.modelspace().query("LINE")))
                line.dxf.end = (999, 999)
                return document

            ezdxf.readfile = tampered_readfile
            try:
                result = write_dxf(
                    minimal_plan(),
                    output,
                    dry_run=False,
                    confirm_write=True,
                )
            finally:
                ezdxf.readfile = original_readfile
                bridge.OUTPUT_ROOT = original_root

            self.assert_schema(result, "write_dxf")
            self.assertFalse(result["ok"])
            self.assertEqual("generation_failed", result["details"]["status"])
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".preview.svg").exists())
            self.assertFalse(output.with_suffix(".manifest.json").exists())
            self.assertEqual([], list(Path(tmp).glob(".*.staging")))

    def test_svg_preview_verifier_rejects_dark_background(self) -> None:
        bridge = autocad_dxf._bridge_instance
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / "dark.svg"
            preview.write_text(
                (
                    '<svg xmlns="http://www.w3.org/2000/svg">'
                    '<rect fill="#212830" width="10" height="10"/>'
                    '<path d="M 0 0 L 10 10" stroke="#ffffff"/>'
                    "</svg>"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "white background"):
                bridge._verify_svg_preview(preview)

    def test_manifest_verifier_rejects_tampered_artifact_digest(self) -> None:
        bridge = autocad_dxf._bridge_instance
        artifacts = [
            {
                "role": "cad_drawing",
                "relative_path": "drawing.dxf",
                "media_type": "image/vnd.dxf",
                "size_bytes": 10,
                "sha256": "a" * 64,
            },
            {
                "role": "cad_preview",
                "relative_path": "drawing.preview.svg",
                "media_type": "image/svg+xml",
                "size_bytes": 20,
                "sha256": "b" * 64,
            },
        ]
        manifest = {
            "schema_version": "1.0",
            "bridge": bridge.bridge_id,
            "action": "write_dxf",
            "state": "completed",
            "generation_id": bridge._generation_id(artifacts),
            "recovery": {
                "stale_staging_recovered": False,
                "recovered_file_count": 0,
                "minimum_age_seconds": bridge.STAGING_RECOVERY_MIN_AGE_SECONDS,
            },
            "artifact": artifacts[0],
            "artifacts": [dict(item) for item in artifacts],
            "verification": {
                "content_match": True,
                "content_sha256": "c" * 64,
            },
            "preview_verification": {
                "verified": True,
                "entity_metadata_match": True,
                "entity_mapping_sha256": "d" * 64,
            },
        }
        manifest["artifacts"][1]["sha256"] = "e" * 64

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "tampered.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact digests"):
                bridge._verify_generation_manifest(
                    manifest_path,
                    expected_artifacts=artifacts,
                    artifact_paths=[
                        Path(tmp) / "drawing.dxf",
                        Path(tmp) / "drawing.preview.svg",
                    ],
                    expected_recovery=manifest["recovery"],
                    expected_content_sha256="c" * 64,
                    expected_mapping_sha256="d" * 64,
                )

    def test_manifest_verifier_rejects_changed_staged_bytes(self) -> None:
        bridge = autocad_dxf._bridge_instance
        with tempfile.TemporaryDirectory() as tmp:
            dxf_path = Path(tmp) / "drawing.dxf"
            preview_path = Path(tmp) / "drawing.preview.svg"
            dxf_path.write_bytes(b"audited-dxf")
            preview_path.write_bytes(b"<svg/>")
            artifacts = [
                {
                    "role": "cad_drawing",
                    "relative_path": "drawing.dxf",
                    "media_type": "image/vnd.dxf",
                    "size_bytes": dxf_path.stat().st_size,
                    "sha256": bridge._sha256(dxf_path),
                },
                {
                    "role": "cad_preview",
                    "relative_path": "drawing.preview.svg",
                    "media_type": "image/svg+xml",
                    "size_bytes": preview_path.stat().st_size,
                    "sha256": bridge._sha256(preview_path),
                },
            ]
            manifest = {
                "schema_version": "1.0",
                "bridge": bridge.bridge_id,
                "action": "write_dxf",
                "state": "completed",
                "generation_id": bridge._generation_id(artifacts),
                "recovery": {
                    "stale_staging_recovered": False,
                    "recovered_file_count": 0,
                    "minimum_age_seconds": bridge.STAGING_RECOVERY_MIN_AGE_SECONDS,
                },
                "artifact": artifacts[0],
                "artifacts": artifacts,
                "verification": {
                    "content_match": True,
                    "content_sha256": "c" * 64,
                },
                "preview_verification": {
                    "verified": True,
                    "entity_metadata_match": True,
                    "entity_mapping_sha256": "d" * 64,
                },
            }
            manifest_path = Path(tmp) / "drawing.manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            preview_path.write_bytes(b"<svg>changed</svg>")

            with self.assertRaisesRegex(ValueError, "artifact bytes"):
                bridge._verify_generation_manifest(
                    manifest_path,
                    expected_artifacts=artifacts,
                    artifact_paths=[dxf_path, preview_path],
                    expected_recovery=manifest["recovery"],
                    expected_content_sha256="c" * 64,
                    expected_mapping_sha256="d" * 64,
                )

    def test_svg_annotation_requires_one_path_per_entity(self) -> None:
        class EmptyDocument:
            def modelspace(self) -> list:
                return []

        bridge = autocad_dxf._bridge_instance
        source = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 L 10 10"/></svg>'
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            bridge._annotate_svg_entities(source, EmptyDocument())

    def test_svg_preview_verifier_rejects_metadata_that_disagrees_with_readback(
        self,
    ) -> None:
        bridge = autocad_dxf._bridge_instance
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / "wrong-layer.svg"
            preview.write_text(
                (
                    '<svg xmlns="http://www.w3.org/2000/svg">'
                    '<rect fill="#ffffff" width="10" height="10"/>'
                    '<path id="dxf-entity-0001" data-dxf-layer="WRONG" '
                    'data-dxf-type="LINE" d="M 0 0 L 10 10" stroke="#000000"/>'
                    "</svg>"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match DXF readback"):
                bridge._verify_svg_preview(
                    preview,
                    expected_entity_metadata=[
                        {
                            "id": "dxf-entity-0001",
                            "layer": "OUTLINE",
                            "type": "LINE",
                        }
                    ],
                )


if __name__ == "__main__":
    unittest.main()
