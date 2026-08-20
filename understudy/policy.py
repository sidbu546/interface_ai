"""Guardrails, enforced in code rather than in the prompt.

A rule written in a system prompt is advice the model can talk itself out of.
A rule enforced here sits on the only path to the browser, so neither the
discovery loop nor (later) the replay engine can route around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


class PolicyViolation(Exception):
    """Raised when an action is refused. Surfaced to the model as a tool error."""


@dataclass
class Policy:
    # Only these origins may ever be loaded.
    allowed_origins: set[str] = field(default_factory=set)
    # Substrings of a URL that may never be requested, whatever the origin.
    denied_paths: tuple[str, ...] = ("/_reset",)
    # Action kinds the agent is permitted to perform at all.
    allowed_actions: frozenset[str] = frozenset(
        {"click", "type", "type_secret", "select", "read", "request_human", "finish"}
    )
    # Visible control text that indicates an irreversible action. Discovery is
    # never allowed to press these; replay escalates to a human instead.
    irreversible_labels: tuple[str, ...] = (
        "submit transfer",
        "confirm transfer",
        "transfer funds now",
        "delete",
        "close account",
    )
    max_steps: int = 25
    max_seconds: float = 300.0

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins:
            raise PolicyViolation(
                f"navigation to {origin} is outside the allowlist {sorted(self.allowed_origins)}"
            )
        for denied in self.denied_paths:
            if denied in url:
                raise PolicyViolation(f"path {denied!r} is explicitly denied")

    def check_action(self, kind: str) -> None:
        if kind not in self.allowed_actions:
            raise PolicyViolation(f"action {kind!r} is not permitted")

    def classify_risk(self, label: str) -> str:
        """read | mutating | irreversible, from the control's visible text."""
        lowered = (label or "").strip().lower()
        if any(marker in lowered for marker in self.irreversible_labels):
            return "irreversible"
        return "mutating" if lowered in {"continue", "log in", "sign on"} else "read"

    def check_click(self, label: str) -> None:
        if self.classify_risk(label) == "irreversible":
            raise PolicyViolation(
                f"{label!r} is classified irreversible; discovery must stop here and "
                "escalate to a human rather than commit it"
            )


def default_policy(base_url: str) -> Policy:
    parsed = urlparse(base_url)
    return Policy(allowed_origins={f"{parsed.scheme}://{parsed.netloc}"})
