"""Tests for human-in-the-loop control transfer.

The interactive path -- a person typing into a real browser -- cannot be
automated here, so these tests drive the seam directly with a fake surface and
a scripted operator. What they pin down is the part that must not regress:

  * control is recorded as transferred, and back again
  * capture is marked suspended for the duration
  * no credential ever reaches the journal
  * an absent operator degrades safely instead of proceeding blind
  * resume verifies the screen actually changed rather than assuming it
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from understudy.agent import DiscoveryAgent
from understudy.journal import Journal
from understudy.policy import default_policy


class FakeSurface:
    """A surface whose URL the test controls, standing in for a live browser."""

    def __init__(self, url: str):
        self.url = url
        self.typed: list[tuple[int, str]] = []

    def observe(self):
        class Snap:
            pass

        snap = Snap()
        snap.url = self.url
        snap.nodes = []
        return snap

    def type_text(self, ref: int, text: str) -> str:
        self.typed.append((ref, text))
        return "typed"


def build_agent(tmp_path: Path, surface, credential_mode="human") -> DiscoveryAgent:
    journal = Journal(run_id="test-run", directory=tmp_path)
    agent = DiscoveryAgent(
        surface=surface,
        policy=default_policy("http://127.0.0.1:8099/"),
        journal=journal,
        secrets={},
        use_vision=False,
        credential_mode=credential_mode,
    )
    agent._current_step = 1
    return agent


def records(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]


# ------------------------------------------------------------ credential handoff


def test_credential_handoff_transfers_control_and_back(tmp_path, monkeypatch):
    surface = FakeSurface("http://127.0.0.1:8099/meridian/index.htm")
    agent = build_agent(tmp_path, surface)

    # The scripted operator signs in, so the screen advances past the login page.
    def operator(_prompt):
        surface.url = "http://127.0.0.1:8099/meridian/main.htm"
        return ""

    monkeypatch.setattr("builtins.input", operator)
    result, ok = agent._human_credential_handoff({"secret_name": "password"})

    assert ok
    assert "signed in" in result
    assert agent._human_signed_in

    events = [r["event"] for r in records(tmp_path)]
    assert "intervention_raised" in events
    assert "intervention_resolved" in events

    raised = next(r for r in records(tmp_path) if r["event"] == "intervention_raised")
    assert raised["control_transferred_to"] == "human"
    assert raised["capture"] == "suspended"

    resolved = next(r for r in records(tmp_path) if r["event"] == "intervention_resolved")
    assert resolved["control_transferred_to"] == "agent"
    assert resolved["actor"] == "human"


def test_resume_verifies_rather_than_assuming(tmp_path, monkeypatch):
    """Operator hands control back without actually signing in."""
    surface = FakeSurface("http://127.0.0.1:8099/meridian/index.htm")
    agent = build_agent(tmp_path, surface)

    monkeypatch.setattr("builtins.input", lambda _prompt: "")  # changes nothing
    result, ok = agent._human_credential_handoff({"secret_name": "password"})

    assert not ok, "still on the sign-on screen must not count as success"
    assert "still showing" in result
    assert not agent._human_signed_in


def test_absent_operator_fails_safely(tmp_path, monkeypatch):
    surface = FakeSurface("http://127.0.0.1:8099/meridian/index.htm")
    agent = build_agent(tmp_path, surface)

    def no_operator(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_operator)
    result, ok = agent._human_credential_handoff({"secret_name": "password"})

    assert not ok
    assert "HANDOFF FAILED" in result
    # The intervention is still recorded -- an abandoned handoff is evidence too.
    assert any(r["event"] == "intervention_resolved" for r in records(tmp_path))


def test_no_credential_reaches_the_journal(tmp_path, monkeypatch):
    surface = FakeSurface("http://127.0.0.1:8099/meridian/index.htm")
    agent = build_agent(tmp_path, surface)

    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    agent._human_credential_handoff({"secret_name": "password"})

    written = (tmp_path / "journal.jsonl").read_text()
    assert "demo1234" not in written
    assert surface.typed == [], "in human mode the system must type nothing"


def test_handoff_is_not_repeated_once_signed_in(tmp_path, monkeypatch):
    surface = FakeSurface("http://127.0.0.1:8099/meridian/main.htm")
    agent = build_agent(tmp_path, surface)
    agent._human_signed_in = True

    def should_not_run(_prompt):
        raise AssertionError("must not pause again once the operator has signed in")

    monkeypatch.setattr("builtins.input", should_not_run)
    result, ok = agent._human_credential_handoff({"secret_name": "password"})
    assert ok and "already signed in" in result


# ---------------------------------------------------------------- action handoff


def test_irreversible_action_handoff_returns_control_to_the_agent(tmp_path, monkeypatch):
    """The transfer case: agent stages it, operator commits, agent continues."""
    surface = FakeSurface("http://127.0.0.1:8099/meridian/transfer_confirm.htm")
    agent = build_agent(tmp_path, surface, credential_mode="env")

    def operator(_prompt):
        surface.url = "http://127.0.0.1:8099/meridian/transfer_done.htm"
        return ""

    monkeypatch.setattr("builtins.input", operator)
    result, ok = agent._human_action_handoff(
        {
            "reason": "committing the transfer is irreversible",
            "instruction": "click Submit Transfer",
        }
    )

    assert ok
    assert "transfer_done" in result
    # The agent is told to verify, not to assume the action succeeded.
    assert "verify" in result.lower()

    raised = next(r for r in records(tmp_path) if r["event"] == "intervention_raised")
    assert raised["kind"] == "human_action"
    assert raised["capture"] == "suspended"


def test_action_handoff_without_operator_is_not_reported_as_done(tmp_path, monkeypatch):
    surface = FakeSurface("http://127.0.0.1:8099/meridian/transfer_confirm.htm")
    agent = build_agent(tmp_path, surface, credential_mode="env")

    monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(EOFError))
    result, ok = agent._human_action_handoff(
        {"reason": "irreversible", "instruction": "click Submit Transfer"}
    )

    assert not ok
    assert "HANDOFF FAILED" in result
