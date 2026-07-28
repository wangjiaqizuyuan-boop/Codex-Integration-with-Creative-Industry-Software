from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    if (parent / "starbridge_mcp").is_dir():
        sys.path.insert(0, str(parent))
        break

from starbridge_mcp.bridges.blender_safe_scene import build_scene_plan
from starbridge_mcp.core.security import sanitize

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = Path(__file__).resolve()
OUTPUT_ROOT = (REPO_ROOT / "output").resolve()
TEMPLATE_ID = "starbridge_public_scene_v1"
ARTIFACT_SPECS = (
    ("editable_scene", "scene.blend", "application/x-blender"),
    ("preview_render", "preview.png", "image/png"),
    ("execution_receipt", "receipt.json", "application/json"),
)


class OutputPolicyError(ValueError):
    pass


def _failure(code: str, message: str) -> dict[str, Any]:
    return sanitize(
        {
            "ok": False,
            "bridge": "blender",
            "action": "fixed_scene_render",
            "mode": "confirmed_local",
            "state": "failed",
            "submitted": False,
            "terminal": True,
            "result_ready": False,
            "error": {"code": code, "message": message},
            "failure_recovery": {
                "partial_success_allowed": False,
                "cleanup_scope": "current_staging_batch_only",
                "previous_verified_batches_preserved": True,
            },
        }
    )


def _resolve_output_directory(value: str) -> Path:
    raw = Path(str(value or "output/blender/fixed-scene"))
    candidate = raw if raw.is_absolute() else REPO_ROOT / raw
    resolved = candidate.resolve()
    if resolved == OUTPUT_ROOT or OUTPUT_ROOT not in resolved.parents:
        raise OutputPolicyError("Output must be a child directory of the ignored output root.")
    return resolved


def _find_blender_executable() -> Path | None:
    for variable in ("STARBRIDGE_BLENDER_EXE", "BLENDER_EXE"):
        configured = os.environ.get(variable)
        if configured:
            candidate = Path(configured)
            if candidate.is_file():
                return candidate
    discovered = shutil.which("blender") or shutil.which("blender.exe")
    return Path(discovered) if discovered else None


def _build_blender_command(blender: Path, output_dir: Path) -> list[str]:
    return [
        str(blender),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(SCRIPT_PATH),
        "--",
        "--worker",
        "--output-dir",
        str(output_dir),
    ]


def _run_blender(
    command: list[str], *, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _staging_directory(output_dir: Path) -> Path:
    return output_dir.with_name(f".{output_dir.name}.staging")


def _cleanup_current_batch(output_dir: Path, *, remove_final: bool = False) -> None:
    targets = [_staging_directory(output_dir)]
    if remove_final:
        targets.append(output_dir)
    for target in targets:
        resolved = target.resolve()
        if resolved != OUTPUT_ROOT and OUTPUT_ROOT in resolved.parents:
            shutil.rmtree(resolved, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_artifacts(output_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for role, relative_path, media_type in ARTIFACT_SPECS:
        path = output_dir / relative_path
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"required artifact missing or empty: {relative_path}")
        artifacts.append(
            {
                "role": role,
                "relative_path": relative_path,
                "media_type": media_type,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    if (
        receipt.get("ok") is not True
        or receipt.get("schema_version") != "1.0"
        or receipt.get("template_id") != TEMPLATE_ID
    ):
        raise ValueError("execution receipt does not match the fixed template contract")
    return artifacts


def run_fixed_scene(
    *,
    output_dir: str = "output/blender/fixed-scene",
    confirm_run: bool = False,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    plan = build_scene_plan()
    if not confirm_run:
        return sanitize(
            {
                "ok": True,
                "bridge": "blender",
                "action": "fixed_scene_render",
                "mode": "dry_run",
                "state": "planned",
                "submitted": False,
                "terminal": False,
                "result_ready": False,
                "template_id": TEMPLATE_ID,
                "scene": plan["scene"],
                "artifact_contract": plan["artifact_contract"],
                "confirmation_required": "--confirm-run",
                "write_policy": {
                    "output_root": "output/",
                    "existing_batch_overwrite": False,
                    "arbitrary_python": False,
                },
            }
        )

    try:
        resolved_output = _resolve_output_directory(output_dir)
    except (OSError, OutputPolicyError, ValueError):
        return _failure("output_not_allowed", "Output is outside the allowed ignored root.")

    if resolved_output.exists():
        return _failure(
            "output_batch_exists",
            "The target batch already exists; choose a new output batch name.",
        )

    blender = _find_blender_executable()
    if blender is None:
        return _failure(
            "blender_not_detected",
            "Blender executable was not detected; no generation was started.",
        )

    timeout = max(30, min(int(timeout_seconds), 900))
    try:
        completed = _run_blender(
            _build_blender_command(blender, resolved_output),
            timeout_seconds=timeout,
        )
    except subprocess.TimeoutExpired:
        _cleanup_current_batch(resolved_output)
        return _failure(
            "blender_timeout",
            "The fixed Blender generation timed out; the current staging batch is not accepted.",
        )
    except OSError:
        _cleanup_current_batch(resolved_output)
        return _failure(
            "blender_launch_failed",
            "Blender could not be started; no completed batch was accepted.",
        )

    if completed.returncode != 0:
        _cleanup_current_batch(resolved_output)
        return _failure(
            "blender_execution_failed",
            "Blender reported a fixed-template generation failure.",
        )

    try:
        artifacts = _collect_artifacts(resolved_output)
    except (OSError, ValueError, json.JSONDecodeError):
        _cleanup_current_batch(resolved_output, remove_final=True)
        return _failure(
            "artifact_verification_failed",
            "The generated batch did not satisfy the three-artifact contract.",
        )

    return sanitize(
        {
            "ok": True,
            "bridge": "blender",
            "action": "fixed_scene_render",
            "mode": "confirmed_local",
            "state": "completed",
            "submitted": True,
            "terminal": True,
            "result_ready": True,
            "template_id": TEMPLATE_ID,
            "artifacts": artifacts,
            "completion_rule": "all_required_artifacts_verified",
        }
    )


def _material(bpy: Any, name: str, color: tuple[float, float, float, float]) -> Any:
    material = bpy.data.materials.new(name=name)
    material.diffuse_color = color
    return material


def _point_camera_at(camera: Any, target: tuple[float, float, float]) -> None:
    from mathutils import Vector

    direction = Vector(target) - camera.location
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _worker_generate(output_dir_value: str) -> int:
    try:
        import bpy
    except ImportError:
        return 2

    try:
        output_dir = _resolve_output_directory(output_dir_value)
    except (OSError, OutputPolicyError, ValueError):
        return 2

    staging = _staging_directory(output_dir)
    if output_dir.exists() or staging.exists():
        return 2

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    try:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        scene.name = "starbridge_public_scene"
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.render.resolution_x = 640
        scene.render.resolution_y = 360
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = "//preview.png"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.world.color = (0.035, 0.045, 0.065)

        neutral = _material(bpy, "mat_grid_neutral", (0.18, 0.20, 0.24, 1.0))
        blue = _material(bpy, "mat_starbridge_blue", (0.05, 0.32, 0.80, 1.0))
        green = _material(bpy, "mat_safety_green", (0.08, 0.70, 0.34, 1.0))

        bpy.ops.mesh.primitive_plane_add(size=12, location=(0, 0, 0))
        ground = bpy.context.object
        ground.name = "ground_grid"
        ground.data.materials.append(neutral)

        bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
        cube = bpy.context.object
        cube.name = "center_cube"
        cube.data.materials.append(blue)

        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=(2.6, 0, 1))
        sphere = bpy.context.object
        sphere.name = "orbit_sphere"
        sphere.data.materials.append(green)

        beacon_locations = ((-2.5, -1.5, 0.6), (-2.5, 0, 0.6), (-2.5, 1.5, 0.6))
        for index, location in enumerate(beacon_locations):
            bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.35, depth=1.2, location=location)
            beacon = bpy.context.object
            beacon.name = f"axis_beacon_{index + 1}"
            beacon.data.materials.append(green if index == 1 else blue)

        bpy.ops.object.camera_add(location=(7.2, -7.2, 5.2))
        camera = bpy.context.object
        camera.name = "camera_overview"
        camera.data.lens = 42
        _point_camera_at(camera, (0, 0, 1))
        scene.camera = camera

        bpy.ops.object.light_add(type="AREA", location=(4, -3, 7))
        key_light = bpy.context.object
        key_light.name = "key_area_light"
        key_light.data.energy = 900
        key_light.data.shape = "DISK"
        key_light.data.size = 5

        bpy.ops.object.light_add(type="POINT", location=(-4, -1, 4))
        rim_light = bpy.context.object
        rim_light.name = "rim_point_light"
        rim_light.data.energy = 500

        scene_path = staging / "scene.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(scene_path))
        bpy.ops.render.render(write_still=True)

        preview_path = staging / "preview.png"
        if not scene_path.is_file() or not preview_path.is_file():
            raise RuntimeError("fixed outputs missing")

        receipt = {
            "ok": True,
            "schema_version": "1.0",
            "template_id": TEMPLATE_ID,
            "generator": "starbridge_fixed_blender_template",
            "artifacts": [
                {
                    "relative_path": "scene.blend",
                    "size_bytes": scene_path.stat().st_size,
                    "sha256": _sha256(scene_path),
                },
                {
                    "relative_path": "preview.png",
                    "size_bytes": preview_path.stat().st_size,
                    "sha256": _sha256(preview_path),
                },
            ],
        }
        (staging / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
        return 0
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        return 1


def _script_arguments() -> list[str]:
    arguments = sys.argv[1:]
    if "--" in arguments:
        arguments = arguments[arguments.index("--") + 1 :]
    return arguments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a fixed public Blender scene with an atomic artifact batch."
    )
    parser.add_argument("--output-dir", default="output/blender/fixed-scene")
    parser.add_argument("--confirm-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(_script_arguments())

    if args.worker:
        return _worker_generate(args.output_dir)

    result = run_fixed_scene(
        output_dir=args.output_dir,
        confirm_run=args.confirm_run,
        timeout_seconds=args.timeout_seconds,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Blender fixed scene:", result["state"])
        if not result["ok"]:
            print("error:", result["error"]["code"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
