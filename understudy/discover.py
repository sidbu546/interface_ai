"""CLI entry point for a discovery run.

    python3 -m understudy.discover \\
        --goal "Log in as jsmith, open account 13566, and report its balance \\
                and the date and amount of the most recent transaction." \\
        --target http://127.0.0.1:8099/meridian/index.htm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .agent import MODEL, DiscoveryAgent
from .journal import Journal
from .policy import default_policy
from .surface import WebSurface

DEFAULT_GOAL = (
    "Log in, open account 13566, and report its current balance and the date "
    "and amount of the most recent posted transaction."
)
DEFAULT_TARGET = "http://127.0.0.1:8099/meridian/index.htm"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an LLM-driven discovery pass.")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--no-vision", action="store_true", help="AX tree only")
    parser.add_argument("--evidence", default="evidence")
    parser.add_argument(
        "--credentials",
        choices=["env", "human"],
        default="env",
        help=(
            "env: the driver injects test credentials the model never sees. "
            "human: automation pauses and you sign in on the live session, so "
            "the system never holds the credential at all."
        ),
    )
    args = parser.parse_args(argv)

    # A person cannot type into a browser they cannot see.
    if args.credentials == "human" and not args.headed:
        print("[discovery] --credentials human requires a visible browser; enabling --headed")
        args.headed = True

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. See scripts/check_model_access.py")
        return 2

    run_id = time.strftime("discovery-%Y%m%d-%H%M%S")
    journal = Journal(run_id=run_id, directory=Path(args.evidence) / run_id)
    policy = default_policy(args.target)
    policy.max_steps = args.max_steps

    # In "human" mode this stays empty: the system holds no credential at all.
    secrets = (
        {}
        if args.credentials == "human"
        else {
            "username": os.environ.get("BANK_USER", "jsmith"),
            "password": os.environ.get("BANK_PASSWORD", "demo1234"),
        }
    )

    print(f"[discovery] run    {run_id}")
    print(f"[discovery] goal   {args.goal}")
    print(f"[discovery] target {args.target}")
    print(f"[discovery] model  {MODEL}")
    print(f"[discovery] creds  {args.credentials}")

    journal.run_started(args.goal, args.target, MODEL)

    surface = WebSurface(headless=not args.headed)
    try:
        policy.check_url(args.target)
        surface.navigate(args.target)
        agent = DiscoveryAgent(
            surface=surface,
            policy=policy,
            journal=journal,
            secrets=secrets,
            use_vision=not args.no_vision,
            credential_mode=args.credentials,
        )
        result = agent.run(args.goal)
    finally:
        surface.close()

    calls, tokens_in, tokens_out = journal.totals
    print(f"\n[discovery] status {result['status']}")
    if result.get("outputs"):
        print("[discovery] outputs")
        for key, value in result["outputs"].items():
            print(f"              {key}: {value}")
    print(
        f"[discovery] {calls} model calls · {tokens_in:,} in / {tokens_out:,} out tokens"
    )
    print(f"[discovery] evidence {journal.path}")

    return 0 if result["status"] in {"success", "business_outcome"} else 1


if __name__ == "__main__":
    sys.exit(main())
