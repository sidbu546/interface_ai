"""Replay a saved capability. This is the path an AI agent triggers in production.

    python3 -m understudy.replay_run \\
        --artifact artifacts/meridian.account.balance_and_last_activity@1.0.0.json \\
        --input account_id=13566

Exit codes are part of the contract:
    0  ok                 the capability succeeded, outputs returned
    2  business_outcome   a legitimate non-success answer (no such account)
    3  needs_human        an operator is required to continue
    4  failed             a defect: which step, what was expected, what was seen
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .artifact import CapabilityArtifact
from .faults import FaultProfile
from .policy import default_policy
from .replay import ReplayEngine
from .surface import WebSurface


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--input expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        values[name.strip()] = value.strip()
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a capability artifact.")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--base-url", default="http://127.0.0.1:8099/")
    parser.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "do not ask for credentials; take them from BANK_USER/BANK_PASSWORD "
            "instead. For scripts and CI only -- an interactive run should let a "
            "person type their own credentials."
        ),
    )
    parser.add_argument("--headless", action="store_true",
                        help="hide the browser (implies --unattended)")
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    parser.add_argument("--evidence", default="evidence",
                        help="where to write a screenshot + context on failure")
    parser.add_argument("--fault-delay", metavar="MATCH:MS",
                        help="stall matching requests, e.g. account.htm:1500")
    parser.add_argument("--fault-status", metavar="MATCH:CODE",
                        help="return an HTTP status, e.g. account.htm:500")
    parser.add_argument("--fault-rewrite", metavar="MATCH=URL",
                        help="serve another URL's response, e.g. "
                             "account.htm=http://127.0.0.1:8099/meridian/account.htm?id=19001")
    parser.add_argument("--fault-dialog", metavar="MATCH",
                        help="inject an unexpected interstitial, e.g. overview.htm")
    args = parser.parse_args(argv)

    quiet = args.json
    artifact = CapabilityArtifact.load(Path(args.artifact))
    params = parse_inputs(args.input)

    attended = not (args.unattended or args.headless)
    if artifact.requires_operator and not attended:
        # stderr, not stdout: in --json mode a caller parses stdout, and a
        # friendly note in that stream is a broken contract.
        print(
            f"note: running unattended; credentials for "
            f"{', '.join(artifact.operator_steps)} will come from the environment.",
            file=sys.stderr,
        )

    faults = FaultProfile()
    if args.fault_delay:
        match, _, ms = args.fault_delay.rpartition(":")
        faults.delay_match, faults.delay_ms = match, int(ms)
    if args.fault_status:
        match, _, code = args.fault_status.rpartition(":")
        faults.status_match, faults.http_status = match, int(code)
    if args.fault_dialog:
        faults.dialog_match = args.fault_dialog
    if args.fault_rewrite:
        match, _, destination = args.fault_rewrite.partition("=")
        faults.rewrite_match, faults.rewrite_to = match, destination
    if faults.active and not quiet:
        print(f"faults    {faults.describe()}")

    surface = WebSurface(headless=not attended, faults=faults)
    try:
        engine = ReplayEngine(
            surface=surface,
            policy=default_policy(args.base_url),
            # Attended: the system holds no credential at all -- the person at
            # the keyboard types it into the live session, and nothing is captured.
            secrets=(
                {}
                if attended
                else {
                    "username": os.environ.get("BANK_USER", ""),
                    "password": os.environ.get("BANK_PASSWORD", ""),
                }
            ),
            operator=_terminal_operator if attended else None,
            on_event=(lambda _m: None) if quiet else print,
            evidence_dir=Path(args.evidence)
            / f"replay-{time.strftime('%Y%m%d-%H%M%S')}",
        )
        if not quiet:
            print(f"replaying {artifact.capability.id}@{artifact.capability.version}")
            print(f"inputs    {params or '(none)'}\n")
        result = engine.run(artifact, params)
    finally:
        try:
            surface.page.unroute_all(behavior="ignoreErrors")
        except Exception:
            pass
        surface.close()

    if quiet:
        print(json.dumps(
            {
                "status": result.status,
                "outputs": result.outputs,
                "outcome_code": result.outcome_code,
                "reason": result.reason,
                "step": result.step,
                "expected": result.expected,
                "observed": result.observed,
                "evidence_ref": result.evidence_ref,
                "steps_run": result.steps_run,
                "duration_ms": result.duration_ms,
                "model_calls": 0,
                "fallback_rate": result.fallback_rate,
            },
            indent=2,
        ))
    else:
        print()
        print(result.render())

    return result.exit_code


def _terminal_operator(step_id: str, instruction: str) -> bool:
    print("\n" + "=" * 66)
    print("  REPLAY PAUSED - control handed to you")
    print("=" * 66)
    print(f"  step: {step_id}")
    print(f"  {instruction}")
    print("\n  Evidence capture is suspended until you hand control back.")
    print("=" * 66)
    try:
        input("  Press Enter when done (or Ctrl-C to abandon)... ")
        return True
    except EOFError:
        return False


if __name__ == "__main__":
    sys.exit(main())
