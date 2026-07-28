from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.blender_bridge import render_fixed_scene


class BlenderFixedSceneRunnerTests(unittest.TestCase):
    def test_default_is_dry_run_and_never_starts_blender(self) -> None:
        with patch.object(
            render_fixed_scene.subprocess,
            "run",
            side_effect=AssertionError("dry-run must not start Blender"),
        ):
            result = render_fixed_scene.run_fixed_scene(
                output_dir="output/blender/fixed-scene-test",
                confirm_run=False,
            )

        self.assertTrue(result["ok"])
        self.assertEqual("dry_run", result["mode"])
        self.assertFalse(result["submitted"])
        self.assertEqual(
            {"scene.blend", "preview.png", "receipt.json"},
            {
                item["relative_path"]
                for item in result["artifact_contract"]["artifacts"]
            },
        )

    def test_confirmed_run_soft_fails_when_blender_is_missing(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"STARBRIDGE_BLENDER_EXE": "", "BLENDER_EXE": ""},
                clear=False,
            ),
            patch.object(render_fixed_scene.shutil, "which", return_value=None),
            patch.object(
                render_fixed_scene.subprocess,
                "run",
                side_effect=AssertionError("missing Blender must not start a process"),
            ),
        ):
            result = render_fixed_scene.run_fixed_scene(
                output_dir="output/blender/fixed-scene-test",
                confirm_run=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual("failed", result["state"])
        self.assertEqual("blender_not_detected", result["error"]["code"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["result_ready"])

    def test_confirmed_run_rejects_output_outside_ignored_root(self) -> None:
        result = render_fixed_scene.run_fixed_scene(
            output_dir="../private-output",
            confirm_run=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual("output_not_allowed", result["error"]["code"])
        self.assertNotIn(str(Path.home()), json.dumps(result))

    def test_success_requires_three_verified_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "fixed-scene"

            def fake_blender_run(
                _command: list[str], *, timeout_seconds: int
            ) -> subprocess.CompletedProcess[str]:
                self.assertGreaterEqual(timeout_seconds, 30)
                output_dir.mkdir()
                (output_dir / "scene.blend").write_bytes(b"BLENDER-fixed-scene")
                (output_dir / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\npreview")
                (output_dir / "receipt.json").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "schema_version": "1.0",
                            "template_id": "starbridge_public_scene_v1",
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                patch.object(
                    render_fixed_scene,
                    "_resolve_output_directory",
                    return_value=output_dir,
                ),
                patch.object(
                    render_fixed_scene,
                    "_find_blender_executable",
                    return_value=Path("blender"),
                ),
                patch.object(render_fixed_scene, "_run_blender", side_effect=fake_blender_run),
            ):
                result = render_fixed_scene.run_fixed_scene(
                    output_dir="output/blender/fixed-scene-test",
                    confirm_run=True,
                )

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["state"])
        self.assertTrue(result["submitted"])
        self.assertTrue(result["terminal"])
        self.assertTrue(result["result_ready"])
        self.assertEqual(3, len(result["artifacts"]))
        self.assertTrue(all(item["sha256"] for item in result["artifacts"]))
        self.assertNotIn(str(output_dir), json.dumps(result))

    def test_timeout_removes_only_current_staging_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary).resolve()
            output_dir = output_root / "fixed-scene"
            staging = output_root / ".fixed-scene.staging"
            preserved = output_root / "previous-verified"
            staging.mkdir()
            preserved.mkdir()
            (staging / "partial.blend").write_bytes(b"partial")
            (preserved / "receipt.json").write_text("{}", encoding="utf-8")

            with (
                patch.object(render_fixed_scene, "OUTPUT_ROOT", output_root),
                patch.object(
                    render_fixed_scene,
                    "_resolve_output_directory",
                    return_value=output_dir,
                ),
                patch.object(
                    render_fixed_scene,
                    "_find_blender_executable",
                    return_value=Path("blender"),
                ),
                patch.object(
                    render_fixed_scene,
                    "_run_blender",
                    side_effect=subprocess.TimeoutExpired(["blender"], 30),
                ),
            ):
                result = render_fixed_scene.run_fixed_scene(
                    output_dir="output/blender/fixed-scene-test",
                    confirm_run=True,
                    timeout_seconds=30,
                )

            self.assertFalse(result["ok"])
            self.assertEqual("blender_timeout", result["error"]["code"])
            self.assertFalse(staging.exists())
            self.assertTrue(preserved.is_dir())


if __name__ == "__main__":
    unittest.main()
