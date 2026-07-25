from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from benchmark.vector60.runner import (
    REPOSITORY_ROOT,
    BenchmarkRunnerError,
    FormalMetrics,
    dry_run_benchmark,
    main,
    run_benchmark,
    validate_manifest,
)

CATEGORIES = ("logo_or_icon", "lineart", "flat", "illustration")


def write_dataset(root: Path, *, missing_id: str | None = None) -> Path:
    dataset = root / "private-dataset"
    dataset.mkdir(parents=True)
    cases: list[dict[str, str]] = []
    for category in CATEGORIES:
        for index in range(1, 11):
            case_id = f"{category}-{index:02d}"
            source = dataset / f"{case_id}.png"
            if case_id != missing_id:
                Image.new("RGBA", (4, 3), (index, 20, 30, 255)).save(source)
            cases.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "input": source.name,
                }
            )
    manifest = dataset / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "vector60-manifest-v1", "cases": cases}),
        encoding="utf-8",
    )
    return manifest


class FakeRuntime:
    def __init__(self, *, fail_enhanced: bool = False) -> None:
        self.configs = []
        self.fail_enhanced = fail_enhanced

    def vectorize(self, config):
        self.configs.append(config)
        output = Path(config.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "artisan_baseline.svg").write_text("baseline", encoding="utf-8")
        if config.auto_enhance and self.fail_enhanced:
            raise RuntimeError(f"private failure: {config.input_path}")
        (output / "vector.svg").write_text(
            "enhanced" if config.auto_enhance else "default",
            encoding="utf-8",
        )
        result = {"vector": {"width": 4, "height": 3}}
        if config.auto_enhance:
            result["vector60"] = {"status": "selected"}
        return result

    @staticmethod
    def score(reference, svg_path, render_path, expected_width, expected_height):
        render_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", reference.size, (10, 20, 30, 255)).save(render_path)
        is_baseline = svg_path.name == "artisan_baseline.svg"
        return FormalMetrics(
            ssim=0.95 if not is_baseline else 0.90,
            normalized_mae=0.04 if not is_baseline else 0.06,
            edge_dice=0.94 if not is_baseline else 0.90,
            anchors=70 if not is_baseline else 100,
            subpaths=5,
            svg_bytes=100,
            reference_width=reference.width,
            reference_height=reference.height,
            rendered_width=reference.width,
            rendered_height=reference.height,
        )

    @staticmethod
    def verify(path, width, height):
        if not path.is_file() or (width, height) != (4, 3):
            raise AssertionError("unexpected fake artifact")
        return {
            "verified": True,
            "embedded_raster_count": 0,
            "external_reference_count": 0,
        }


class Vector60BenchmarkRunnerTests(unittest.TestCase):
    def test_public_manifest_schema_is_strict_and_contains_no_local_values(self) -> None:
        schema_path = REPOSITORY_ROOT / "benchmark" / "vector60" / "manifest.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        serialized = json.dumps(schema, sort_keys=True)

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["cases"]["minItems"], 40)
        self.assertEqual(schema["properties"]["cases"]["maxItems"], 40)
        self.assertFalse(schema["$defs"]["case"]["additionalProperties"])
        self.assertNotIn(str(REPOSITORY_ROOT), serialized)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("cookie", serialized.lower())

    def test_validate_and_dry_run_read_only_the_explicit_manifest_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root)
            extra = manifest.parent / "not-listed.png"
            extra.write_bytes(b"not-an-image")
            output = root / "ignored-output"

            plan = validate_manifest(manifest, output)
            dry_run = dry_run_benchmark(manifest, output)

            self.assertEqual(plan.case_count, 40)
            self.assertEqual(plan.category_counts, dict.fromkeys(CATEGORIES, 10))
            self.assertEqual(dry_run["action"], "dry-run")
            self.assertEqual(dry_run["formal_metrics_status"], "unverified")
            self.assertFalse(dry_run["writes_planned"])
            self.assertFalse(output.exists())

    def test_runs_40_baselines_and_enhancements_without_report_path_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root)
            output = root / "ignored-output"
            runtime = FakeRuntime()

            result = run_benchmark(
                manifest,
                output,
                vectorizer=runtime.vectorize,
                scorer=runtime.score,
                verifier=runtime.verify,
            )

            self.assertEqual(len(runtime.configs), 80)
            self.assertEqual(sum(config.auto_enhance for config in runtime.configs), 40)
            self.assertTrue(all(config.mode == "artisan" for config in runtime.configs))
            self.assertEqual(result["dataset_status"], "ready")
            self.assertEqual(result["case_counts"]["passed"], 40)
            self.assertEqual(result["fallback_count"], 0)
            self.assertEqual(result["gates"]["edge_dice_median"]["status"], "passed")
            self.assertEqual(result["gates"]["normalized_mae_median"]["status"], "passed")
            self.assertEqual(result["gates"]["anchor_rule"]["status"], "passed")
            self.assertEqual(result["gates"]["seam_free_4x"]["status"], "unverified")
            first_metrics = result["cases"][0]["metrics"]
            self.assertEqual(first_metrics["ssim"], 0.95)
            self.assertEqual(first_metrics["artisan_baseline_ssim"], 0.90)
            self.assertEqual(first_metrics["subpath_count"], 5)
            self.assertEqual(first_metrics["svg_bytes"], 100)
            self.assertTrue(
                all(item["status"] == "generated_unreviewed" for item in result["comparisons"])
            )
            serialized = (output / "summary.json").read_text(encoding="utf-8") + (
                output / "report.md"
            ).read_text(encoding="utf-8")
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("private-dataset", serialized)
            self.assertNotIn(".png", (output / "report.md").read_text(encoding="utf-8"))
            comparison = output / "comparisons" / "logo_or_icon" / "logo_or_icon-01.png"
            with Image.open(comparison) as image:
                self.assertEqual(image.size, (32, 12))

    def test_missing_manifest_is_unverified_without_fake_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            runtime = FakeRuntime()

            result = run_benchmark(
                root / "private-missing-manifest.json",
                output,
                vectorizer=runtime.vectorize,
                scorer=runtime.score,
                verifier=runtime.verify,
            )

            self.assertEqual(runtime.configs, [])
            self.assertEqual(result["dataset_status"], "unverified")
            self.assertEqual(result["overall_status"], "unverified")
            self.assertEqual(result["case_counts"]["unverified"], 40)
            self.assertEqual(result["gates"]["success_count"]["status"], "unverified")
            self.assertFalse((output / "comparisons").exists())
            self.assertNotIn(str(root), (output / "summary.json").read_text(encoding="utf-8"))

    def test_one_missing_input_prevents_partial_dataset_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root, missing_id="flat-05")
            runtime = FakeRuntime()

            result = run_benchmark(
                manifest,
                root / "output",
                vectorizer=runtime.vectorize,
                scorer=runtime.score,
                verifier=runtime.verify,
            )

            self.assertEqual(runtime.configs, [])
            self.assertEqual(result["case_counts"]["unverified"], 40)

    def test_enhancement_failure_falls_back_to_scored_artisan_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root)
            output = root / "output"
            runtime = FakeRuntime(fail_enhanced=True)

            result = run_benchmark(
                manifest,
                output,
                vectorizer=runtime.vectorize,
                scorer=runtime.score,
                verifier=runtime.verify,
            )

            self.assertEqual(result["case_counts"]["passed"], 40)
            self.assertEqual(result["fallback_count"], 40)
            baseline = output / "artifacts" / "flat" / "flat-01" / "artisan_baseline.svg"
            enhanced = output / "artifacts" / "flat" / "flat-01" / "auto_enhance.svg"
            self.assertEqual(baseline.read_bytes(), enhanced.read_bytes())
            serialized = (output / "summary.json").read_text(encoding="utf-8")
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("private failure", serialized)

    def test_sources_below_output_root_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            manifest = write_dataset(output)
            source = output / "private-dataset" / "lineart-01.png"
            original = source.read_bytes()
            runtime = FakeRuntime()

            result = run_benchmark(
                manifest,
                output,
                vectorizer=runtime.vectorize,
                scorer=runtime.score,
                verifier=runtime.verify,
            )

            self.assertEqual(runtime.configs, [])
            self.assertEqual(result["case_counts"]["unverified"], 40)
            self.assertEqual(source.read_bytes(), original)

    def test_preview_or_resized_evidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkRunnerError, "preview_evidence_rejected"):
            FormalMetrics(0.9, 0.05, 0.9, 5, 2, 100, 4, 3, 4, 3, render_kind="preview")
        with self.assertRaisesRegex(BenchmarkRunnerError, "non_original_resolution_evidence"):
            FormalMetrics(0.9, 0.05, 0.9, 5, 2, 100, 4, 3, 8, 6)

    def test_duplicate_inputs_and_manifest_metadata_are_rejected_privately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["cases"][1]["input"] = document["cases"][0]["input"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkRunnerError, "duplicate_input"):
                validate_manifest(manifest, root / "output")

            document["token"] = "private-secret-value"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            output = root / "other-output"
            result = run_benchmark(manifest, output)
            serialized = (output / "summary.json").read_text(encoding="utf-8")
            self.assertEqual(result["case_counts"]["unverified"], 40)
            self.assertNotIn("private-secret-value", serialized)
            self.assertNotIn("token", serialized.lower())

    def test_in_repository_output_must_stay_under_ignored_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = write_dataset(Path(temporary))
            forbidden_output = REPOSITORY_ROOT / "benchmark" / "vector60" / "private-output"

            with self.assertRaisesRegex(BenchmarkRunnerError, "output_must_be_gitignored"):
                validate_manifest(manifest, forbidden_output)

    def test_cli_defaults_to_dry_run_and_never_echoes_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = write_dataset(root)
            output = root / "output"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--manifest",
                        str(manifest),
                        "--output-root",
                        str(output),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["action"], "dry-run")
            self.assertFalse(output.exists())
            self.assertNotIn(str(root), stdout.getvalue())
            self.assertNotIn(manifest.name, stdout.getvalue())

    def test_cli_invalid_manifest_path_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_name = "private-customer-manifest.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--manifest",
                        str(root / private_name),
                        "--output-root",
                        str(root / "output"),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertNotIn(private_name, stdout.getvalue())
            self.assertNotIn(str(root), stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["case_counts"]["unverified"], 40)


if __name__ == "__main__":
    unittest.main()
