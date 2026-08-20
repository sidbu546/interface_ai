"""Select a capability and invoke it, with no model anywhere in the process.

    python3 scripts/route_invoke.py --capability meridian.account.balance_and_last_activity \\
        --input account_id=13566
    python3 scripts/route_invoke.py --intent read_balance --input account_id=13566
    python3 scripts/route_invoke.py --list

An LLM is only required when the request arrives as free-form English. A caller
that already knows what it wants -- a workflow engine, a queue consumer, a
scheduled job, a UI where a person picked from a list -- selects by name or by a
declared intent tag, and never contacts a model at all.

This is the same catalog and the same replay engine the AI agent uses. Only the
*selection* step differs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from understudy.catalog import Catalog, tool_name_for  # noqa: E402

# A deterministic routing table: intent tag -> capability id. In a real system
# this lives beside the capability (or in the artifact's metadata) and is
# reviewed like any other config. No inference, no ambiguity, no model.
ROUTES = {
    "read_balance": "meridian.account.balance_and_last_activity",
    "transfer_funds": "meridian.transfer.execute",
}


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs or []:
        name, _, value = pair.partition("=")
        if not _:
            raise SystemExit(f"--input expects name=value, got {pair!r}")
        values[name.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Invoke a capability without using a model to choose it."
    )
    parser.add_argument("--capability", help="exact capability id")
    parser.add_argument("--intent", choices=sorted(ROUTES), help="a declared intent tag")
    parser.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--list", action="store_true", help="show the catalog and exit")
    parser.add_argument("--unattended", action="store_true",
                        help="take credentials from the environment instead of "
                             "asking. For scripts and CI only.")
    args = parser.parse_args()

    catalog = Catalog(ROOT / "artifacts")

    if args.list or not (args.capability or args.intent):
        print("CATALOG (selection needs no model)")
        for name, artifact in catalog.capabilities.items():
            signature = artifact.tool_signature()
            needs = ", ".join(f"{p}" for p in signature["input_schema"]["required"])
            print(f"  {name}")
            print(f"      inputs   {needs or '(none)'}")
            print(f"      operator {signature['requires_operator']}")
        print("\nINTENT ROUTES")
        for intent, capability in ROUTES.items():
            print(f"  {intent:<16} -> {capability}")
        return 0

    capability_id = args.capability or ROUTES[args.intent]
    name = tool_name_for(capability_id)
    if name not in catalog.capabilities:
        print(f"no capability {capability_id!r}. Available: {sorted(catalog.capabilities)}")
        return 2

    how = f"--capability {capability_id}" if args.capability else f"--intent {args.intent}"
    print(f"SELECTED  {capability_id}\n  by       {how}  (lookup, no model)")

    params = parse_inputs(args.input)
    print(f"  inputs   {params}\n")

    result = catalog.invoke(
        name,
        params,
        secrets={} if not args.unattended else {
            "username": os.environ.get("BANK_USER", ""),
            "password": os.environ.get("BANK_PASSWORD", ""),
        },
        headless=args.unattended,
        operator=None if args.unattended else _terminal_operator,
        evidence_dir=ROOT / "evidence" / "route-invoke",
    )

    print("RESULT")
    print(f"  status   {result['status']}")
    if result.get("outcome_code"):
        print(f"  outcome  {result['outcome_code']}")
    for key, value in (result.get("outputs") or {}).items():
        print(f"  {key:<26} {value}")
    for field in ("step", "expected", "observed", "evidence_ref"):
        if result.get(field) and result["status"] != "ok":
            print(f"  {field:<26} {result[field]}")
    print(f"\n  model calls anywhere in this process: 0")

    return {"ok": 0, "business_outcome": 2, "needs_human": 3, "failed": 4}[result["status"]]


def _terminal_operator(step_id: str, instruction: str) -> bool:
    print(f"\n  PAUSED at {step_id}: {instruction}")
    print("  Sign in in the browser window, then come back.")
    try:
        input("  Press Enter when done... ")
        return True
    except EOFError:
        return False


if __name__ == "__main__":
    sys.exit(main())
