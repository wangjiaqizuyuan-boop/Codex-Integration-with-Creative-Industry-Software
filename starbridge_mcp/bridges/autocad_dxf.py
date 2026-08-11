from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from starbridge_mcp.bridges.cad_schema import DEFAULT_LAYERS, normalize_plan
from starbridge_mcp.core.bridge_base import BaseBridge
from starbridge_mcp.core.result_schema import make_result, validate_result
from starbridge_mcp.core.security import sanitize_result


class AutocadDxfBridge(BaseBridge):
    """Autocad/DXF bridge implementation using the common BaseBridge."""

    BRIDGE_ID = "autocad_dxf"
    REPO_ROOT = Path(__file__).resolve().parents[2]
    OUTPUT_ROOT = REPO_ROOT / "examples" / "cad" / "output"
    STAGING_RECOVERY_MIN_AGE_SECONDS = 15 * 60

    @property
    def bridge_id(self) -> str:
        return self.BRIDGE_ID

    @property
    def repo_root(self) -> Path:
        return self.REPO_ROOT

    def _result(
        self,
        *,
        ok: bool,
        action: str,
        message: str,
        details: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
        next_steps: list[str] | None = None,
    ) -> dict[str, Any]:
        result = make_result(
            ok=ok,
            bridge=self.bridge_id,
            action=action,
            message=message,
            details=details or {},
            warnings=warnings or [],
            next_steps=next_steps or [],
        )
        sanitized = sanitize_result(result)
        validate_result(sanitized)
        return sanitized

    def _ezdxf_available(self) -> bool:
        return importlib.util.find_spec("ezdxf") is not None

    def status(self) -> dict[str, Any]:
        ezdxf_available = self._ezdxf_available()
        warnings = []
        next_steps = []
        if not ezdxf_available:
            warnings.append(
                "ezdxf is not installed; dry-run validation still works, but DXF export is disabled."
            )
            next_steps.append(
                "Install ezdxf in a local environment if you want to write test DXF files."
            )
        return self._result(
            ok=True,
            action="status",
            message="AutoCAD / DXF headless bridge is available for safe plan validation.",
            details={
                "requires_autocad": False,
                "ezdxf_available": ezdxf_available,
                "output_root": "examples/cad/output",
                "default_dry_run": True,
                "supported_entities": [
                    "line",
                    "polyline",
                    "circle",
                    "arc",
                    "rectangle",
                    "hatch",
                    "text",
                ],
            },
            warnings=warnings,
            next_steps=next_steps,
        )

    def validate_cad_plan(self, plan: Any) -> dict[str, Any]:
        normalized, errors, warnings = normalize_plan(plan)
        return self._result(
            ok=not errors,
            action="validate_cad_plan",
            message="CAD plan is valid." if not errors else "CAD plan has validation errors.",
            details={
                "errors": errors,
                "normalized_plan": normalized if not errors else {},
                "entity_count": len(normalized.get("entities", []))
                if isinstance(normalized, dict)
                else 0,
                "layer_count": len(normalized.get("layers", []))
                if isinstance(normalized, dict)
                else 0,
            },
            warnings=warnings,
            next_steps=[] if not errors else ["Fix the validation errors before exporting DXF."],
        )

    def create_dxf_plan(self, prompt_or_spec: Any) -> dict[str, Any]:
        if isinstance(prompt_or_spec, dict):
            validation = self.validate_cad_plan(prompt_or_spec)
            if not validation["ok"]:
                return validation
            return self._result(
                ok=True,
                action="create_dxf_plan",
                message="Created CAD plan from structured spec.",
                details={"plan": validation["details"]["normalized_plan"]},
                warnings=validation["warnings"],
                next_steps=["Run summarize_plan or write_dxf with dry_run=True before exporting."],
            )

        prompt = str(prompt_or_spec or "").strip()
        width = 5000
        height = 3000
        if "large" in prompt.lower() or "大型" in prompt:
            width = 9000
            height = 6000
        if "6000" in prompt or "6米" in prompt:
            width = 6000
        if "4000" in prompt or "4米" in prompt:
            height = 4000

        plan = {
            "units": "mm",
            "layers": DEFAULT_LAYERS,
            "entities": [
                {
                    "type": "rectangle",
                    "layer": "OUTLINE",
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": height,
                },
                {
                    "type": "line",
                    "layer": "AUX",
                    "start": [width / 2, 0],
                    "end": [width / 2, height],
                },
                {
                    "type": "line",
                    "layer": "AUX",
                    "start": [0, height / 2],
                    "end": [width, height / 2],
                },
                {
                    "type": "text",
                    "layer": "TEXT",
                    "position": [200, height + 260],
                    "height": 180,
                    "value": "安全 DXF 计划示例",
                },
            ],
            "output": "example_generated.dxf",
        }
        return self._result(
            ok=True,
            action="create_dxf_plan",
            message="Created deterministic CAD plan from prompt.",
            details={"prompt_used": bool(prompt), "plan": plan},
            warnings=[],
            next_steps=["Review the plan, then run write_dxf with dry_run=True."],
        )

    def summarize_plan(self, plan: Any) -> dict[str, Any]:
        validation = self.validate_cad_plan(plan)
        normalized = validation["details"].get("normalized_plan", {})
        return self._result(
            ok=validation["ok"],
            action="summarize_plan",
            message="CAD plan summary is ready."
            if validation["ok"]
            else "Cannot summarize invalid CAD plan.",
            details=self._summary_details(normalized)
            if validation["ok"]
            else self._empty_summary(),
            warnings=validation["warnings"],
            next_steps=validation["next_steps"],
        )

    def _output_is_allowed(self, output_path: Path) -> bool:
        try:
            resolved = output_path.resolve()
            root = self.OUTPUT_ROOT.resolve()
            rel_path = resolved.relative_to(root)
            rel_str = str(rel_path).replace("\\", "/").lower()
            return not rel_str.startswith("examples/output")
        except (ValueError, OSError):
            # outside root or error -> not allowed
            return False

    def _resolve_output_path(self, output: str | Path) -> Path:
        raw = Path(str(output).replace("\\", "/"))
        if raw.is_absolute():
            return raw.resolve()
        declared_root = Path("examples/cad/output")
        try:
            raw = raw.relative_to(declared_root)
        except ValueError:
            pass
        return (self.OUTPUT_ROOT / raw).resolve()

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _generation_failure_type(error: Exception) -> str:
        if isinstance(error, ImportError):
            return "dependency_error"
        if isinstance(error, OSError):
            return "io_error"
        if isinstance(error, RuntimeError):
            return "runtime_error"
        return "verification_error"

    @staticmethod
    def _rollback_generation_paths(paths: list[Path]) -> dict[str, Any]:
        removed_file_count = 0
        failed_file_count = 0
        for path in paths:
            try:
                existed = path.exists() or path.is_symlink()
                path.unlink(missing_ok=True)
            except OSError:
                failed_file_count += 1
            else:
                if existed:
                    removed_file_count += 1
        return {
            "complete": failed_file_count == 0,
            "removed_file_count": removed_file_count,
            "failed_file_count": failed_file_count,
        }

    def _unit_code(self, unit_name: str | None) -> int:
        from ezdxf import units

        return {
            "mm": units.MM,
            "cm": units.CM,
            "m": units.M,
            "inch": units.IN,
        }.get(unit_name, units.MM)

    def _canonical_number(self, value: Any) -> float:
        number = round(float(value), 9)
        return 0.0 if number == 0 else number

    def _canonical_point(self, value: Any) -> list[float]:
        return [
            self._canonical_number(value[0]),
            self._canonical_number(value[1]),
        ]

    def _canonical_plan_content(self, normalized: dict[str, Any]) -> dict[str, Any]:
        entities = []
        for entity in normalized.get("entities", []):
            entity_type = entity["type"]
            canonical = {
                "layer": entity.get("layer", "0"),
            }
            if entity_type == "line":
                canonical.update(
                    type="LINE",
                    start=self._canonical_point(entity["start"]),
                    end=self._canonical_point(entity["end"]),
                )
            elif entity_type == "polyline":
                canonical.update(
                    type="LWPOLYLINE",
                    points=[self._canonical_point(point) for point in entity["points"]],
                    closed=bool(entity.get("closed", False)),
                )
            elif entity_type == "circle":
                canonical.update(
                    type="CIRCLE",
                    center=self._canonical_point(entity["center"]),
                    radius=self._canonical_number(entity["radius"]),
                )
            elif entity_type == "arc":
                start_angle = entity["start_angle"]
                end_angle = entity["end_angle"]
                if entity.get("clockwise", False):
                    start_angle, end_angle = end_angle, start_angle
                canonical.update(
                    type="ARC",
                    center=self._canonical_point(entity["center"]),
                    radius=self._canonical_number(entity["radius"]),
                    start_angle=self._canonical_number(start_angle),
                    end_angle=self._canonical_number(end_angle),
                )
            elif entity_type == "hatch":
                canonical.update(
                    type="HATCH",
                    points=[self._canonical_point(point) for point in entity["points"]],
                    closed=True,
                    solid_fill=True,
                    pattern_name="SOLID",
                )
            elif entity_type == "rectangle":
                x = entity["x"]
                y = entity["y"]
                width = entity["width"]
                height = entity["height"]
                canonical.update(
                    type="LWPOLYLINE",
                    points=[
                        self._canonical_point(point)
                        for point in (
                            (x, y),
                            (x + width, y),
                            (x + width, y + height),
                            (x, y + height),
                        )
                    ],
                    closed=True,
                )
            elif entity_type == "text":
                canonical.update(
                    type="TEXT",
                    position=self._canonical_point(entity["position"]),
                    height=self._canonical_number(entity["height"]),
                    value=entity["value"],
                )
            entities.append(canonical)
        return {
            "units": self._unit_code(normalized.get("units")),
            "layers": [
                {"name": layer["name"], "color": int(layer["color"])}
                for layer in normalized.get("layers", [])
            ],
            "entities": entities,
        }

    def _canonical_readback_content(
        self,
        document: Any,
        normalized: dict[str, Any],
    ) -> dict[str, Any]:
        layers = []
        for expected in normalized.get("layers", []):
            name = expected["name"]
            if not document.layers.has_entry(name):
                raise ValueError("generated DXF is missing an expected layer")
            layers.append(
                {
                    "name": name,
                    "color": int(document.layers.get(name).dxf.color),
                }
            )

        entities = []
        for entity in document.modelspace():
            entity_type = entity.dxftype()
            canonical = {
                "type": entity_type,
                "layer": entity.dxf.get("layer", "0"),
            }
            if entity_type == "LINE":
                canonical.update(
                    start=self._canonical_point(entity.dxf.start),
                    end=self._canonical_point(entity.dxf.end),
                )
            elif entity_type == "LWPOLYLINE":
                canonical.update(
                    points=[self._canonical_point(point) for point in entity.get_points("xy")],
                    closed=bool(entity.closed),
                )
            elif entity_type == "CIRCLE":
                canonical.update(
                    center=self._canonical_point(entity.dxf.center),
                    radius=self._canonical_number(entity.dxf.radius),
                )
            elif entity_type == "ARC":
                canonical.update(
                    center=self._canonical_point(entity.dxf.center),
                    radius=self._canonical_number(entity.dxf.radius),
                    start_angle=self._canonical_number(entity.dxf.start_angle),
                    end_angle=self._canonical_number(entity.dxf.end_angle),
                )
            elif entity_type == "HATCH":
                if len(entity.paths) != 1:
                    raise ValueError("generated HATCH must contain exactly one boundary path")
                boundary = entity.paths[0]
                if not boundary.is_closed:
                    raise ValueError("generated HATCH boundary is not closed")
                vertices = list(boundary.vertices)
                if any(not math.isclose(float(vertex[2]), 0.0) for vertex in vertices):
                    raise ValueError("generated HATCH boundary contains unsupported bulges")
                canonical.update(
                    points=[self._canonical_point(vertex) for vertex in vertices],
                    closed=True,
                    solid_fill=bool(entity.dxf.solid_fill),
                    pattern_name=str(entity.dxf.pattern_name).upper(),
                )
            elif entity_type == "TEXT":
                canonical.update(
                    position=self._canonical_point(entity.dxf.insert),
                    height=self._canonical_number(entity.dxf.height),
                    value=entity.dxf.text,
                )
            else:
                raise ValueError("generated DXF contains an unexpected entity type")
            entities.append(canonical)
        return {
            "units": int(document.units),
            "layers": layers,
            "entities": entities,
        }

    def _content_sha256(self, content: dict[str, Any]) -> str:
        payload = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _generation_id(self, artifacts: list[dict[str, Any]]) -> str:
        return f"sha256:{self._content_sha256({'artifacts': artifacts})}"

    def _annotate_svg_entities(self, text: str, document: Any) -> str:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ValueError("generated SVG preview is not valid XML") from exc

        paths = [
            element for element in root.iter() if element.tag.rsplit("}", 1)[-1].lower() == "path"
        ]
        entities = list(document.modelspace())
        if len(paths) != len(entities):
            raise ValueError("generated SVG paths do not map one-to-one to DXF entities")

        for path, metadata in zip(paths, self._svg_entity_metadata(document)):
            path.set("id", metadata["id"])
            path.set("data-dxf-layer", metadata["layer"])
            path.set("data-dxf-type", metadata["type"])

        ET.register_namespace("", "http://www.w3.org/2000/svg")
        return ET.tostring(
            root,
            encoding="unicode",
            xml_declaration=True,
        )

    def _svg_entity_metadata(self, document: Any) -> list[dict[str, str]]:
        return [
            {
                "id": f"dxf-entity-{index:04d}",
                "layer": str(entity.dxf.get("layer", "0")),
                "type": entity.dxftype(),
            }
            for index, entity in enumerate(document.modelspace(), start=1)
        ]

    def _write_and_audit_dxf(
        self,
        normalized: dict[str, Any],
        staging_path: Path,
    ) -> dict[str, Any]:
        import ezdxf

        document = ezdxf.new("R2010", setup=True)
        document.units = self._unit_code(normalized.get("units"))

        for layer in normalized.get("layers", []):
            name = layer["name"]
            color = layer["color"]
            if document.layers.has_entry(name):
                document.layers.get(name).dxf.color = color
            else:
                document.layers.add(name=name, color=color)

        modelspace = document.modelspace()
        for entity in normalized.get("entities", []):
            attributes = {"layer": entity.get("layer", "0")}
            entity_type = entity["type"]
            if entity_type == "line":
                modelspace.add_line(
                    entity["start"],
                    entity["end"],
                    dxfattribs=attributes,
                )
            elif entity_type == "polyline":
                modelspace.add_lwpolyline(
                    entity["points"],
                    close=entity.get("closed", False),
                    dxfattribs=attributes,
                )
            elif entity_type == "circle":
                modelspace.add_circle(
                    entity["center"],
                    entity["radius"],
                    dxfattribs=attributes,
                )
            elif entity_type == "arc":
                modelspace.add_arc(
                    entity["center"],
                    entity["radius"],
                    entity["start_angle"],
                    entity["end_angle"],
                    is_counter_clockwise=not entity.get("clockwise", False),
                    dxfattribs=attributes,
                )
            elif entity_type == "hatch":
                hatch = modelspace.add_hatch(color=7, dxfattribs=attributes)
                hatch.paths.add_polyline_path(entity["points"], is_closed=True)
            elif entity_type == "rectangle":
                x = entity["x"]
                y = entity["y"]
                width = entity["width"]
                height = entity["height"]
                modelspace.add_lwpolyline(
                    [
                        (x, y),
                        (x + width, y),
                        (x + width, y + height),
                        (x, y + height),
                    ],
                    close=True,
                    dxfattribs=attributes,
                )
            elif entity_type == "text":
                modelspace.add_text(
                    entity["value"],
                    height=entity["height"],
                    dxfattribs=attributes,
                ).set_placement(entity["position"])

        preflight_auditor = document.audit()
        if preflight_auditor.has_errors:
            raise ValueError("generated DXF failed pre-write audit")
        document.saveas(staging_path)

        readback = ezdxf.readfile(staging_path)
        readback_auditor = readback.audit()
        entity_count = len(readback.modelspace())
        expected_count = len(normalized.get("entities", []))
        if readback_auditor.has_errors or entity_count != expected_count:
            raise ValueError("generated DXF failed readback verification")
        expected_content_sha256 = self._content_sha256(self._canonical_plan_content(normalized))
        readback_content_sha256 = self._content_sha256(
            self._canonical_readback_content(readback, normalized)
        )
        if readback_content_sha256 != expected_content_sha256:
            raise ValueError("generated DXF content does not match the approved plan")
        return {
            "readback_ok": True,
            "audit_errors": len(readback_auditor.errors),
            "audit_fixes": len(readback_auditor.fixes),
            "entity_count": entity_count,
            "expected_entity_count": expected_count,
            "content_match": True,
            "content_sha256": readback_content_sha256,
        }

    def _verify_svg_preview(
        self,
        path: Path,
        *,
        expected_entity_metadata: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        payload = path.read_bytes()
        if not payload or len(payload) > 64 * 1024 * 1024:
            raise ValueError("generated SVG preview has an invalid size")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("generated SVG preview is not UTF-8") from exc

        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "<!doctype",
                "<!entity",
                "<script",
                "<image",
                "<foreignobject",
                "javascript:",
                "url(",
                "@import",
            )
        ):
            raise ValueError("generated SVG preview contains unsafe or external content")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ValueError("generated SVG preview is not valid XML") from exc

        namespace = "http://www.w3.org/2000/svg"
        if root.tag != f"{{{namespace}}}svg":
            raise ValueError("generated SVG preview has an invalid root element")

        path_count = 0
        background_count = 0
        entity_ids = []
        entity_metadata: list[dict[str, str]] = []
        layer_entity_counts: Counter[str] = Counter()
        entity_type_counts: Counter[str] = Counter()
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            if local_name in {"a", "iframe", "object", "embed", "use", "video", "audio"}:
                raise ValueError("generated SVG preview contains an external-capable element")
            for raw_name, raw_value in element.attrib.items():
                attribute_name = raw_name.rsplit("}", 1)[-1].lower()
                value = raw_value.strip().lower()
                if (
                    attribute_name in {"href", "src"}
                    or attribute_name.startswith("on")
                    or "url(" in value
                    or "javascript:" in value
                ):
                    raise ValueError("generated SVG preview contains an external reference")
            if local_name == "path":
                if not (element.get("d") or "").strip():
                    raise ValueError("generated SVG preview contains an empty path")
                entity_id = (element.get("id") or "").strip()
                layer_name = (element.get("data-dxf-layer") or "").strip()
                entity_type = (element.get("data-dxf-type") or "").strip()
                if not re.fullmatch(r"dxf-entity-[0-9]{4}", entity_id):
                    raise ValueError("generated SVG path has an invalid entity ID")
                if (
                    not layer_name
                    or len(layer_name) > 255
                    or any(ord(character) < 32 for character in layer_name)
                ):
                    raise ValueError("generated SVG path has invalid DXF layer metadata")
                if entity_type not in {
                    "LINE",
                    "LWPOLYLINE",
                    "CIRCLE",
                    "ARC",
                    "HATCH",
                    "TEXT",
                }:
                    raise ValueError("generated SVG path has invalid DXF type metadata")
                entity_ids.append(entity_id)
                entity_metadata.append({"id": entity_id, "layer": layer_name, "type": entity_type})
                layer_entity_counts[layer_name] += 1
                entity_type_counts[entity_type] += 1
                path_count += 1
            elif local_name == "rect":
                if (element.get("fill") or "").lower() != "#ffffff":
                    raise ValueError("generated SVG preview does not use a white background")
                background_count += 1
        if path_count == 0:
            raise ValueError("generated SVG preview contains no vector paths")
        expected_ids = [f"dxf-entity-{index:04d}" for index in range(1, path_count + 1)]
        if entity_ids != expected_ids:
            raise ValueError("generated SVG entity IDs are not unique and sequential")
        if expected_entity_metadata is not None and entity_metadata != expected_entity_metadata:
            raise ValueError("generated SVG entity metadata does not match DXF readback")
        if background_count != 1:
            raise ValueError("generated SVG preview must contain one white background")
        colors = {color.lower() for color in re.findall(r"#[0-9a-fA-F]{6}", text)}
        if colors != {"#000000", "#ffffff"}:
            raise ValueError("generated SVG preview does not use the black-on-white palette")
        entity_mapping_sha256 = self._content_sha256({"entities": entity_metadata})

        return {
            "verified": True,
            "entity_metadata_match": expected_entity_metadata is not None,
            "entity_mapping_sha256": entity_mapping_sha256,
            "path_count": path_count,
            "layer_count": len(layer_entity_counts),
            "layer_entity_counts": dict(sorted(layer_entity_counts.items())),
            "entity_type_counts": dict(sorted(entity_type_counts.items())),
            "background_color": "#ffffff",
            "foreground_color": "#000000",
            "color_policy": "black_on_white",
            "embedded_raster_count": 0,
            "external_reference_count": 0,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _write_and_verify_svg_preview(
        self,
        dxf_path: Path,
        staging_path: Path,
    ) -> dict[str, Any]:
        import ezdxf
        from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg

        readback = ezdxf.readfile(dxf_path)
        auditor = readback.audit()
        if auditor.has_errors:
            raise ValueError("cannot preview a DXF that failed readback audit")

        backend = svg.SVGBackend()
        preview_config = config.Configuration(
            background_policy=config.BackgroundPolicy.WHITE,
            color_policy=config.ColorPolicy.BLACK,
        )
        Frontend(
            RenderContext(readback),
            backend,
            config=preview_config,
        ).draw_layout(readback.modelspace())
        annotated_svg = self._annotate_svg_entities(
            backend.get_string(layout.Page(0, 0)),
            readback,
        )
        staging_path.write_text(
            annotated_svg,
            encoding="utf-8",
        )
        verification = self._verify_svg_preview(
            staging_path,
            expected_entity_metadata=self._svg_entity_metadata(readback),
        )
        verification["source_audit_errors"] = len(auditor.errors)
        verification["source_entity_count"] = len(readback.modelspace())
        return verification

    def _verify_generation_manifest(
        self,
        manifest_path: Path,
        *,
        expected_artifacts: list[dict[str, Any]],
        artifact_paths: list[Path],
        expected_recovery: dict[str, Any],
        expected_content_sha256: str,
        expected_mapping_sha256: str,
    ) -> dict[str, Any]:
        payload = manifest_path.read_bytes()
        if not payload or len(payload) > 1024 * 1024:
            raise ValueError("generated manifest has an invalid size")
        try:
            manifest = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("generated manifest is not valid UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise ValueError("generated manifest must contain one JSON object")
        if (
            manifest.get("schema_version") != "1.0"
            or manifest.get("bridge") != self.bridge_id
            or manifest.get("action") != "write_dxf"
            or manifest.get("state") != "completed"
        ):
            raise ValueError("generated manifest identity does not match this CAD batch")
        if (
            len(expected_artifacts) != 2
            or len(artifact_paths) != len(expected_artifacts)
            or manifest.get("artifact") != expected_artifacts[0]
            or manifest.get("artifacts") != expected_artifacts
        ):
            raise ValueError("generated manifest artifact digests do not match staged outputs")
        generation_id = self._generation_id(expected_artifacts)
        if manifest.get("generation_id") != generation_id:
            raise ValueError("generated manifest ID does not match CAD artifacts")
        if manifest.get("recovery") != expected_recovery:
            raise ValueError("generated manifest recovery evidence does not match this CAD batch")
        for artifact, artifact_path in zip(expected_artifacts, artifact_paths, strict=True):
            if (
                not artifact_path.is_file()
                or artifact_path.is_symlink()
                or artifact_path.stat().st_size != artifact.get("size_bytes")
                or self._sha256(artifact_path) != artifact.get("sha256")
            ):
                raise ValueError("CAD artifact bytes do not match generated manifest")

        verification = manifest.get("verification")
        if (
            not isinstance(verification, dict)
            or verification.get("content_match") is not True
            or verification.get("content_sha256") != expected_content_sha256
        ):
            raise ValueError("generated manifest DXF verification does not match readback")
        preview_verification = manifest.get("preview_verification")
        if (
            not isinstance(preview_verification, dict)
            or preview_verification.get("verified") is not True
            or preview_verification.get("entity_metadata_match") is not True
            or preview_verification.get("entity_mapping_sha256") != expected_mapping_sha256
        ):
            raise ValueError("generated manifest SVG verification does not match readback")

        return {
            "verified": True,
            "schema_version": "1.0",
            "generation_id": generation_id,
            "recovery": expected_recovery,
            "artifact_count": len(expected_artifacts),
            "artifact_digests_verified": True,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _entity_points(self, entity: dict[str, Any]) -> list[list[float]]:
        entity_type = entity.get("type")
        if entity_type == "line":
            return [entity["start"], entity["end"]]
        if entity_type == "polyline":
            return list(entity["points"])
        if entity_type == "circle":
            x, y = entity["center"]
            radius = entity["radius"]
            return [[x - radius, y - radius], [x + radius, y + radius]]
        if entity_type == "arc":
            x, y = entity["center"]
            radius = entity["radius"]
            start_angle = entity["start_angle"]
            end_angle = entity["end_angle"]
            if entity.get("clockwise", False):
                start_angle, end_angle = end_angle, start_angle
            sweep = (end_angle - start_angle) % 360.0
            angles = [start_angle, end_angle]
            angles.extend(
                angle
                for angle in (0.0, 90.0, 180.0, 270.0)
                if (angle - start_angle) % 360.0 <= sweep
            )
            return [
                [
                    x + radius * math.cos(math.radians(angle)),
                    y + radius * math.sin(math.radians(angle)),
                ]
                for angle in angles
            ]
        if entity_type == "hatch":
            return list(entity["points"])
        if entity_type == "rectangle":
            x = entity["x"]
            y = entity["y"]
            width = entity["width"]
            height = entity["height"]
            return [[x, y], [x + width, y + height]]
        if entity_type == "text":
            return [entity["position"]]
        return []

    def _plan_bbox(self, entities: list[dict[str, Any]]) -> dict[str, float] | None:
        points = [point for entity in entities for point in self._entity_points(entity)]
        if not points:
            return None
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        return {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)}

    def _empty_summary(self) -> dict[str, Any]:
        return {
            "units": None,
            "layer_count": 0,
            "layers": [],
            "entity_count": 0,
            "entity_types": {},
            "bbox": None,
        }

    def _summary_details(self, normalized: dict[str, Any]) -> dict[str, Any]:
        entities = normalized.get("entities", [])
        counts = Counter(entity.get("type", "unknown") for entity in entities)
        return {
            "units": normalized.get("units"),
            "layer_count": len(normalized.get("layers", [])),
            "layers": [layer["name"] for layer in normalized.get("layers", [])],
            "entity_count": len(entities),
            "entity_types": dict(sorted(counts.items())),
            "bbox": self._plan_bbox(entities),
        }

    def _manifest_for(
        self, normalized: dict[str, Any], output: Path, summary: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "bridge": self.bridge_id,
            "action": "write_dxf",
            "output": str(output),
            "plan_summary": summary,
            "normalized_plan": normalized,
        }

    def write_dxf(
        self,
        plan: Any,
        output: str | None = None,
        output_path: str | None = None,
        *,
        dry_run: bool = True,
        confirm_write: bool = False,
    ) -> dict[str, Any]:
        if output is None:
            output = output_path
        validation = self.validate_cad_plan(plan)
        if not validation["ok"]:
            return validation

        if not dry_run and not confirm_write:
            return self._result(
                ok=False,
                action="write_dxf",
                message="Real DXF write requires confirm_write=True.",
                details={"confirm_write": False},
                warnings=["Real writes must be explicitly confirmed."],
                next_steps=["Call with confirm_write=True (and dry_run=False) to proceed."],
            )

        normalized = validation["details"]["normalized_plan"]
        summary = self._summary_details(normalized)

        if output is None:
            output = "example.dxf"
        out_path = self._resolve_output_path(output)

        if not dry_run and (
            not self._output_is_allowed(out_path) or out_path.suffix.lower() != ".dxf"
        ):
            return self._result(
                ok=False,
                action="write_dxf",
                message=(
                    "Output must be one .dxf file inside the allowed sandbox (examples/cad/output)."
                ),
                details={"output_path": str(out_path)},
                warnings=["Only outputs under examples/cad/output are allowed for real writes."],
                next_steps=["Use a path inside the sandbox or dry_run=True."],
            )

        if dry_run:
            manifest = self._manifest_for(normalized, out_path, summary)
            return self._result(
                ok=True,
                action="write_dxf",
                message="DXF write (dry-run) prepared.",
                details={
                    "dry_run": True,
                    "output_path": str(out_path),
                    "manifest": manifest,
                    "summary": summary,
                    "confirm_write": confirm_write,
                },
                warnings=["dry_run=True."],
                next_steps=["Set dry_run=False with explicit confirmation to write."],
            )

        if not _ezdxf_available():
            return self._result(
                ok=False,
                action="write_dxf",
                message="ezdxf not available; cannot write DXF.",
                details={"ezdxf_available": False, "status": "unavailable"},
                warnings=["Install ezdxf to enable DXF export."],
                next_steps=["pip install ezdxf"],
            )

        manifest_path = out_path.with_suffix(".manifest.json")
        preview_path = out_path.with_suffix(".preview.svg")
        staging_dxf = out_path.with_name(f".{out_path.name}.staging")
        staging_preview = preview_path.with_name(f".{preview_path.name}.staging")
        staging_manifest = manifest_path.with_name(f".{manifest_path.name}.staging")
        final_paths = (out_path, preview_path, manifest_path)
        staging_paths = (staging_dxf, staging_preview, staging_manifest)
        final_present = [path for path in final_paths if path.exists() or path.is_symlink()]
        staging_present = [path for path in staging_paths if path.exists() or path.is_symlink()]
        recovered_staging_batch = False
        staging_is_recoverable = False
        staging_is_regular = False
        retry_after_seconds = 0
        if not final_present and staging_present:
            try:
                now = time.time()
                staging_is_regular = all(
                    path.is_file() and not path.is_symlink() for path in staging_present
                )
                if staging_is_regular:
                    modification_times = [path.stat().st_mtime for path in staging_present]
                    recovery_cutoff = now - self.STAGING_RECOVERY_MIN_AGE_SECONDS
                    staging_is_recoverable = all(
                        modified_at <= recovery_cutoff for modified_at in modification_times
                    )
                    if not staging_is_recoverable:
                        retry_after_seconds = max(
                            1,
                            math.ceil(
                                max(modification_times)
                                + self.STAGING_RECOVERY_MIN_AGE_SECONDS
                                - now
                            ),
                        )
            except OSError:
                staging_is_recoverable = False
        if not final_present and staging_present and staging_is_recoverable:
            try:
                for path in staging_present:
                    path.unlink()
                recovered_staging_batch = True
            except OSError:
                return self._result(
                    ok=False,
                    action="write_dxf",
                    message="Stale CAD staging files could not be cleaned safely.",
                    details={
                        "dry_run": False,
                        "status": "stale_staging_cleanup_failed",
                        "confirm_write": confirm_write,
                    },
                    warnings=["No final output was overwritten."],
                    next_steps=["Inspect the exact output batch and retry with a new filename."],
                )
        elif not final_present and staging_present and staging_is_regular:
            return self._result(
                ok=False,
                action="write_dxf",
                message="CAD generation may still be in progress; staging files were preserved.",
                details={
                    "dry_run": False,
                    "state": "in_progress",
                    "terminal": False,
                    "result_ready": False,
                    "status": "generation_in_progress",
                    "retry_after_seconds": retry_after_seconds,
                    "confirm_write": confirm_write,
                },
                warnings=["No active or recently updated staging file was removed."],
                next_steps=["Retry after the suggested delay or use a unique .dxf filename."],
            )
        elif final_present or staging_present:
            return self._result(
                ok=False,
                action="write_dxf",
                message="DXF output batch already exists; refusing to overwrite it.",
                details={
                    "dry_run": False,
                    "status": "output_batch_exists",
                    "confirm_write": confirm_write,
                },
                warnings=["Choose a new output name; existing files are preserved."],
                next_steps=["Use a unique .dxf filename inside examples/cad/output."],
            )
        recovery = {
            "stale_staging_recovered": recovered_staging_batch,
            "recovered_file_count": len(staging_present) if recovered_staging_batch else 0,
            "minimum_age_seconds": self.STAGING_RECOVERY_MIN_AGE_SECONDS,
        }

        out_path.parent.mkdir(parents=True, exist_ok=True)
        promoted_dxf = False
        promoted_preview = False
        promoted_manifest = False
        try:
            verification = self._write_and_audit_dxf(normalized, staging_dxf)
            dxf_artifact = {
                "role": "cad_drawing",
                "relative_path": out_path.relative_to(self.OUTPUT_ROOT.resolve()).as_posix(),
                "media_type": "image/vnd.dxf",
                "size_bytes": staging_dxf.stat().st_size,
                "sha256": self._sha256(staging_dxf),
            }
            preview_verification = self._write_and_verify_svg_preview(
                staging_dxf,
                staging_preview,
            )
            preview_artifact = {
                "role": "cad_preview",
                "relative_path": preview_path.relative_to(self.OUTPUT_ROOT.resolve()).as_posix(),
                "media_type": "image/svg+xml",
                "size_bytes": staging_preview.stat().st_size,
                "sha256": self._sha256(staging_preview),
            }
            generation_id = self._generation_id([dxf_artifact, preview_artifact])
            manifest = {
                "schema_version": "1.0",
                "bridge": self.bridge_id,
                "action": "write_dxf",
                "state": "completed",
                "generation_id": generation_id,
                "recovery": recovery,
                "artifact": dxf_artifact,
                "artifacts": [dxf_artifact, preview_artifact],
                "plan_summary": summary,
                "verification": verification,
                "preview_verification": preview_verification,
            }
            staging_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_verification = self._verify_generation_manifest(
                staging_manifest,
                expected_artifacts=[dxf_artifact, preview_artifact],
                artifact_paths=[staging_dxf, staging_preview],
                expected_recovery=recovery,
                expected_content_sha256=verification["content_sha256"],
                expected_mapping_sha256=preview_verification["entity_mapping_sha256"],
            )

            staging_dxf.replace(out_path)
            promoted_dxf = True
            staging_preview.replace(preview_path)
            promoted_preview = True
            staging_manifest.replace(manifest_path)
            promoted_manifest = True

            delivery_verification = self._verify_generation_manifest(
                manifest_path,
                expected_artifacts=[dxf_artifact, preview_artifact],
                artifact_paths=[out_path, preview_path],
                expected_recovery=recovery,
                expected_content_sha256=verification["content_sha256"],
                expected_mapping_sha256=preview_verification["entity_mapping_sha256"],
            )
            delivery_verification["promoted_artifacts_verified"] = True
            manifest_artifact = {
                "role": "generation_manifest",
                "relative_path": manifest_path.relative_to(self.OUTPUT_ROOT.resolve()).as_posix(),
                "media_type": "application/json",
                "size_bytes": manifest_path.stat().st_size,
                "sha256": self._sha256(manifest_path),
            }
            return self._result(
                ok=True,
                action="write_dxf",
                message="DXF generated, audited, and delivered with a verified SVG preview.",
                details={
                    "dry_run": False,
                    "state": "completed",
                    "terminal": True,
                    "result_ready": True,
                    "confirm_write": confirm_write,
                    "recovered_staging_batch": recovered_staging_batch,
                    "recovery": recovery,
                    "generation_id": generation_id,
                    "artifacts": [dxf_artifact, preview_artifact, manifest_artifact],
                    "verification": verification,
                    "preview_verification": preview_verification,
                    "manifest_verification": manifest_verification,
                    "delivery_verification": delivery_verification,
                    "summary": summary,
                },
            )
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            rollback_paths = [staging_dxf, staging_preview, staging_manifest]
            if promoted_manifest:
                rollback_paths.append(manifest_path)
            if promoted_preview:
                rollback_paths.append(preview_path)
            if promoted_dxf:
                rollback_paths.append(out_path)
            rollback = self._rollback_generation_paths(rollback_paths)
            rollback_complete = rollback["complete"]
            failure = {
                "type": self._generation_failure_type(error),
                "retry_safe": rollback_complete,
                "same_output_retry_allowed": rollback_complete,
            }
            return self._result(
                ok=False,
                action="write_dxf",
                message=(
                    "DXF generation failed; the current batch was rolled back."
                    if rollback_complete
                    else "DXF generation failed and rollback was incomplete."
                ),
                details={
                    "dry_run": False,
                    "state": "failed",
                    "terminal": True,
                    "result_ready": False,
                    "status": (
                        "generation_failed" if rollback_complete else "generation_rollback_failed"
                    ),
                    "failure": failure,
                    "rollback": rollback,
                    "confirm_write": confirm_write,
                },
                warnings=[
                    "No partial output from the current batch was accepted."
                    if rollback_complete
                    else "Some batch files could not be removed; no output was accepted."
                ],
                next_steps=[
                    "Resolve the reported failure type and retry the same output name."
                    if rollback_complete
                    else "Inspect the output sandbox and retry with a unique output name."
                ],
            )


# Back-compat module level functions for existing callers
_bridge = AutocadDxfBridge()


def status() -> dict[str, Any]:
    return _bridge.status()


def validate_cad_plan(plan: Any) -> dict[str, Any]:
    return _bridge.validate_cad_plan(plan)


def create_dxf_plan(prompt_or_spec: Any) -> dict[str, Any]:
    return _bridge.create_dxf_plan(prompt_or_spec)


def summarize_plan(plan: Any) -> dict[str, Any]:
    return _bridge.summarize_plan(plan)


def write_dxf(
    plan: Any,
    output: str | None = None,
    output_path: str | None = None,
    *,
    dry_run: bool = True,
    confirm_write: bool = False,
) -> dict[str, Any]:
    return _bridge.write_dxf(
        plan, output=output, output_path=output_path, dry_run=dry_run, confirm_write=confirm_write
    )


# Module level compat for tests and legacy code
OUTPUT_ROOT = AutocadDxfBridge.OUTPUT_ROOT
BRIDGE_ID = AutocadDxfBridge.BRIDGE_ID

_bridge_instance = _bridge


def _ezdxf_available() -> bool:
    return _bridge_instance._ezdxf_available()


def _output_is_allowed(output_path: Path) -> bool:
    return _bridge_instance._output_is_allowed(output_path)


def _entity_points(entity: dict[str, Any]) -> list[list[float]]:
    return _bridge_instance._entity_points(entity)


def _plan_bbox(entities: list[dict[str, Any]]) -> dict[str, float] | None:
    return _bridge_instance._plan_bbox(entities)


def _empty_summary() -> dict[str, Any]:
    return _bridge_instance._empty_summary()


def _summary_details(normalized: dict[str, Any]) -> dict[str, Any]:
    return _bridge_instance._summary_details(normalized)


def _manifest_for(
    normalized: dict[str, Any], output: Path, summary: dict[str, Any]
) -> dict[str, Any]:
    return _bridge_instance._manifest_for(normalized, output, summary)
