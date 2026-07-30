from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "examples" / "photoshop_bridge" / "scripts" / "export_demo_preview.ps1"


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell"),
    "Photoshop PowerShell bridge tests require Windows PowerShell.",
)
class PhotoshopDemoExportScriptTests(unittest.TestCase):
    def test_dry_run_returns_two_sandbox_exports_as_json(self) -> None:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["confirm_export"])
        self.assertEqual(
            [
                "examples/output/photoshop/starbridge_ps_demo.png",
                "examples/output/photoshop/starbridge_ps_demo.jpg",
            ],
            result["exported_files"],
        )
        self.assertNotIn(str(Path.home()), completed.stdout)


if __name__ == "__main__":
    unittest.main()
