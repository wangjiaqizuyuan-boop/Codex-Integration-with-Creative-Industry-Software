from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType
from unittest import mock

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "examples" / "illustrator_bridge" / "scripts"


def load_script_module(name: str, filename: str) -> ModuleType:
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load test module: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

exact = load_script_module("exact_pixel_vector_for_tests", "exact_pixel_vector.py")


class ExactPixelVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_dir.name)
        self.sandbox = self.root / "examples" / "output" / "illustrator" / "exact-pixel"
        self.repo_patch = mock.patch.object(exact, "REPO_ROOT", self.root)
        self.sandbox_patch = mock.patch.object(exact, "SANDBOX_ROOT", self.sandbox)
        self.repo_patch.start()
        self.sandbox_patch.start()

    def tearDown(self) -> None:
        self.sandbox_patch.stop()
        self.repo_patch.stop()
        self.temporary_dir.cleanup()

    def make_source(self) -> Path:
        source = self.root / "private-customer-name.png"
        image = Image.new("RGBA", (4, 3), (255, 255, 255, 255))
        image.putdata(
            [
                (255, 0, 0, 255),
                (255, 0, 0, 255),
                (0, 0, 255, 128),
                (0, 0, 255, 128),
                (255, 0, 0, 255),
                (0, 255, 0, 0),
                (0, 255, 0, 0),
                (0, 0, 255, 128),
                (255, 255, 255, 255),
                (255, 255, 255, 255),
                (255, 255, 255, 255),
                (255, 255, 255, 255),
            ]
        )
        image.save(source)
        return source

    def args(self, source: Path, output_name: str = "case") -> argparse.Namespace:
        return argparse.Namespace(
            input=str(source),
            reference_id="sample",
            output_dir=str(self.sandbox / output_name),
            max_pixels=100,
            max_subpaths=100,
        )

    def test_rebuilds_every_pixel_as_verified_raster_free_svg_paths(self) -> None:
        source = self.make_source()

        result = exact.run_exact_vector(self.args(source))

        output = self.sandbox / "case"
        svg_path = output / "exact_pixel_vector.svg"
        report_path = output / "exact_pixel_vector.report.json"
        svg_text = svg_path.read_text(encoding="utf-8")
        root = ET.fromstring(svg_text)
        paths = root.findall(f"{{{exact.verify_svg_artifact.__globals__['SVG_NAMESPACE']}}}path")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"]["pixel_count"], 12)
        self.assertEqual(result["vector"]["covered_pixel_count"], 12)
        self.assertEqual(result["vector"]["rectangle_subpaths"], 6)
        self.assertEqual(result["vector"]["path_objects"], 4)
        self.assertEqual(result["vector"]["addressable_paint_objects"], 4)
        self.assertEqual(result["vector"]["paint_object_id_scheme"], "paint-{rrggbbaa}")
        self.assertEqual(len(paths), 4)
        self.assertEqual(
            [path.get("id") for path in paths],
            [
                "paint-0000ff80",
                "paint-00ff0000",
                "paint-ff0000ff",
                "paint-ffffffff",
            ],
        )
        self.assertIn('fill-opacity="0.501960784313725"', svg_text)
        self.assertIn('fill-opacity="0"', svg_text)
        self.assertNotIn("<image", svg_text)
        self.assertNotIn("data:image", svg_text)
        self.assertTrue(report_path.is_file())
        self.assertEqual(
            {
                "state": "completed",
                "atomic_batch": True,
                "overwritten": False,
                "artifact_count": 2,
            },
            result["delivery"],
        )

    def test_reports_are_deterministic_and_do_not_leak_private_input_names(self) -> None:
        source = self.make_source()

        first = exact.run_exact_vector(self.args(source, "first"))
        second = exact.run_exact_vector(self.args(source, "second"))

        self.assertEqual(first["artifact"]["sha256"], second["artifact"]["sha256"])
        serialized = json.dumps(first)
        self.assertNotIn(source.name, serialized)
        self.assertNotIn(str(source.parent), serialized)
        self.assertFalse(Path(first["artifact"]["path"]).is_absolute())

    def test_verifier_rejects_paint_ids_that_do_not_match_rgba(self) -> None:
        source = self.make_source()
        exact.run_exact_vector(self.args(source))
        svg_path = self.sandbox / "case" / "exact_pixel_vector.svg"
        svg_text = svg_path.read_text(encoding="utf-8")

        for replacement in ("paint-0000ff81", "customer-private-name"):
            with self.subTest(replacement=replacement):
                tampered = self.root / f"{replacement}.svg"
                tampered.write_text(
                    svg_text.replace("paint-0000ff80", replacement),
                    encoding="utf-8",
                )
                with self.assertRaises(exact.SvgArtifactError) as raised:
                    exact.verify_svg_artifact(tampered)
                self.assertEqual("invalid_paint_object_id", raised.exception.code)

    def test_rejects_outside_outputs_and_over_complex_images_without_partial_files(self) -> None:
        source = self.make_source()
        outside = self.args(source)
        outside.output_dir = str(self.root / "outside")
        with self.assertRaises(exact.ExactVectorError) as outside_error:
            exact.run_exact_vector(outside)
        self.assertEqual(outside_error.exception.code, "output_outside_sandbox")

        complex_args = self.args(source, "too-complex")
        complex_args.max_subpaths = 1
        with self.assertRaises(exact.ExactVectorError) as complex_error:
            exact.run_exact_vector(complex_args)
        self.assertEqual(complex_error.exception.code, "vector_too_complex")
        self.assertFalse((self.sandbox / "too-complex").exists())

    def test_existing_batch_is_preserved_without_staging_residue(self) -> None:
        source = self.make_source()
        args = self.args(source)
        exact.run_exact_vector(args)
        output = self.sandbox / "case"
        before = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}

        with self.assertRaises(exact.ExactVectorError) as conflict:
            exact.run_exact_vector(args)

        after = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
        self.assertEqual("output_batch_exists", conflict.exception.code)
        self.assertEqual(before, after)
        self.assertEqual([], list(self.sandbox.glob(".exact-pixel-staging-*")))

    def test_publish_failure_cleans_staging_and_leaves_no_batch(self) -> None:
        source = self.make_source()
        args = self.args(source, "publish-failure")

        with (
            mock.patch.object(Path, "rename", side_effect=OSError("simulated publish failure")),
            self.assertRaises(exact.ExactVectorError) as publish_error,
        ):
            exact.run_exact_vector(args)

        self.assertEqual("output_publish_failed", publish_error.exception.code)
        self.assertFalse((self.sandbox / "publish-failure").exists())
        self.assertEqual([], list(self.sandbox.glob(".exact-pixel-staging-*")))


if __name__ == "__main__":
    unittest.main()
