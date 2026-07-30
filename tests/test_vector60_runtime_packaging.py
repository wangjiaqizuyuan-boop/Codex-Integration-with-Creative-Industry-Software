from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 uses the project dependency
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNTIME_VERSIONS = {
    "vtracer": "0.6.15",
    "skia-pathops": "0.9.2",
    "svgpathtools": "1.7.2",
}


class Vector60RuntimePackagingTest(unittest.TestCase):
    def test_vector60_extra_pins_production_python_components(self) -> None:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        dependencies = project["optional-dependencies"]["vector60"]
        for name, version in PYTHON_RUNTIME_VERSIONS.items():
            self.assertIn(f"{name}=={version}", dependencies)

    def test_root_lockfile_pins_svgo_with_integrity(self) -> None:
        package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        lockfile = json.loads((REPO_ROOT / "package-lock.json").read_text(encoding="utf-8"))

        self.assertEqual("4.0.2", package["dependencies"]["svgo"])
        locked_svgo = lockfile["packages"]["node_modules/svgo"]
        self.assertEqual("4.0.2", locked_svgo["version"])
        self.assertRegex(locked_svgo["integrity"], r"^sha512-")

    def test_quickstart_installs_vector60_and_locked_node_dependencies(self) -> None:
        quickstart = (REPO_ROOT / "scripts" / "quickstart.ps1").read_text(encoding="utf-8")

        self.assertIn('$extras.Add("vector60")', quickstart)
        self.assertIn('Join-Path $repoRoot "package-lock.json"', quickstart)
        self.assertRegex(
            quickstart,
            r'(?s)"install root Node dependencies".*?"ci", "--prefix", \$repoRoot',
        )
        self.assertNotIn("npx", quickstart.lower())

    def test_legacy_bootstrap_installs_vector60_extra(self) -> None:
        setup = (REPO_ROOT / "scripts" / "setup_starbridge.ps1").read_text(encoding="utf-8")
        self.assertIn('"-e", ".[vector60]"', setup)

    def test_sidecar_pins_and_collects_python_runtime_only(self) -> None:
        scripts = REPO_ROOT / "apps" / "starbridge-desktop" / "scripts"
        requirements = (scripts / "requirements-sidecar-build.txt").read_text(encoding="utf-8")
        spec = (scripts / "starbridge-sidecar.spec").read_text(encoding="utf-8")
        build = (scripts / "Build-Sidecar.ps1").read_text(encoding="utf-8")
        sidecar_entry = (scripts / "sidecar_entry.py").read_text(encoding="utf-8")
        sidecar_test = (scripts / "Test-Sidecar.ps1").read_text(encoding="utf-8")

        self.assertIn("jsonschema==4.26.0", requirements)
        notices = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("## jsonschema", notices)
        self.assertIn("Version: `4.26.0`", notices)
        self.assertIn("a7277432b0f7bcd0551f6e589d30457017125df4", notices)
        self.assertIn("runtime infrastructure", notices)
        for name, version in PYTHON_RUNTIME_VERSIONS.items():
            self.assertIn(f"{name}=={version}", requirements)
        for import_name in ("vtracer", "pathops", "svgpathtools"):
            self.assertIn(f'"{import_name}"', spec)
        self.assertIn("copy_metadata(distribution)", spec)
        self.assertIn("collect_data_files(", spec)
        self.assertIn('"model_contracts"', spec)
        self.assertIn('"model_contracts.schemas"', spec)
        self.assertIn('includes=["schemas/*.json"]', spec)
        self.assertIn("vector60_python_runtime_included = $true", build)
        self.assertIn("vector60_node_runtime_included = $false", build)
        self.assertIn("vector60_svgo_runtime_included = $false", build)
        self.assertIn("--vector60-runtime-check", sidecar_entry)
        self.assertIn("--vector60-runtime-check", build)
        self.assertIn("--vector60-runtime-check", sidecar_test)
        self.assertIn("vector60_node_runtime_included = $false", sidecar_test)
        self.assertIn("vector60_svgo_runtime_included = $false", sidecar_test)
        self.assertIn("missing packaged Python module", sidecar_test)
        self.assertIn("Get-SafePythonFailureKind", sidecar_test)
        self.assertIn("MCP connector exited before responding", sidecar_test)
        self.assertNotIn("throw $startupError", sidecar_test)
        self.assertNotIn("throw $mcpStartupError", sidecar_test)
        self.assertNotRegex(build.lower(), r"\bnpx\b")

    def test_ci_runs_real_cross_platform_runtime_smoke(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("test-vector60-runtime:", workflow)
        for version in ("3.10", "3.11", "3.12", "3.13"):
            self.assertIn(f'python-version: "{version}"', workflow)
        self.assertIn("os: windows-latest", workflow)
        self.assertIn('pip install -e ".[vector60]"', workflow)
        self.assertIn("npm ci --ignore-scripts", workflow)
        self.assertIn("svgo --version", workflow)
        self.assertNotRegex(workflow.lower(), re.compile(r"\bnpx\b"))

    def test_internal_windows_build_gate_is_unsigned_and_not_a_release(self) -> None:
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "vector60-internal-windows.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("pull_request:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("INTERNAL-UNSIGNED-NOT-FOR-RELEASE", workflow)
        self.assertIn("Test-Sidecar.ps1", workflow)
        self.assertIn("--vector60-runtime-check", workflow)
        self.assertIn('$signature.Status -ne "NotSigned"', workflow)
        self.assertIn("node_runtime_included = $false", workflow)
        self.assertIn("svgo_runtime_included = $false", workflow)
        self.assertIn("clean_machine_install_validated = $false", workflow)
        self.assertIn("retention-days: 7", workflow)
        self.assertRegex(
            workflow,
            re.compile(
                r"(?s)- name: Prepare Tauri test resources.*?"
                r"starbridge-sidecar-x86_64-pc-windows-msvc\.exe.*?"
                r"cargo test --manifest-path.*?"
                r"Build-Sidecar\.ps1"
            ),
        )
        self.assertRegex(
            workflow,
            re.compile(
                r"(?s)- name: Upload short-lived internal test artifact\s+"
                r"if: github\.event_name == 'workflow_dispatch'\s+"
                r"uses: actions/upload-artifact@v7"
            ),
        )
        self.assertNotIn("softprops/action-gh-release", workflow)
        self.assertNotIn("TAURI_SIGNING_PRIVATE_KEY", workflow)
        self.assertNotIn("WINDOWS_SIGNING_CERTIFICATE", workflow)


if __name__ == "__main__":
    unittest.main()
