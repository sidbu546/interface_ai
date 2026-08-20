"""One command that checks the whole system and tells you how to fix what's broken.

    python3 scripts/doctor.py

Checks run in dependency order -- interpreter, packages, browser, credentials,
target app, surface, artifacts, replay, tests -- because a failure early on
makes everything after it fail for uninteresting reasons. When something is
wrong the check prints the exact command that fixes it, rather than a stack
trace you have to interpret.

Exit code is 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Do this once, at import: a check that fails because the package is not on the
# path would otherwise report a misleading cause, which is worse than no check.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
APP_URL = "http://127.0.0.1:8099"
ARTIFACT = ROOT / "artifacts" / "meridian.account.balance_and_last_activity@1.0.0.json"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


class Result:
    def __init__(self, ok: bool, detail: str = "", fix: str = "", warn: bool = False):
        self.ok, self.detail, self.fix, self.warn = ok, detail, fix, warn


def ok(detail: str = "") -> Result:
    return Result(True, detail)


def fail(detail: str, fix: str) -> Result:
    return Result(False, detail, fix)


def warn(detail: str, fix: str) -> Result:
    return Result(True, detail, fix, warn=True)


# --------------------------------------------------------------------- checks


def check_python() -> Result:
    major, minor = sys.version_info[:2]
    where = "venv" if sys.prefix != sys.base_prefix else "system/conda"
    if (major, minor) < (3, 10):
        return fail(
            f"Python {major}.{minor} ({where})",
            "This project needs Python 3.10+. Activate the venv:\n"
            "    source .venv/bin/activate",
        )
    if where != "venv" and (ROOT / ".venv").exists():
        return warn(
            f"Python {major}.{minor} ({where}) but a .venv exists",
            "You are not in the project venv, so packages may differ:\n"
            "    source .venv/bin/activate",
        )
    return ok(f"Python {major}.{minor} ({where})")


def check_packages() -> Result:
    missing = []
    for module in ("flask", "pytest", "playwright", "anthropic", "pydantic"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        return fail(
            f"missing: {', '.join(missing)}",
            "Install them into the active environment:\n"
            "    python3 -m pip install -r requirements.txt",
        )
    return ok("flask, pytest, playwright, anthropic, pydantic")


def check_browser() -> Result:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return fail("playwright not importable", "python3 -m pip install playwright")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            version = browser.version
            browser.close()
        return ok(f"Chromium {version}")
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            return fail(
                "Chromium is not installed for this Playwright version",
                "    python3 -m playwright install chromium",
            )
        return fail(f"cannot launch Chromium: {type(exc).__name__}", message[:200])


def check_api_key() -> Result:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fail(
            "ANTHROPIC_API_KEY is not set in this shell",
            "Needed only for discovery runs (replay never calls a model).\n"
            "    echo 'export ANTHROPIC_API_KEY=\"sk-ant-...\"' >> ~/.zshenv\n"
            "    source ~/.zshenv\n"
            "  Note: ~/.zshrc is read by interactive shells only, so scripts and\n"
            "  tools will not see a key defined there. ~/.zshenv is read by all.",
        )
    # A key in .zshrc only works interactively -- a classic silent failure.
    try:
        seen = subprocess.run(
            ["zsh", "-c", "echo ${#ANTHROPIC_API_KEY}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if seen == "0":
            return warn(
                "set here, but invisible to non-interactive shells",
                "Move it from ~/.zshrc to ~/.zshenv so scripts can see it:\n"
                "    grep ANTHROPIC_API_KEY ~/.zshrc >> ~/.zshenv",
            )
    except Exception:
        pass
    return ok(f"set ({len(os.environ['ANTHROPIC_API_KEY'])} chars)")


def check_port() -> Result:
    with socket.socket() as probe:
        probe.settimeout(2)
        if probe.connect_ex(("127.0.0.1", 8099)) != 0:
            return fail(
                "nothing is listening on 127.0.0.1:8099",
                "Start the target app in another terminal:\n"
                "    python3 -m targets.legacy_bank.app",
            )
    return ok("something is listening on 8099")


def check_app() -> Result:
    try:
        for path in ("/", "/meridian/index.htm", "/cascade/index.htm"):
            with urllib.request.urlopen(APP_URL + path, timeout=8) as response:
                if response.status != 200:
                    return fail(
                        f"{path} returned HTTP {response.status}",
                        "Restart the app:\n    python3 -m targets.legacy_bank.app",
                    )
        return ok("both tenants serving (meridian, cascade)")
    except urllib.error.URLError as exc:
        return fail(
            f"cannot reach the app: {exc.reason}",
            "Start it:\n    python3 -m targets.legacy_bank.app",
        )


def check_surface() -> Result:
    """The load-bearing assumption: the AX tree is usable on this target."""
    try:
        from understudy.surface import WebSurface

        surface = WebSurface(headless=True)
        try:
            surface.navigate(f"{APP_URL}/meridian/index.htm")
            snapshot = surface.observe()
            unnamed = [
                n for n in snapshot.nodes if n.role == "textbox" and not n.name and n.anchor
            ]
            if len(unnamed) < 2:
                return fail(
                    f"expected 2 unlabelled textboxes with anchors, found {len(unnamed)}",
                    "The accessibility tree is not resolving as expected. Check that\n"
                    "  the login page still renders, then re-run:\n"
                    "    python3 -m pytest tests/test_target_app.py -q",
                )
        finally:
            surface.close()
        return ok(f"{len(snapshot.nodes)} nodes; anchors resolving on unnamed inputs")
    except Exception as exc:
        return fail(
            f"surface failed: {type(exc).__name__}: {exc}",
            "Usually a browser problem. Try:\n    python3 -m playwright install chromium",
        )


def check_artifact() -> Result:
    if not ARTIFACT.exists():
        return fail(
            f"no artifact at {ARTIFACT.relative_to(ROOT)}",
            "Record one, then compile it:\n"
            "    python3 -m understudy.discover\n"
            "    python3 -m understudy.compile_run "
            "--capability meridian.account.balance_and_last_activity",
        )
    try:
        from understudy.artifact import CapabilityArtifact

        artifact = CapabilityArtifact.load(ARTIFACT)
    except ImportError as exc:
        return fail(
            f"cannot import understudy: {exc}",
            "Run from the project root:\n    cd " + str(ROOT),
        )
    except Exception as exc:
        return fail(
            f"artifact does not validate: {exc}",
            "The schema changed since it was compiled. Recompile:\n"
            "    python3 -m understudy.compile_run "
            "--capability meridian.account.balance_and_last_activity",
        )

    problems = []
    if not artifact.inputs:
        problems.append("no declared inputs (the flow may be frozen to one record)")
    if not any(o.extract for o in artifact.outputs):
        problems.append("no output has an extraction rule")
    if problems:
        return warn(
            "; ".join(problems),
            "Re-record so the journal carries a final screen, then recompile:\n"
            "    python3 -m understudy.discover --no-vision",
        )
    extractable = sum(1 for o in artifact.outputs if o.extract)
    return ok(
        f"{artifact.capability.id}@{artifact.capability.version} "
        f"[{artifact.capability.approval.value}] "
        f"{len(artifact.steps)} steps, {extractable} extractable outputs"
    )


def check_replay() -> Result:
    """End to end, with no model in the loop."""
    env = dict(os.environ, BANK_USER="jsmith", BANK_PASSWORD="demo1234")
    try:
        finished = subprocess.run(
            [
                sys.executable, "-m", "understudy.replay_run",
                "--artifact", str(ARTIFACT),
                "--input", "account_id=13566",
                "--unattended", "--json",
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return fail("replay timed out after 180s", "Is the target app still responding?")

    try:
        result = json.loads(finished.stdout)
    except json.JSONDecodeError:
        return fail(
            f"replay produced no parseable result (exit {finished.returncode})",
            (finished.stderr or finished.stdout or "").strip()[:300],
        )

    if result["status"] != "ok":
        return fail(
            f"replay returned {result['status']} at {result.get('step')}",
            f"reason: {result.get('reason')}\n"
            "  Recompile from a fresh run if the app or schema changed:\n"
            "    python3 -m understudy.discover --no-vision\n"
            "    python3 -m understudy.compile_run "
            "--capability meridian.account.balance_and_last_activity",
        )

    balance = result["outputs"].get("current_balance")
    if balance != "$4820.55":
        return warn(
            f"replay ok but balance is {balance}, expected $4820.55",
            "Seed data has been mutated by a transfer. Reset it:\n"
            f"    curl -X POST {APP_URL}/_reset",
        )
    return ok(
        f"ok in {result['duration_ms']}ms, {result['steps_run']} steps, "
        f"{result['model_calls']} model calls, balance {balance}"
    )


def check_tests() -> Result:
    finished = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    tail = (finished.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "no output"
    if finished.returncode != 0:
        failures = [l for l in tail if l.startswith("FAILED")][:5]
        return fail(
            summary,
            "Failing tests:\n    " + "\n    ".join(failures or ["see output"])
            + "\n  Re-run one for detail:\n    python3 -m pytest tests/ -q -x -vv",
        )
    return ok(summary)


CHECKS = [
    ("interpreter", check_python, None),
    ("packages", check_packages, "interpreter"),
    ("browser", check_browser, "packages"),
    ("api key (discovery only)", check_api_key, "packages"),
    ("target app port", check_port, None),
    ("target app", check_app, "target app port"),
    ("surface / AX tree", check_surface, "target app"),
    ("artifact", check_artifact, "packages"),
    ("replay end to end", check_replay, "artifact"),
    ("test suite", check_tests, "packages"),
]


def main() -> int:
    print(f"\n  understudy doctor{DIM}  ({ROOT}){RESET}\n")
    results: dict[str, Result] = {}
    failures: list[tuple[str, Result]] = []

    for name, run, needs in CHECKS:
        if needs and not results.get(needs, Result(False)).ok:
            print(f"  {DIM}skip{RESET}  {name}{DIM}  (needs '{needs}'){RESET}")
            results[name] = Result(False)
            continue
        try:
            result = run()
        except Exception as exc:  # a check itself broke
            result = fail(f"check raised {type(exc).__name__}: {exc}", "")
        results[name] = result

        if not result.ok:
            print(f"  {RED}FAIL{RESET}  {name}{DIM}  {result.detail}{RESET}")
            failures.append((name, result))
        elif result.warn:
            print(f"  {YELLOW}WARN{RESET}  {name}{DIM}  {result.detail}{RESET}")
            failures.append((name, result))
        else:
            print(f"  {GREEN}ok{RESET}    {name}{DIM}  {result.detail}{RESET}")

    print()
    if not failures:
        print(f"  {GREEN}Everything checks out.{RESET}\n")
        return 0

    print(f"  {'-' * 62}\n  How to fix\n")
    for name, result in failures:
        label = "warning" if result.warn else "failure"
        print(f"  [{label}] {name}")
        for line in (result.fix or "no suggestion available").splitlines():
            print(f"    {line}")
        print()

    hard = [f for f in failures if not f[1].warn]
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
