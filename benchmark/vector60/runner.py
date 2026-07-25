"""Run the local Vector60 benchmark without publishing private dataset metadata."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from starbridge_mcp.vectorization.engine import RunConfig, run_vectorization
from starbridge_mcp.vectorization.svg_verify import verify_svg_artifact
from starbridge_mcp.vectorization.vector60.scorer import score_final_svg_candidate

from .aggregate import (
    CATEGORIES,
    EXPECTED_CASE_IDS,
    SCHEMA_VERSION,
    aggregate_summary,
    render_markdown,
)

MANIFEST_VERSION = "vector60-manifest-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = (
    REPOSITORY_ROOT / "examples" / "output" / "vectorization" / "vector60-benchmark"
)
_MANIFEST_KEYS = frozenset({"schema_version", "cases"})
_MANIFEST_CASE_KEYS = frozenset({"case_id", "category", "input"})
_CATEGORY_SCENES = {
    "logo_or_icon": "logo",
    "lineart": "lineart",
    "flat": "flat",
    "illustration": "illustration",
}
_SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class BenchmarkRunnerError(RuntimeError):
    """A safe runner failure whose code never contains user data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SafeArgumentParser(argparse.ArgumentParser):
    """Avoid argparse messages that echo private command-line values."""

    def error(self, message: str) -> None:
        raise BenchmarkRunnerError("invalid_arguments")


@dataclass(frozen=True)
class ManifestCase:
    case_id: str
    category: str
    input_path: Path


@dataclass(frozen=True)
class BenchmarkPlan:
    case_count: int
    category_counts: Mapping[str, int]
    action: str

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_VERSION,
            "action": self.action,
            "dataset_status": "ready",
            "case_count": self.case_count,
            "category_counts": dict(self.category_counts),
            "formal_metrics_status": "unverified",
            "writes_planned": self.action == "run",
        }


@dataclass(frozen=True)
class FormalMetrics:
    """Metrics admitted only from an original-resolution final SVG render."""

    ssim: float
    normalized_mae: float
    edge_dice: float
    anchors: int
    subpaths: int
    svg_bytes: int
    reference_width: int
    reference_height: int
    rendered_width: int
    rendered_height: int
    elapsed_seconds: float = 0.0
    render_kind: str = "final_svg"

    def __post_init__(self) -> None:
        if self.render_kind != "final_svg":
            raise BenchmarkRunnerError("preview_evidence_rejected")
        if (self.reference_width, self.reference_height) != (
            self.rendered_width,
            self.rendered_height,
        ):
            raise BenchmarkRunnerError("non_original_resolution_evidence")
        if (
            min(
                self.reference_width,
                self.reference_height,
                self.rendered_width,
                self.rendered_height,
            )
            <= 0
        ):
            raise BenchmarkRunnerError("invalid_render_dimensions")
        if not all(
            math.isfinite(value)
            for value in (
                self.ssim,
                self.normalized_mae,
                self.edge_dice,
                self.elapsed_seconds,
            )
        ):
            raise BenchmarkRunnerError("invalid_formal_metrics")
        if not -1.0 <= self.ssim <= 1.0:
            raise BenchmarkRunnerError("invalid_formal_metrics")
        if not 0.0 <= self.normalized_mae <= 1.0:
            raise BenchmarkRunnerError("invalid_formal_metrics")
        if not 0.0 <= self.edge_dice <= 1.0:
            raise BenchmarkRunnerError("invalid_formal_metrics")
        if min(self.anchors, self.subpaths, self.svg_bytes) < 0:
            raise BenchmarkRunnerError("invalid_formal_metrics")
        if self.elapsed_seconds < 0:
            raise BenchmarkRunnerError("invalid_formal_metrics")


Vectorizer = Callable[[RunConfig], Mapping[str, Any]]
Scorer = Callable[[Image.Image, Path, Path, int, int], FormalMetrics]
Verifier = Callable[[Path, int, int], Mapping[str, Any]]


def _default_scorer(
    reference: Image.Image,
    svg_path: Path,
    render_path: Path,
    expected_width: int,
    expected_height: int,
) -> FormalMetrics:
    score = score_final_svg_candidate(
        candidate_id="benchmark_final",
        reference=reference,
        svg_path=svg_path,
        render_path=render_path,
        expected_svg_width=expected_width,
        expected_svg_height=expected_height,
    )
    return FormalMetrics(
        ssim=score.visual.ssim,
        normalized_mae=score.visual.normalized_mae,
        edge_dice=score.visual.edge_dice,
        anchors=score.complexity.anchors,
        subpaths=score.complexity.subpaths,
        svg_bytes=score.complexity.bytes,
        reference_width=score.evidence.original_width,
        reference_height=score.evidence.original_height,
        rendered_width=score.evidence.render_width,
        rendered_height=score.evidence.render_height,
        elapsed_seconds=score.elapsed_seconds,
        render_kind=score.evidence.render_kind,
    )


def _default_verifier(path: Path, width: int, height: int) -> Mapping[str, Any]:
    return verify_svg_artifact(path, expected_width=width, expected_height=height)


def _safe_unverified_document() -> dict[str, Any]:
    cases = [
        {
            "case_id": f"{category}-{index:02d}",
            "category": category,
            "status": "unverified",
        }
        for category in CATEGORIES
        for index in range(1, 11)
    ]
    return {"schema_version": SCHEMA_VERSION, "cases": cases}


def _load_manifest(manifest_path: Path) -> list[ManifestCase]:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BenchmarkRunnerError("dataset_unavailable") from None
    if not isinstance(document, Mapping) or set(document) != _MANIFEST_KEYS:
        raise BenchmarkRunnerError("invalid_manifest")
    if document.get("schema_version") != MANIFEST_VERSION:
        raise BenchmarkRunnerError("invalid_manifest")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != 40:
        raise BenchmarkRunnerError("invalid_manifest")

    manifest_cases: list[ManifestCase] = []
    seen_ids: set[str] = set()
    manifest_directory = manifest_path.resolve().parent
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping) or set(raw_case) != _MANIFEST_CASE_KEYS:
            raise BenchmarkRunnerError("invalid_manifest")
        case_id = raw_case.get("case_id")
        category = raw_case.get("category")
        input_value = raw_case.get("input")
        if (
            case_id not in EXPECTED_CASE_IDS
            or case_id in seen_ids
            or category not in CATEGORIES
            or not case_id.startswith(f"{category}-")
            or not isinstance(input_value, str)
            or not input_value
            or "\x00" in input_value
        ):
            raise BenchmarkRunnerError("invalid_manifest")
        input_path = Path(input_value)
        if not input_path.is_absolute():
            input_path = manifest_directory / input_path
        manifest_cases.append(
            ManifestCase(
                case_id=str(case_id),
                category=str(category),
                input_path=input_path.resolve(),
            )
        )
        seen_ids.add(str(case_id))
    if seen_ids != EXPECTED_CASE_IDS:
        raise BenchmarkRunnerError("invalid_manifest")
    return sorted(manifest_cases, key=lambda case: case.case_id)


def _assert_local_output_root(output_root: Path) -> Path:
    resolved_output = output_root.resolve()
    try:
        resolved_output.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        return resolved_output
    allowed_root = (REPOSITORY_ROOT / "examples" / "output").resolve()
    try:
        resolved_output.relative_to(allowed_root)
    except ValueError:
        raise BenchmarkRunnerError("output_must_be_gitignored") from None
    if resolved_output == allowed_root:
        raise BenchmarkRunnerError("output_root_too_broad")
    return resolved_output


def _validate_dataset(cases: Sequence[ManifestCase], output_root: Path) -> None:
    resolved_inputs: set[Path] = set()
    for case in cases:
        path = case.input_path
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES or not path.is_file():
            raise BenchmarkRunnerError("dataset_unavailable")
        if output_root == path or output_root in path.parents:
            raise BenchmarkRunnerError("source_inside_output_root")
        if path in resolved_inputs:
            raise BenchmarkRunnerError("duplicate_input")
        resolved_inputs.add(path)


def validate_manifest(manifest_path: Path, output_root: Path) -> BenchmarkPlan:
    """Validate exactly the explicitly listed 40 items without scanning directories."""

    resolved_output = _assert_local_output_root(output_root)
    cases = _load_manifest(manifest_path)
    _validate_dataset(cases, resolved_output)
    category_counts = {
        category: sum(case.category == category for case in cases) for category in CATEGORIES
    }
    return BenchmarkPlan(
        case_count=len(cases),
        category_counts=category_counts,
        action="validate",
    )


def dry_run_benchmark(manifest_path: Path, output_root: Path) -> dict[str, Any]:
    """Return an anonymous execution plan and perform no writes or image reads."""

    plan = validate_manifest(manifest_path, output_root)
    return BenchmarkPlan(
        case_count=plan.case_count,
        category_counts=plan.category_counts,
        action="dry-run",
    ).as_public_dict()


def _open_reference(path: Path) -> Image.Image:
    try:
        with Image.open(path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGBA")
    except Exception:
        raise BenchmarkRunnerError("invalid_dataset_image") from None


def _result_dimensions(result: Mapping[str, Any]) -> tuple[int, int]:
    vector = result.get("vector")
    if not isinstance(vector, Mapping):
        raise BenchmarkRunnerError("invalid_vectorization_result")
    width = vector.get("width")
    height = vector.get("height")
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise BenchmarkRunnerError("invalid_vectorization_result")
    return width, height


def _safe_svg(verifier: Verifier, path: Path, width: int, height: int) -> None:
    evidence = verifier(path, width, height)
    if (
        evidence.get("verified") is not True
        or evidence.get("embedded_raster_count") != 0
        or evidence.get("external_reference_count") != 0
    ):
        raise BenchmarkRunnerError("unsafe_svg")


def _comparison_4x(left_path: Path, right_path: Path, output_path: Path) -> None:
    try:
        with Image.open(left_path) as opened_left, Image.open(right_path) as opened_right:
            left = opened_left.convert("RGB")
            right = opened_right.convert("RGB")
        if left.size != right.size:
            raise BenchmarkRunnerError("comparison_dimension_mismatch")
        scaled_size = (left.width * 4, left.height * 4)
        left = left.resize(scaled_size, Image.Resampling.NEAREST)
        right = right.resize(scaled_size, Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (left.width * 2, left.height), "white")
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width, 0))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="PNG", optimize=False)
    except BenchmarkRunnerError:
        raise
    except Exception:
        raise BenchmarkRunnerError("comparison_failed") from None


def _copy_artifacts(
    *,
    case: ManifestCase,
    output_root: Path,
    baseline_svg: Path,
    enhanced_svg: Path,
    baseline_render: Path,
    enhanced_render: Path,
) -> None:
    artifact_directory = output_root / "artifacts" / case.category / case.case_id
    artifact_directory.mkdir(parents=True, exist_ok=True)
    for source, filename in (
        (baseline_svg, "artisan_baseline.svg"),
        (enhanced_svg, "auto_enhance.svg"),
        (baseline_render, "artisan_baseline.png"),
        (enhanced_render, "auto_enhance.png"),
    ):
        shutil.copyfile(source, artifact_directory / filename)
    _comparison_4x(
        baseline_render,
        enhanced_render,
        output_root / "comparisons" / case.category / f"{case.case_id}.png",
    )


def _run_case(
    case: ManifestCase,
    *,
    temporary_root: Path,
    output_root: Path,
    vectorizer: Vectorizer,
    scorer: Scorer,
    verifier: Verifier,
) -> dict[str, Any]:
    reference = _open_reference(case.input_path)
    baseline_directory = temporary_root / case.case_id / "baseline"
    enhanced_directory = temporary_root / case.case_id / "auto-enhance"
    baseline_result = vectorizer(
        RunConfig(
            input_path=str(case.input_path),
            mode="artisan",
            reference_id=f"{case.case_id}-baseline",
            output_dir=str(baseline_directory),
            output_root=str(temporary_root),
            auto_enhance=False,
        )
    )
    baseline_svg = baseline_directory / "artisan_baseline.svg"
    baseline_width, baseline_height = _result_dimensions(baseline_result)
    _safe_svg(verifier, baseline_svg, baseline_width, baseline_height)
    baseline_render = temporary_root / case.case_id / "artisan-baseline-final.png"
    baseline_metrics = scorer(
        reference,
        baseline_svg,
        baseline_render,
        baseline_width,
        baseline_height,
    )

    fallback_used = False
    enhanced_svg = enhanced_directory / "vector.svg"
    enhanced_render = temporary_root / case.case_id / "auto-enhance-final.png"
    try:
        enhanced_result = vectorizer(
            RunConfig(
                input_path=str(case.input_path),
                mode="artisan",
                reference_id=f"{case.case_id}-auto-enhance",
                output_dir=str(enhanced_directory),
                output_root=str(temporary_root),
                auto_enhance=True,
                scene_preset=_CATEGORY_SCENES[case.category],
            )
        )
        enhanced_width, enhanced_height = _result_dimensions(enhanced_result)
        _safe_svg(verifier, enhanced_svg, enhanced_width, enhanced_height)
        enhanced_metrics = scorer(
            reference,
            enhanced_svg,
            enhanced_render,
            enhanced_width,
            enhanced_height,
        )
        vector60 = enhanced_result.get("vector60")
        fallback_used = not isinstance(vector60, Mapping) or vector60.get("status") != "selected"
    except Exception:
        fallback_used = True
        enhanced_svg = baseline_svg
        enhanced_render.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline_render, enhanced_render)
        enhanced_metrics = baseline_metrics

    _copy_artifacts(
        case=case,
        output_root=output_root,
        baseline_svg=baseline_svg,
        enhanced_svg=enhanced_svg,
        baseline_render=baseline_render,
        enhanced_render=enhanced_render,
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "status": "passed",
        "fallback_used": fallback_used,
        "metrics": {
            "ssim": enhanced_metrics.ssim,
            "artisan_baseline_ssim": baseline_metrics.ssim,
            "edge_dice": enhanced_metrics.edge_dice,
            "artisan_baseline_edge_dice": baseline_metrics.edge_dice,
            "normalized_mae": enhanced_metrics.normalized_mae,
            "artisan_baseline_normalized_mae": baseline_metrics.normalized_mae,
            "anchor_count": enhanced_metrics.anchors,
            "artisan_baseline_anchor_count": baseline_metrics.anchors,
            "subpath_count": enhanced_metrics.subpaths,
            "artisan_baseline_subpath_count": baseline_metrics.subpaths,
            "svg_bytes": enhanced_metrics.svg_bytes,
            "artisan_baseline_svg_bytes": baseline_metrics.svg_bytes,
            "elapsed_seconds": enhanced_metrics.elapsed_seconds,
            "artisan_baseline_elapsed_seconds": baseline_metrics.elapsed_seconds,
            "safe_svg": {
                "no_bitmap": True,
                "no_script": True,
                "no_external_links": True,
            },
        },
    }


def _write_outputs(output_root: Path, result: Mapping[str, Any]) -> None:
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (output_root / "report.md").write_text(
            render_markdown(result),
            encoding="utf-8",
            newline="\n",
        )
    except (OSError, UnicodeError):
        raise BenchmarkRunnerError("output_write_failed") from None


def run_benchmark(
    manifest_path: Path,
    output_root: Path,
    *,
    vectorizer: Vectorizer = run_vectorization,
    scorer: Scorer = _default_scorer,
    verifier: Verifier = _default_verifier,
) -> dict[str, Any]:
    """Execute 40 explicit cases or emit a wholly unverified anonymous result."""

    resolved_output = _assert_local_output_root(output_root)
    try:
        cases = _load_manifest(manifest_path)
        _validate_dataset(cases, resolved_output)
    except BenchmarkRunnerError:
        result = aggregate_summary(_safe_unverified_document())
        result["dataset_status"] = "unverified"
        _write_outputs(resolved_output, result)
        return result

    case_summaries: list[dict[str, Any]] = []
    generated_comparisons: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="vector60-benchmark-") as temporary:
        temporary_root = Path(temporary).resolve()
        for case in cases:
            try:
                case_summary = _run_case(
                    case,
                    temporary_root=temporary_root,
                    output_root=resolved_output,
                    vectorizer=vectorizer,
                    scorer=scorer,
                    verifier=verifier,
                )
            except Exception:
                case_summary = {
                    "case_id": case.case_id,
                    "category": case.category,
                    "status": "failed",
                    "fallback_used": False,
                }
            else:
                generated_comparisons.add(case.case_id)
            case_summaries.append(case_summary)

    result = aggregate_summary({"schema_version": SCHEMA_VERSION, "cases": case_summaries})
    result["dataset_status"] = "ready"
    for comparison in result["comparisons"]:
        if comparison["case_id"] in generated_comparisons:
            comparison["status"] = "generated_unreviewed"
    _write_outputs(resolved_output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="Run the local privacy-safe Vector60 benchmark.")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument(
        "--action",
        choices=("validate", "dry-run", "run"),
        default="dry-run",
        help="Default dry-run performs no writes.",
    )
    return parser


def _failure_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unverified",
        "dataset_status": "unverified",
        "case_counts": {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "unverified": 40,
        },
        "fallback_count": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        manifest = Path(args.manifest)
        output_root = Path(args.output_root)
        if args.action == "validate":
            plan = validate_manifest(manifest, output_root)
            payload = {"ok": True, **plan.as_public_dict()}
        elif args.action == "dry-run":
            payload = {"ok": True, **dry_run_benchmark(manifest, output_root)}
        else:
            result = run_benchmark(manifest, output_root)
            payload = {
                "ok": result["dataset_status"] == "ready",
                "status": result["overall_status"],
                "dataset_status": result["dataset_status"],
                "case_counts": result["case_counts"],
                "fallback_count": result["fallback_count"],
            }
    except Exception:
        payload = _failure_payload()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
