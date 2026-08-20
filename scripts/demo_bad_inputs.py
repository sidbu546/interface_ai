"""Invoke the transfer capability with deliberately wrong inputs.

    python3 scripts/demo_bad_inputs.py

Every case is a real replay of meridian.transfer.execute with one bad argument.
No model is involved. Each non-success result reports the same three things the
brief asks for: what step, what was expected, what was observed.

The operator is scripted so the sweep runs unattended; interactively it is a
person clicking Submit Transfer.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from understudy.artifact import CapabilityArtifact  # noqa: E402
from understudy.policy import default_policy  # noqa: E402
from understudy.replay import ReplayEngine  # noqa: E402
from understudy.surface import WebSurface  # noqa: E402

APP = "http://127.0.0.1:8099"
ARTIFACT = ROOT / "artifacts" / "meridian.transfer.execute@1.2.0.json"
GOOD = {
    "account_id": "13566",
    "amount": "25",
    "from_account": "13344",
    "to_account": "13566",
}

CASES: list[tuple[str, dict]] = [
    ("amount is not a number", {"amount": "abc"}),
    ("amount is negative", {"amount": "-50"}),
    ("amount exceeds balance", {"amount": "999999"}),
    ("amount is empty", {"amount": ""}),
    ("account does not exist", {"account_id": "99999"}),
    ("account belongs to someone else", {"account_id": "19001"}),
    ("source and destination are the same", {"from_account": "13566"}),
]


def wrap(text: str, indent: str = " " * 16, width: int = 90) -> str:
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


def run(label: str, bad: dict) -> None:
    urllib.request.urlopen(
        urllib.request.Request(f"{APP}/_reset", method="POST"), timeout=10
    )
    artifact = CapabilityArtifact.load(ARTIFACT)
    surface = WebSurface(headless=True)

    def operator(_step_id: str, _instruction: str) -> bool:
        for node in surface.observe().nodes:
            if (node.name or "").strip() == "Submit Transfer":
                surface.click(node.ref)
                return True
        return False

    try:
        result = ReplayEngine(
            surface,
            default_policy(f"{APP}/"),
            secrets={"username": "jsmith", "password": "demo1234"},
            operator=operator,
            on_event=lambda _m: None,
            evidence_dir=ROOT / "evidence" / "bad-inputs",
        ).run(artifact, {**GOOD, **bad})
    finally:
        surface.close()

    changed = ", ".join(f"{k}={v!r}" for k, v in bad.items())
    print(f"\n  {label}")
    print(f"      input       {changed}")
    print(
        f"      result      exit={result.exit_code}  {result.status}"
        + (f"  [{result.outcome_code}]" if result.outcome_code else "")
    )
    if result.status == "ok":
        for key, value in result.outputs.items():
            print(f"      {key:<12}{value}")
        return
    print(f"      step        {result.step or '-'}")
    print(f"      expected    {wrap(result.expected)}")
    print(f"      observed    {wrap(result.observed)}")


def main() -> int:
    if not ARTIFACT.exists():
        print(f"missing {ARTIFACT.name} -- compile a transfer discovery run first")
        return 2
    try:
        urllib.request.urlopen(APP, timeout=5)
    except Exception:
        print("Target app not running:  python3 -m targets.legacy_bank.app")
        return 1

    print("\nGOOD INPUT   the capability doing its job")
    run("everything valid", {})

    print("\nBAD INPUT    each one a legitimate answer, not a crash")
    for label, bad in CASES:
        run(label, bad)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
