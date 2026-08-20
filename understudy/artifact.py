"""The capability artifact: a typed, versioned, reviewable contract.

This is the load-bearing schema of the whole system. A discovery run compiles
into one of these; deterministic replay reads one; a calling agent reads its
top half to know what the capability needs and returns; a human reviews it
before it is approved for unattended use.

Two principles shape it.

**A contract, not a step list.** The top of the artifact -- inputs, outputs,
outcomes, success condition -- tells a caller everything it needs without
reading a single step. Steps are the implementation.

**No selectors, anywhere.** A step identifies a control by a ranked ladder of
*semantic* descriptors: role plus accessible name, then narrower scope, then a
structural anchor, then geometry. Each rung is a different kind of evidence, so
they fail independently. Nothing in here mentions CSS, XPath or a DOM, which is
what lets the same artifact describe a desktop surface later.

The artifact also splits in two: a tenant-agnostic ``CapabilityArtifact`` (the
flow, keyed by vendor product) and a small per-institution ``TenantBinding``.
That split is what stops hundreds of tenants meaning hundreds of recordings.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


# --------------------------------------------------------------------- enums


class Sensitivity(str, Enum):
    """Drives redaction. Anything above `internal` never reaches disk."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"


class Risk(str, Enum):
    """What a step could do if it goes wrong. Decides who is allowed to run it."""

    READ = "read"
    REVERSIBLE = "reversible"
    MUTATING = "mutating"
    IRREVERSIBLE = "irreversible"
    CREDENTIAL = "credential"


class Approval(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"


class OutcomeKind(str, Enum):
    """The distinction the brief calls the most common design mistake here."""

    BUSINESS = "business"  # a legitimate answer the caller must handle
    RECOVERABLE = "recoverable"  # retry or dismiss, then continue
    FATAL = "fatal"  # stop and surface a debuggable error


# ---------------------------------------------------------------- locating


class Descriptor(BaseModel):
    """One rung of the locator ladder.

    Ordered by how much meaning each carries: a role with an accessible name is
    the strongest evidence; raw geometry is the weakest and is policy-gated.
    """

    strategy: Literal["role_name", "role_anchor", "text", "ordinal", "coords"]
    role: str | None = None
    name: str | None = None
    # When the control's visible name *is* the data -- a link whose text is the
    # account number -- the recorded value must not be frozen into the locator,
    # or every replay reopens the account the discovery run happened to use.
    # Replay substitutes this input parameter's value for `name` at resolve time.
    name_param: str | None = None
    name_match: Literal["exact", "normalized", "contains"] = "normalized"
    # The visible text that gives an unnamed control its meaning. On legacy
    # tables this is frequently the only thing identifying an input.
    anchor: str | None = None
    # Frame names differ per institution, so this belongs to the binding rather
    # than the flow whenever it varies.
    frame: str | None = None
    ordinal: int | None = None

    def describe(self) -> str:
        if self.name_param:
            return f"{self.role} named {{{self.name_param}}}"
        if self.strategy == "role_name":
            return f"{self.role} named {self.name!r}"
        if self.strategy == "role_anchor":
            return f"{self.role} labelled by {self.anchor!r}"
        return f"{self.strategy} {self.name or self.anchor!r}"


class Target(BaseModel):
    primary: Descriptor
    fallbacks: list[Descriptor] = Field(default_factory=list)
    # Replay refuses a match below this rather than clicking the wrong thing
    # confidently. Ambiguity is an error, not a coin flip.
    min_confidence: float = 0.80


class Checkpoint(BaseModel):
    """An assertion that the screen actually reached the expected state.

    A click that appears to succeed and a click that changed the screen are
    different events. Without this, a failure surfaces four steps later as
    something incomprehensible.
    """

    kind: Literal["text_present", "text_absent", "url_contains", "node_present"]
    value: str
    frame: str | None = None
    timeout_ms: int = 8000


# ------------------------------------------------------------------ contract


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "number", "integer", "boolean", "date"] = "string"
    required: bool = True
    pattern: str | None = None
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    example: str | None = None


class Extraction(BaseModel):
    """How replay reads one value off the screen.

    ``anchor_cell`` is the workhorse on legacy tables: find the cell whose text
    is the label, take the cell beside it. That relationship survives restyling
    and reordering in a way a positional read never does.
    """

    method: Literal["anchor_cell", "regex", "node_text"]
    anchor: str | None = None
    frame: str | None = None
    pattern: str | None = None
    group: int = 1


class OutputSpec(BaseModel):
    name: str
    type: Literal["string", "number", "integer", "boolean", "date"] = "string"
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    extracted_at_step: str | None = None
    # Without this an output is a promise the artifact cannot keep.
    extract: Extraction | None = None


class OutcomeSpec(BaseModel):
    """A declared non-success result, with how to detect it -- and for a
    recoverable one, what to actually do about it.

    "Recoverable" is not a single behaviour: an unexpected interstitial is
    dismissed, an expired session is re-authenticated, a transient stall is
    waited out. Collapsing them into "retry" would leave the first two
    looping until the budget runs out.
    """

    code: str
    kind: OutcomeKind
    detector: Checkpoint
    message: str = ""
    # What should have been true instead. Reported verbatim as the `expected`
    # half of a failure, so the report names the actual condition ("the
    # credentials to be accepted") rather than restating that a rule matched.
    expectation: str = ""
    recovery: Literal["retry", "dismiss", "reauth"] = "retry"
    # For `dismiss`: the visible name of the control that clears the condition.
    recovery_target: str | None = None
    # How many times recovery may be attempted before escalating.
    max_attempts: int = 1


# --------------------------------------------------------------------- steps


class Action(BaseModel):
    kind: Literal[
        "navigate",
        "click",
        "type",
        "type_secret",
        "select",
        "read",
        "assert",
        "credential_entry",
        "human_action",
    ]
    url: str | None = None
    text: str | None = None
    value: str | None = None
    # When set, the value comes from an input parameter at invocation time
    # rather than being baked in from the recorded run.
    param: str | None = None
    secret_ref: str | None = None
    # Entering a credential logically includes submitting it. When a person
    # signed in during discovery, the click that submitted the form was never
    # recorded (capture is suspended during a handoff), so the flow would
    # otherwise stall on the sign-on screen at replay time.
    submit_form: bool = False
    instruction: str | None = None


class Step(BaseModel):
    id: str
    # The *why*, in the discoverer's words. This is what makes the artifact
    # reviewable by a human and re-recordable by a model if a step ever breaks.
    intent: str
    action: Action
    target: Target | None = None
    precondition: Checkpoint | None = None
    postcondition: Checkpoint | None = None
    risk: Risk = Risk.READ
    timeout_ms: int = 10_000
    on_error: list[str] = Field(default_factory=lambda: ["wait_retry", "escalate"])
    # When this step's control cannot be resolved, that absence *means*
    # something to the caller rather than indicating a broken capability.
    on_unresolved: str | None = None

    @property
    def requires_operator(self) -> bool:
        return self.risk in (Risk.CREDENTIAL, Risk.IRREVERSIBLE)


# ---------------------------------------------------------------- provenance


class Provenance(BaseModel):
    discovered_by_model: str
    run_id: str
    created_at: str
    recorded_on_tenant: str
    evidence_ref: str
    # Server-issued identifiers. These are what let a reviewer confirm the run
    # genuinely happened rather than trusting the transcript.
    request_ids: list[str] = Field(default_factory=list)
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class CapabilityMeta(BaseModel):
    id: str
    name: str
    summary: str
    version: str = "1.0.0"
    approval: Approval = Approval.DRAFT
    # Rolling replay success rate, once replay has run enough times to mean
    # anything. None until then -- an unmeasured capability should not look
    # like a reliable one.
    stability: float | None = None

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not SEMVER.match(value):
            raise ValueError(f"version must be semver, got {value!r}")
        return value


class AppProfile(BaseModel):
    product: str
    version_range: str = "*"
    surface_kind: Literal["web", "legacy_web", "desktop"] = "legacy_web"


# ------------------------------------------------------------------ artifact


class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability: CapabilityMeta
    app: AppProfile
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    success: Checkpoint | None = None
    provenance: Provenance | None = None

    # ------------------------------------------------------------- contract

    @property
    def requires_operator(self) -> bool:
        """True if any step needs a human. A caller should know this up front."""
        return any(step.requires_operator for step in self.steps)

    @property
    def operator_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.requires_operator]

    def tool_signature(self) -> dict:
        """The agent-facing view: what this capability needs and returns.

        This is what a capability catalog would serve, and it is deliberately
        derivable from the artifact rather than maintained separately.
        """
        return {
            "name": self.capability.id,
            "description": self.capability.summary,
            "version": self.capability.version,
            "approval": self.capability.approval.value,
            "requires_operator": self.requires_operator,
            "operator_steps": self.operator_steps,
            "input_schema": {
                "type": "object",
                "properties": {
                    p.name: {
                        "type": p.type,
                        "description": p.description,
                        **({"pattern": p.pattern} if p.pattern else {}),
                    }
                    for p in self.inputs
                },
                "required": [p.name for p in self.inputs if p.required],
            },
            "returns": {o.name: o.type for o in self.outputs},
            "outcomes": [
                {"code": o.code, "kind": o.kind.value} for o in self.outcomes
            ],
        }

    # ------------------------------------------------------------ persistence

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.capability.id}@{self.capability.version}.json"
        path.write_text(self.model_dump_json(indent=2, exclude_none=True))
        return path

    @classmethod
    def load(cls, path: Path) -> "CapabilityArtifact":
        return cls.model_validate_json(Path(path).read_text())

    def summarise(self) -> str:
        lines = [
            f"{self.capability.id}@{self.capability.version} [{self.capability.approval.value}]",
            f"  {self.capability.summary}",
            f"  app        {self.app.product} ({self.app.surface_kind})",
            f"  inputs     {', '.join(f'{p.name}:{p.type}' for p in self.inputs) or '(none)'}",
            f"  outputs    {', '.join(f'{o.name}:{o.type}' for o in self.outputs) or '(none)'}",
            f"  outcomes   {', '.join(o.code for o in self.outcomes) or '(none)'}",
            f"  steps      {len(self.steps)}",
        ]
        if self.requires_operator:
            lines.append(f"  operator   required at {', '.join(self.operator_steps)}")
        return "\n".join(lines)


# ------------------------------------------------------------------ bindings


class TenantBinding(BaseModel):
    """Per-institution overlay onto a tenant-agnostic flow.

    Deliberately small: a base URL, relabelled controls, renamed frames, a
    step or two to insert. Small enough that a human can review one per tenant
    in a minute, which is what makes per-tenant review affordable at all.
    """

    tenant_id: str
    capability_id: str
    capability_version: str
    base_url: str
    # step_id -> partial overrides (target descriptors, timeouts)
    overrides: dict[str, dict] = Field(default_factory=dict)
    # step_id -> steps to run before it (a tenant-only interstitial)
    insert_before: dict[str, list[Step]] = Field(default_factory=dict)
    last_verified: str | None = None
    # Rising fallback usage is the drift signal: the flow still works, but it
    # is working harder, which is when to re-record rather than after it breaks.
    fallback_rate: float | None = None
    status: Literal["healthy", "needs_review", "broken"] = "healthy"

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.capability_id}@{self.capability_version}--{self.tenant_id}.json"
        path.write_text(self.model_dump_json(indent=2, exclude_none=True))
        return path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export_json_schema(directory: Path) -> Path:
    """Emit the JSON Schema. One definition serves validation and the catalog."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "capability-artifact.schema.json"
    path.write_text(json.dumps(CapabilityArtifact.model_json_schema(), indent=2))
    return path
