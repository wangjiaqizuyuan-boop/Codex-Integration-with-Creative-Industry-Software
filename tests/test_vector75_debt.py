from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEBT_DOCUMENT = REPO_ROOT / "benchmark" / "vector60" / "vector75-debt.md"


class Vector75DebtContractTests(unittest.TestCase):
    def test_document_keeps_missing_evidence_unverified_and_bounded(self) -> None:
        text = DEBT_DOCUMENT.read_text(encoding="utf-8")
        normalized_text = " ".join(text.split())

        required_evidence_gaps = (
            "40 张 Vector60 验收图尚未提供",
            "尚未完成人工白缝复核",
            "pixel_match=true",
            "different_pixel_count=0",
            "maximum_channel_difference=0",
            "Node.js 与 SVGO 未打入 Python sidecar",
            "Windows 内部测试 workflow 尚未执行",
            "Microsoft Defender",
            "SmartScreen",
            "安装包签名均未验证",
        )
        required_boundaries = (
            "Vectorizer.AI",
            "diffvg 与 LIVE 仅允许离线、只读研究",
            "不增加第五种公开矢量化模式",
            "不使用 Illustrator Image Trace",
            "不扩大 macOS、Adobe、ComfyUI、AutoCAD、Blender、CapCut",
        )

        self.assertIn("状态：`planning_only`", text)
        for statement in required_evidence_gaps + required_boundaries:
            with self.subTest(statement=statement):
                self.assertIn(statement, text)
        self.assertIn("不表示 Vector60 已通过，也不表示 Vector75 已实现", normalized_text)
        self.assertNotIn("状态：`passed`", text)

    def test_each_roadmap_item_has_risk_validation_and_dependency_boundary(self) -> None:
        text = DEBT_DOCUMENT.read_text(encoding="utf-8")
        roadmap_rows = [
            line
            for line in text.splitlines()
            if line.startswith("| P0 |") or line.startswith("| P1 |") or line.startswith("| P2 |")
        ]

        self.assertGreaterEqual(len(roadmap_rows), 12)
        for row in roadmap_rows:
            with self.subTest(row=row):
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                self.assertEqual(len(cells), 5)
                self.assertTrue(all(cells))


if __name__ == "__main__":
    unittest.main()
