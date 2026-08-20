"""Demonstrate every runtime condition the replay contract distinguishes.

    python3 scripts/demo_errors.py

Each row is a real replay against the live target app, with no model in the
loop. Faults are injected beneath the surface driver, so the engine cannot tell
an injected condition from a genuine one.

Requires the target app:
    python3 -m targets.legacy_bank.app
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = "http://127.0.0.1:8099"
READ_PATH = "artifacts/meridian.account.balance_and_last_activity@1.0.0.json"
TRANSFER = "artifacts/meridian.transfer.execute@1.2.0.json"

EXIT_MEANING = {0: "ok", 2: "business outcome", 3: "needs human", 4: "failed"}


def replay(artifact: str, *extra: str, secrets: bool = True) -> tuple[int, dict]:
    command = [
        sys.executable, "-m", "understudy.replay_run",
        "--artifact", artifact, "--json", *extra,
    ]
    if secrets:
        command.append("--unattended")
    finished = subprocess.run(
        command,
        cwd=ROOT,
        env=dict(os.environ, BANK_USER="jsmith", BANK_PASSWORD="demo1234"),
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        return finished.returncode, json.loads(finished.stdout)
    except json.JSONDecodeError:
        return finished.returncode, {"status": "?", "reason": finished.stderr[:120]}


def wrap(text: str, indent: str = " " * 16, width: int = 96) -> str:
    words, lines, current = (text or "").split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return ("\n" + indent).join(lines) or "-"


def row(label: str, artifact: str, *extra: str, secrets: bool = True) -> None:
    """Every non-success result reports the same three things."""
    code, result = replay(artifact, *extra, secrets=secrets)
    status = result.get("status", "?")
    outcome = result.get("outcome_code")
    head = f"  {label:<21} exit={code}  {status}"
    if outcome:
        head += f"  [{outcome}]"
    print(head)
    if status == "ok":
        return
    print(f"    step        {result.get('step') or '-'}")
    print(f"    expected    {wrap(result.get('expected'))}")
    print(f"    observed    {wrap(result.get('observed'))}")
    if result.get("evidence_ref"):
        print(f"    evidence    {result['evidence_ref']}")


def main() -> int:
    try:
        urllib.request.urlopen(APP, timeout=5)
    except urllib.error.URLError:
        print("The target app is not running. In another terminal:\n"
              "    python3 -m targets.legacy_bank.app")
        return 1

    urllib.request.urlopen(
        urllib.request.Request(f"{APP}/_reset", method="POST"), timeout=10
    )

    print("\nBUSINESS OUTCOMES   a legitimate answer the caller must handle   exit 2")
    row("record not found", READ_PATH, "--input", "account_id=99999")
    row("no transactions", READ_PATH, "--input", "account_id=13901")
    row("permission denial", READ_PATH, "--input", "account_id=13566",
        "--fault-rewrite", f"account.htm={APP}/meridian/account.htm?id=19001")
    row("validation error", TRANSFER, "--input", "account_id=13566",
        "--input", "amount=abc", "--input", "from_account=13344",
        "--input", "to_account=13566")
    row("insufficient funds", TRANSFER, "--input", "account_id=13566",
        "--input", "amount=999999", "--input", "from_account=13344",
        "--input", "to_account=13566")

    print("\nRECOVERABLE         handled deliberately, the run completes      exit 0")
    row("unexpected dialog", READ_PATH, "--input", "account_id=13566",
        "--fault-dialog", "overview.htm")
    row("transient slowness", READ_PATH, "--input", "account_id=13566",
        "--fault-delay", "account.htm:1500")

    print("\nHARD FAILURE        stops with a debuggable error                exit 4")
    row("app error (500)", READ_PATH, "--input", "account_id=13566",
        "--fault-status", "account.htm:500")

    print("\nNEEDS HUMAN         an operator is required to continue          exit 3")
    row("credential step", READ_PATH, "--input", "account_id=13566", secrets=False)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
