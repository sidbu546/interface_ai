"""Tests for the capability artifact schema and the compiler.

The schema is the contract everything else agrees on, so these tests pin down
the properties that make it a contract rather than a transcript:

  * a caller can read what it needs and returns without reading any step
  * no step carries a selector -- only semantic descriptors
  * recorded data is parameterised, not frozen into locators
  * steps needing a human are declared up front
  * provenance carries the server-issued ids that prove the run happened
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from understudy.artifact import (
    Approval,
    CapabilityArtifact,
    OutcomeKind,
    Risk,
    Sensitivity,
    TenantBinding,
    export_json_schema,
)
from understudy.compiler import compile_run

CAPABILITY = "meridian.account.balance_and_last_activity"


def write_journal(tmp_path: Path) -> Path:
    """A minimal but realistic discovery journal."""
    records = [
        {"event": "run_started", "run_id": "r1", "goal": "Open account 13566 and read its balance.",
         "target": "http://127.0.0.1:8099/meridian/index.htm", "model": "claude-opus-5"},
        {"event": "intervention_raised", "step": 1, "kind": "credential_handoff",
         "reason": "a credential step was reached", "capture": "suspended"},
        {"event": "intervention_resolved", "step": 1, "kind": "credential_handoff",
         "actor": "human", "note": "signed in"},
        {"event": "decided", "step": 2, "model": "claude-opus-5", "message_id": "msg_1",
         "request_id": "req_abc", "stop_reason": "tool_use", "latency_ms": 1200,
         "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0}},
        {"event": "acted", "step": 2, "actor": "agent", "tool": "click",
         "args": {"ref": 31, "intent": "Open account 13566"},
         "target": {"role": "link", "name": "13566", "anchor": "", "frame": "contentframe"},
         "result": "clicked", "ok": True},
        {"event": "acted", "step": 3, "actor": "agent", "tool": "type",
         "args": {"ref": 17, "text": "25", "intent": "Enter amount"},
         "target": {"role": "textbox", "name": "", "anchor": "$", "frame": "contentframe"},
         "result": "typed", "ok": True},
        {"event": "acted", "step": 4, "actor": "agent", "tool": "click",
         "args": {"ref": 9, "intent": "A failed attempt"},
         "target": {"role": "link", "name": "Nope", "anchor": "", "frame": ""},
         "result": "failed", "ok": False},
        {"event": "run_finished", "status": "success",
         "outputs": {"balance": "$4820.55", "last_txn_date": "2026-08-14"},
         "model_calls": 1, "tokens_in": 100, "tokens_out": 10, "duration_s": 5.0},
    ]
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


@pytest.fixture()
def artifact(tmp_path) -> CapabilityArtifact:
    return compile_run(write_journal(tmp_path), capability_id=CAPABILITY)


# ------------------------------------------------------------------ contract


def test_caller_can_read_the_contract_without_reading_steps(artifact):
    sig = artifact.tool_signature()
    assert sig["name"] == CAPABILITY
    assert "balance" in sig["returns"]
    assert sig["input_schema"]["type"] == "object"
    assert sig["outcomes"], "declared outcomes are part of the contract"


def test_operator_requirement_is_declared_up_front(artifact):
    """A caller must know a human is needed before invoking, not discover it."""
    assert artifact.requires_operator
    assert any("authenticate" in s for s in artifact.operator_steps)
    assert artifact.tool_signature()["requires_operator"] is True


def test_business_outcomes_are_declared_with_detectors(artifact):
    codes = {o.code: o for o in artifact.outcomes}
    assert "account_not_found" in codes
    assert codes["account_not_found"].kind is OutcomeKind.BUSINESS
    # A recoverable condition is a different tier from a business outcome.
    assert codes["session_expired"].kind is OutcomeKind.RECOVERABLE
    for outcome in artifact.outcomes:
        assert outcome.detector.value, "an outcome without a detector is undetectable"


def test_compiled_artifact_starts_as_draft(artifact):
    """Inference is useful, not trustworthy. Approval is a human act."""
    assert artifact.capability.approval is Approval.DRAFT
    assert artifact.capability.stability is None


# ------------------------------------------------------------------ locating


def test_no_step_contains_a_selector(artifact):
    """The whole portability claim rests on this."""
    blob = artifact.model_dump_json().lower()
    for banned in ("css", "xpath", "queryselector", "//div", "#id", "class="):
        assert banned not in blob, f"{banned!r} leaked into the artifact"


def test_data_is_parameterised_not_frozen_into_the_locator(artifact):
    """A link whose text is the account number must not pin the capability."""
    step = next(s for s in artifact.steps if s.target and s.target.primary.name_param)
    assert step.target.primary.name_param == "account_id"
    assert "account_id" in {p.name for p in artifact.inputs}


def test_targets_carry_a_fallback_ladder(artifact):
    typed = next(s for s in artifact.steps if s.action.kind == "type")
    assert typed.target is not None
    # The amount field has no accessible name, so it is located by its anchor.
    assert typed.target.primary.strategy == "role_anchor"
    assert typed.target.primary.anchor == "$"
    assert typed.target.min_confidence >= 0.8


def test_failed_attempts_are_not_compiled_into_the_flow(artifact):
    assert not any("failed_attempt" in s.id for s in artifact.steps)
    assert all("Nope" not in (s.target.primary.name or "") for s in artifact.steps if s.target)


# --------------------------------------------------------------------- risk


def test_credential_step_is_classified_and_needs_an_operator(artifact):
    step = next(s for s in artifact.steps if s.action.kind == "credential_entry")
    assert step.risk is Risk.CREDENTIAL
    assert step.requires_operator
    assert step.postcondition is not None, "a handoff must verify, not assume"


def test_account_identifiers_are_marked_sensitive(artifact):
    account = next(p for p in artifact.inputs if p.name == "account_id")
    assert account.sensitivity is Sensitivity.PII


# --------------------------------------------------------------- provenance


def test_provenance_carries_proof_the_run_happened(artifact):
    prov = artifact.provenance
    assert prov.discovered_by_model == "claude-opus-5"
    assert prov.request_ids == ["req_abc"]
    assert prov.recorded_on_tenant == "meridian"


def test_no_credential_survives_compilation(artifact):
    assert "demo1234" not in artifact.model_dump_json()


# -------------------------------------------------------------- persistence


def test_round_trips_through_disk(artifact, tmp_path):
    path = artifact.save(tmp_path)
    reloaded = CapabilityArtifact.load(path)
    assert reloaded.capability.id == artifact.capability.id
    assert len(reloaded.steps) == len(artifact.steps)


def test_version_must_be_semver(artifact):
    artifact.capability.version = "1.2.3"  # fine
    with pytest.raises(ValueError):
        type(artifact.capability)(
            id="x", name="x", summary="x", version="1.2"
        )


def test_json_schema_exports(tmp_path):
    path = export_json_schema(tmp_path)
    schema = json.loads(path.read_text())
    assert "properties" in schema and "steps" in schema["properties"]


# ------------------------------------------------------------------ binding


def test_tenant_binding_is_a_small_overlay():
    """Per-tenant review is only affordable if a binding stays small."""
    binding = TenantBinding(
        tenant_id="cascade",
        capability_id=CAPABILITY,
        capability_version="1.0.0",
        base_url="http://127.0.0.1:8099/cascade/index.htm",
        overrides={"s3_open": {"frame": "body"}},
    )
    assert binding.status == "healthy"
    assert binding.fallback_rate is None
    # The flow itself is not duplicated into the binding.
    assert "steps" not in binding.model_dump(exclude_none=True)


def test_a_value_seen_only_mid_flow_is_still_extractable(tmp_path):
    """An output read before a mutation must bind to the screen that showed it.

    The pre-transfer balance does not survive to the final screen -- by the end
    the same cell reads the new figure. Deriving every output from the last
    screen therefore returns nothing for it (no anchor) or, worse, the wrong
    number. It has to be tied to the step it was actually visible on.
    """
    before = [
        {"role": "LayoutTableCell", "name": "Balance", "frame": "contentframe"},
        {"role": "LayoutTableCell", "name": "$4820.55", "frame": "contentframe"},
    ]
    after = [
        {"role": "LayoutTableCell", "name": "Balance", "frame": "contentframe"},
        {"role": "LayoutTableCell", "name": "$4845.55", "frame": "contentframe"},
    ]
    records = [
        {"event": "run_started", "run_id": "r1", "goal": "Read, transfer, re-read.",
         "target": "http://127.0.0.1:8099/meridian/index.htm", "model": "claude-opus-5"},
        {"event": "acted", "step": 1, "actor": "agent", "tool": "click",
         "args": {"ref": 1, "intent": "Open account 13566"},
         "target": {"role": "link", "name": "13566", "anchor": "", "frame": "contentframe"},
         "result": "clicked", "ok": True},
        # Observed at step 2 == the screen step 1 left behind.
        {"event": "observed", "step": 2, "url": "http://x/account.htm",
         "nodes": len(before), "screen": before},
        {"event": "acted", "step": 2, "actor": "agent", "tool": "click",
         "args": {"ref": 2, "intent": "Re-open account 13566"},
         "target": {"role": "link", "name": "13566", "anchor": "", "frame": "contentframe"},
         "result": "clicked", "ok": True},
        {"event": "final_screen", "nodes": after},
        {"event": "run_finished", "status": "success",
         "outputs": {"balance_before": "$4820.55", "balance_after": "$4845.55"},
         "model_calls": 2, "tokens_in": 10, "tokens_out": 2, "duration_s": 1.0},
    ]
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    artifact = compile_run(path, capability_id=CAPABILITY)

    outputs = {o.name: o for o in artifact.outputs}
    # Both are returnable -- the point of the change.
    assert outputs["balance_before"].extract is not None
    assert outputs["balance_after"].extract is not None
    # And they are read at different points in the run, not both at the end.
    assert outputs["balance_before"].extracted_at_step != artifact.steps[-1].id
    assert outputs["balance_after"].extracted_at_step == artifact.steps[-1].id
