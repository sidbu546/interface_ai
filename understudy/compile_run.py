"""Compile a recorded discovery run into a saved capability artifact.

    python3 -m understudy.compile_run --run discovery-20260816-235309 \\
        --capability meridian.account.balance_and_last_activity
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .artifact import export_json_schema
from .compiler import compile_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a discovery run into an artifact.")
    parser.add_argument("--run", help="run id under evidence/ (default: most recent)")
    parser.add_argument("--capability", required=True, help="capability id, e.g. app.thing.action")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--product", default="meridian-core")
    parser.add_argument("--evidence", default="evidence")
    parser.add_argument("--out", default="artifacts")
    args = parser.parse_args(argv)

    evidence = Path(args.evidence)
    if args.run:
        run_dir = evidence / args.run
    else:
        runs = sorted(evidence.glob("discovery-*"), key=lambda p: p.name)
        if not runs:
            print(f"no discovery runs found under {evidence}/")
            return 2
        run_dir = runs[-1]

    journal = run_dir / "journal.jsonl"
    if not journal.exists():
        print(f"no journal at {journal}")
        return 2

    artifact = compile_run(
        journal,
        capability_id=args.capability,
        product=args.product,
        version=args.version,
    )
    path = artifact.save(Path(args.out))
    schema = export_json_schema(Path(args.out))

    print(f"compiled {run_dir.name}\n")
    print(artifact.summarise())
    print(f"\n  artifact   {path}")
    print(f"  schema     {schema}")

    blind = [
        step.id
        for step in artifact.steps
        if step.action.kind in {"click", "type", "select", "type_secret"}
        and step.target is None
    ]
    if blind:
        print(
            f"\n  WARNING: {len(blind)} acting step(s) have no target and will do "
            f"nothing on replay:\n    " + "\n    ".join(blind)
            + "\n  The recording predates target capture. Re-record:\n"
            "    python3 -m understudy.discover --no-vision"
        )

    if artifact.capability.approval.value == "draft":
        print(
            "\n  This artifact is DRAFT. Parameter names, risk classification and\n"
            "  outcome detectors are inferred and want a human's review before it\n"
            "  is approved for unattended replay."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
