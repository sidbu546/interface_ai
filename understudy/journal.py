"""Evidence: a structured record of what happened and why.

Every model call records the identifiers Anthropic issues -- ``message_id``
and ``request_id`` -- alongside token usage. Those are the fields that let a
third party confirm a discovery run genuinely happened rather than taking the
transcript's word for it.

Secrets never reach this file. The agent asks for a secret *by name* and the
value is substituted at the driver; the journal records the name only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Journal:
    run_id: str
    directory: Path
    _started: float = field(default_factory=time.time)
    _tokens_in: int = 0
    _tokens_out: int = 0
    _model_calls: int = 0

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "journal.jsonl"
        self.shots = self.directory / "screenshots"
        self.shots.mkdir(exist_ok=True)

    # ------------------------------------------------------------------ write

    def _write(self, record: dict[str, Any]) -> None:
        record["run_id"] = self.run_id
        record["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def run_started(self, goal: str, target: str, model: str) -> None:
        self._write(
            {"event": "run_started", "goal": goal, "target": target, "model": model}
        )

    def observed(
        self, step: int, url: str, nodes: list[dict[str, Any]] | int
    ) -> None:
        """Record what the agent saw, including the screen itself.

        Keeping only a node count was enough to audit a run but not to compile
        one: an output read mid-flow -- a balance noted *before* a transfer --
        lives on a screen that no longer exists by the end, so the compiler had
        no way to derive a rule for fetching it again. The screen is kept for
        the same reason ``final_screen`` is.
        """
        count = nodes if isinstance(nodes, int) else len(nodes)
        record: dict[str, Any] = {
            "event": "observed",
            "step": step,
            "url": url,
            "nodes": count,
        }
        if not isinstance(nodes, int):
            record["screen"] = nodes
        self._write(record)

    def decided(self, step: int, response: Any, latency_ms: int) -> None:
        """Record the model's decision plus its server-issued identifiers."""
        usage = response.usage
        self._model_calls += 1
        self._tokens_in += usage.input_tokens
        self._tokens_out += usage.output_tokens
        self._write(
            {
                "event": "decided",
                "step": step,
                "model": response.model,
                "message_id": response.id,
                "request_id": getattr(response, "_request_id", None),
                "stop_reason": response.stop_reason,
                "latency_ms": latency_ms,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_input_tokens": getattr(
                        usage, "cache_read_input_tokens", 0
                    ),
                },
            }
        )

    def acted(
        self,
        step: int,
        tool: str,
        args: dict[str, Any],
        result: str,
        ok: bool,
        target: dict[str, Any] | None = None,
    ) -> None:
        self._write(
            {
                "event": "acted",
                "step": step,
                "actor": "agent",
                "tool": tool,
                "args": redact(args),
                # The semantic descriptor of the control acted on. This, not the
                # ref, is what compiles into a replayable step.
                "target": target,
                "result": result,
                "ok": ok,
            }
        )

    def intervention_raised(self, step: int, kind: str, reason: str) -> None:
        """A human is needed. Records that capture stops here, and why.

        For a credential handoff this is the whole audit trail: that it
        happened, when, and for what. Never what was typed.
        """
        self._write(
            {
                "event": "intervention_raised",
                "step": step,
                "kind": kind,
                "reason": reason,
                "control_transferred_to": "human",
                "capture": "suspended",
            }
        )

    def intervention_resolved(self, step: int, kind: str, note: str) -> None:
        self._write(
            {
                "event": "intervention_resolved",
                "step": step,
                "kind": kind,
                "actor": "human",
                "note": note,
                "control_transferred_to": "agent",
                "capture": "resumed",
            }
        )

    def final_screen(self, nodes: list[dict[str, Any]]) -> None:
        """The screen as it stood when the goal was met.

        The compiler needs this to work out *how* to read each declared output:
        it locates the reported value among these nodes and derives an anchor.
        Without it, outputs are values the artifact observed once but cannot
        fetch again.
        """
        self._write({"event": "final_screen", "nodes": nodes})

    def run_finished(self, status: str, outputs: dict[str, Any] | None = None) -> None:
        self._write(
            {
                "event": "run_finished",
                "status": status,
                "outputs": outputs or {},
                "model_calls": self._model_calls,
                "tokens_in": self._tokens_in,
                "tokens_out": self._tokens_out,
                "duration_s": round(time.time() - self._started, 1),
            }
        )

    # ------------------------------------------------------------------- misc

    @property
    def totals(self) -> tuple[int, int, int]:
        return self._model_calls, self._tokens_in, self._tokens_out


SECRET_KEYS = {"password", "secret", "pin", "token", "secret_name"}


def redact(args: dict[str, Any]) -> dict[str, Any]:
    """Strip anything that could carry a credential.

    ``type_secret`` never receives a value in the first place -- it takes the
    *name* of a secret. This is belt and braces for anything else.
    """
    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if key.lower() in SECRET_KEYS and key.lower() != "secret_name":
            cleaned[key] = "<redacted:secret>"
        else:
            cleaned[key] = value
    return cleaned
