from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODE_SURFACES = (
    Path("README.md"),
    Path("docs/vectorization-modes.md"),
    Path("docs/COMMERCIAL_FEATURE_BOUNDARY_AUDIT.md"),
    Path("docs/PRODUCT_FACTS.md"),
    Path("apps/starbridge-site/src/site-content.mjs"),
    Path("apps/starbridge-desktop/src/pages/LicensePage.tsx"),
)


class PublicVectorModeDocumentationTests(unittest.TestCase):
    def test_public_surfaces_describe_four_modes_without_editable_99(self) -> None:
        for relative_path in PUBLIC_MODE_SURFACES:
            text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=str(relative_path)):
                self.assertNotIn("editable-99", text.lower())
                self.assertNotIn("五种公开矢量模式", text)

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 四种图片矢量化模式", readme)
        for mode in ("exact", "smart", "lightweight", "artisan"):
            self.assertIn(f"`{mode}`", readme)


if __name__ == "__main__":
    unittest.main()
