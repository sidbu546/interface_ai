"""The agent-facing surface: saved artifacts as callable capabilities.

This is what the whole system is *for*. An AI agent decides what needs doing;
it looks up a capability by name, sees its typed arguments and declared
outcomes, and invokes it. Everything below that call is deterministic replay
with no model in the loop.

Two properties matter here.

**The contract is derived, not maintained.** A capability's tool definition
comes from the artifact itself, so it cannot drift from what replay will
actually do.

**Escalation is advertised.** ``requires_operator`` is in the description, so a
calling agent knows a person is needed *before* it invokes, rather than
discovering it half way through.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .artifact import CapabilityArtifact
from .policy import default_policy
from .replay import ReplayEngine, ReplayResult
from .surface import WebSurface

# Anthropic tool names allow [a-zA-Z0-9_-]; capability ids use dots.
_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def tool_name_for(capability_id: str) -> str:
    return _SAFE.sub("_", capability_id)


class Catalog:
    """Loads capability artifacts and exposes them as invocable tools."""

    def __init__(self, directory: str | Path = "artifacts"):
        self.directory = Path(directory)
        self.capabilities: dict[str, CapabilityArtifact] = {}
        for path in sorted(self.directory.glob("*.json")):
            if path.name.endswith("schema.json"):
                continue
            try:
                artifact = CapabilityArtifact.load(path)
            except Exception:
                continue  # a malformed artifact must not take down the catalog
            self.capabilities[tool_name_for(artifact.capability.id)] = artifact

    # ------------------------------------------------------------- discovery

    def describe(self) -> str:
        lines = []
        for name, artifact in self.capabilities.items():
            signature = artifact.tool_signature()
            lines.append(
                f"  {name}  v{signature['version']} [{signature['approval']}]"
                + ("  operator required" if signature["requires_operator"] else "")
            )
        return "\n".join(lines) or "  (none)"

    def tools(self) -> list[dict[str, Any]]:
        """Anthropic tool definitions, derived from the artifacts themselves."""
        definitions = []
        for name, artifact in self.capabilities.items():
            signature = artifact.tool_signature()
            outcomes = ", ".join(
                f"{o['code']} ({o['kind']})" for o in signature["outcomes"]
            )
            description = (
                f"{signature['description']}\n"
                f"Returns: {', '.join(f'{k} ({v})' for k, v in signature['returns'].items()) or 'nothing'}.\n"
                f"Possible non-success outcomes: {outcomes or 'none'}.\n"
                f"Approval state: {signature['approval']}."
            )
            if signature["requires_operator"]:
                description += (
                    "\nNOTE: this capability has steps that need a human operator "
                    f"({', '.join(signature['operator_steps'])}). Unattended it will "
                    "return needs_human."
                )
            definitions.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": signature["input_schema"],
                }
            )
        return definitions

    # -------------------------------------------------------------- invoking

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        secrets: dict[str, str] | None = None,
        base_url: str = "http://127.0.0.1:8099/",
        headless: bool = True,
        evidence_dir: Path | None = None,
        operator: Callable[[str, str], bool] | None = None,
        faults: Any = None,
    ) -> dict[str, Any]:
        """Run a capability. No model is consulted anywhere below this line."""
        artifact = self.capabilities.get(name)
        if artifact is None:
            return {
                "status": "failed",
                "reason": f"no capability named {name!r}",
                "available": sorted(self.capabilities),
            }

        surface = WebSurface(headless=headless, faults=faults)
        try:
            engine = ReplayEngine(
                surface=surface,
                policy=default_policy(base_url),
                secrets=secrets or {},
                operator=operator,
                on_event=(lambda message: print(f"      {message}"))
                if operator is not None
                else (lambda _message: None),
                evidence_dir=evidence_dir,
            )
            result: ReplayResult = engine.run(artifact, arguments)
        finally:
            surface.close()

        return {
            "status": result.status,
            "outputs": result.outputs,
            "outcome_code": result.outcome_code,
            "reason": result.reason,
            "step": result.step,
            "expected": result.expected,
            "observed": result.observed,
            "evidence_ref": result.evidence_ref,
            "model_calls_inside_capability": 0,
            "duration_ms": result.duration_ms,
        }
