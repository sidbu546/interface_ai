"""Runtime conditions against the transfer capability.

    python3 scripts/demo_transfer_errors.py

Every case is a real replay of meridian.transfer.execute -- sign in, read the
balance, stage a transfer, have an operator commit it, read the updated balance
-- with one condition injected. No model is involved in any of it.

The operator is scripted here so the sweep runs unattended. Interactively it is
a person clicking Submit Transfer.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from understudy.artifact import CapabilityArtifact  # noqa: E402
from understudy.faults import FaultProfile  # noqa: E402
from understudy.policy import default_policy  # noqa: E402
from understudy.replay import ReplayEngine  # noqa: E402
from understudy.surface import WebSurface  # noqa: E402

APP = "http://127.0.0.1:8099"
ARTIFACT = ROOT / "artifacts" / "meridian.transfer.execute@1.2.0.json"
SECRETS = {"username": "jsmith", "password": "demo1234"}
BASE_INPUTS = {
    "account_id": "13566",
    "amount": "25",
    "from_account": "13344",
    "to_account": "13566",
}


def reset() -> None:
    urllib.request.urlopen(
        urllib.request.Request(f"{APP}/_reset", method="POST"), timeout=10
    )


def wrap(text: str, indent: str = " " * 16, width: int = 92) -> str:
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


def expire_session_once() -> None:
    """Make the app's next render behave as an idle timeout."""
    urllib.request.urlopen(
        urllib.request.Request(f"{APP}/_expire?count=1", method="POST"), timeout=10
    )


def run(
    label: str,
    inputs: dict | None = None,
    faults: FaultProfile | None = None,
    expire_before: str | None = None,
):
    reset()
    artifact = CapabilityArtifact.load(ARTIFACT)
    surface = WebSurface(headless=True, faults=faults or FaultProfile())

    def operator(_step_id: str, _instruction: str) -> bool:
        """Stands in for the person who commits the transfer."""
        for node in surface.observe().nodes:
            if (node.name or "").strip() == "Submit Transfer":
                surface.click(node.ref)
                return True
        return False

    def on_event(message: str) -> None:
        # Expire the session just as a given step begins, so the *next* request
        # the app serves is the inactivity screen. This is what a real timeout
        # looks like from replay's side: a step that was fine a moment ago
        # suddenly answers with the login page.
        if expire_before and message.startswith(f"[{expire_before}]"):
            expire_session_once()

    try:
        result = ReplayEngine(
            surface,
            default_policy(f"{APP}/"),
            secrets=SECRETS,
            operator=operator,
            on_event=on_event,
            evidence_dir=ROOT / "evidence" / "transfer-errors",
        ).run(artifact, {**BASE_INPUTS, **(inputs or {})})
    finally:
        try:
            surface.page.unroute_all(behavior="ignoreErrors")
        except Exception:
            pass
        surface.close()

    head = f"  {label:<24} exit={result.exit_code}  {result.status}"
    if result.outcome_code:
        head += f"  [{result.outcome_code}]"
    print(head)
    if result.status == "ok":
        for key, value in result.outputs.items():
            print(f"      {key:<26} {value}")
        return
    print(f"      step        {result.step or '-'}")
    print(f"      expected    {wrap(result.expected)}")
    print(f"      observed    {wrap(result.observed)}")
    if result.evidence_ref:
        print(f"      evidence    {result.evidence_ref}")


def main() -> int:
    if not ARTIFACT.exists():
        print(f"missing {ARTIFACT}. Compile a transfer discovery run first.")
        return 2
    try:
        urllib.request.urlopen(APP, timeout=5)
    except Exception:
        print("Target app not running:  python3 -m targets.legacy_bank.app")
        return 1

    print("\nBASELINE            the capability doing its job              exit 0")
    run("transfer completes")

    print("\nBUSINESS OUTCOMES   a legitimate answer for the caller        exit 2")
    run("validation error", {"amount": "abc"})
    run("insufficient funds", {"amount": "999999"})
    run("record not found", {"account_id": "99999"})
    run(
        "permission denial",
        faults=FaultProfile(
            rewrite_match="account.htm",
            rewrite_to=f"{APP}/meridian/account.htm?id=19001",
        ),
    )

    print("\nRECOVERABLE         handled deliberately, run completes       exit 0")
    run("unexpected dialog", faults=FaultProfile(dialog_match="overview.htm"))
    run("transient slowness", faults=FaultProfile(delay_match="transfer.htm", delay_ms=1500))
    run("session timeout", expire_before="s5_open_transfer_funds")

    print("\nHARD FAILURE        stops with a debuggable error             exit 4")
    run("app error (500)", faults=FaultProfile(status_match="transfer.htm", http_status=500))

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
