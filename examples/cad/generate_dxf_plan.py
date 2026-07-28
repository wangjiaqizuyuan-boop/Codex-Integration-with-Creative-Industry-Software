from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from starbridge_mcp.bridges.autocad_dxf import (  # noqa: E402
    summarize_plan,
    validate_cad_plan,
    write_dxf,
)
from starbridge_mcp.core.security import sanitize_result  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a public CAD plan and optionally generate an audited DXF batch."
    )
    parser.add_argument("--confirm-write", action="store_true")
    parser.add_argument("--output", default="safe_example.dxf")
    args = parser.parse_args()

    plan_path = Path(__file__).resolve().with_name("example_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = {
        "validate": validate_cad_plan(plan),
        "summary": summarize_plan(plan),
        "write": write_dxf(
            plan,
            args.output,
            dry_run=not args.confirm_write,
            confirm_write=args.confirm_write,
        ),
    }
    print(json.dumps(sanitize_result(result), ensure_ascii=False, indent=2))
    return 0 if result["write"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
