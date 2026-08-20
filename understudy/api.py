"""The agent-facing capability catalog, as an HTTP API.

    python3 -m understudy.api          # serves on 127.0.0.1:8100

This is the interface an AI agent calls in production. It is deliberately
model-free: discovery is a GET, invocation is a POST with typed arguments, and
everything underneath is deterministic replay. Whatever selects the capability
-- an agent, a workflow engine, a queue consumer, a person with curl -- talks to
the same three endpoints.

    GET  /capabilities                    what can be called, and with what
    GET  /capabilities/{name}             one capability's full contract
    POST /capabilities/{name}/invoke      run it, with typed inputs

The contract each endpoint serves is derived from the artifact itself, so it
cannot drift from what replay will actually do.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .catalog import Catalog

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="Understudy capability catalog",
    description=(
        "Saved back-office automations, callable by name with typed arguments. "
        "No model is invoked by any endpoint."
    ),
    version="1.0.0",
)


def catalog() -> Catalog:
    # Reloaded per request so a freshly compiled artifact appears without a
    # restart -- the catalog is a view over the artifacts directory, not a cache.
    return Catalog(ROOT / "artifacts")


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    # Absent means unattended. A capability with operator steps then returns
    # needs_human rather than silently skipping them.
    attended: bool = False


@app.get("/capabilities")
def list_capabilities() -> dict[str, Any]:
    """Discovery: what an agent can call, and what each one needs."""
    entries = []
    for name, artifact in catalog().capabilities.items():
        signature = artifact.tool_signature()
        entries.append(
            {
                "name": name,
                "capability_id": artifact.capability.id,
                "version": signature["version"],
                "approval": signature["approval"],
                "description": signature["description"],
                "input_schema": signature["input_schema"],
                "returns": signature["returns"],
                "outcomes": signature["outcomes"],
                "requires_operator": signature["requires_operator"],
                "operator_steps": signature["operator_steps"],
            }
        )
    return {"capabilities": entries, "count": len(entries)}


@app.get("/capabilities/{name}")
def get_capability(name: str) -> dict[str, Any]:
    artifact = catalog().capabilities.get(name)
    if artifact is None:
        raise HTTPException(404, f"no capability named {name!r}")
    return artifact.tool_signature()


@app.post("/capabilities/{name}/invoke")
def invoke_capability(name: str, request: InvokeRequest) -> dict[str, Any]:
    """Execution: deterministic replay. No model is consulted."""
    active = catalog()
    if name not in active.capabilities:
        raise HTTPException(404, f"no capability named {name!r}")

    result = active.invoke(
        name,
        request.inputs,
        # Credentials belong to the caller, never to the capability. Attended
        # invocation over HTTP would need a co-browsing surface, so this API
        # only offers the vault path; attended runs go through the CLI.
        secrets={
            "username": os.environ.get("BANK_USER", ""),
            "password": os.environ.get("BANK_PASSWORD", ""),
        },
        evidence_dir=ROOT / "evidence" / f"api-{name[:24]}",
    )
    # HTTP status mirrors the result contract: a business outcome is a
    # successful call that returned a non-success answer, not a server error.
    return result


def main() -> int:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("API_PORT", "8100")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
