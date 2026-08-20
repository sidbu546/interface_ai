"""Compile a discovery run into a capability artifact.

The compiler is what makes the artifact a *contract* rather than a transcript.
It reads the typed journal a run produced and emits ordered steps with semantic
targets, declared inputs and outputs, and the risk classification each step
carries -- decoupled from the model's prose entirely.

Two things it does deliberately:

**Parameterises the recorded values.** The run typed "25" into a field labelled
"$"; the artifact declares an ``amount`` parameter and binds the step to it.
Otherwise every replay would transfer exactly twenty-five dollars forever.

**Marks the result draft.** Inference is good enough to be useful and not good
enough to trust unattended. Parameter naming, risk classification and outcome
detectors all want a human's eye before this is approved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifact import (
    Extraction,
    Action,
    AppProfile,
    CapabilityArtifact,
    CapabilityMeta,
    Checkpoint,
    Descriptor,
    OutcomeKind,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    Provenance,
    Risk,
    Sensitivity,
    Step,
    Target,
    utc_now,
)

# Values that are obviously data rather than fixed UI text, and what to call
# them. Ordered: the first match names the parameter.
_PARAM_HINTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^\d{5}$"), "account_id", "integer"),
    (re.compile(r"^\d+(\.\d{1,2})?$"), "amount", "number"),
]


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return cleaned or "value"


def _param_for(value: str, label: str) -> tuple[str, str]:
    """(param_name, type) for a recorded value."""
    for pattern, name, kind in _PARAM_HINTS:
        if pattern.match(value or ""):
            # Prefer the field's own label when it is meaningful; "$" is not.
            base = _slug(label)
            if base in {"value", "_", ""} or len(base) < 3:
                return name, kind
            return base, kind
    return _slug(label), "string"


def _looks_like_data(text: str) -> tuple[str, str] | None:
    """(param_name, type) if this visible text is really a data value."""
    for pattern, name, kind in _PARAM_HINTS:
        if pattern.match((text or "").strip()):
            return name, kind
    return None


def _descriptor(
    target: dict[str, Any] | None, name_param: str | None = None
) -> Target | None:
    """Build the locator ladder from the control that was actually acted on."""
    if not target:
        return None

    role = target.get("role") or None
    name = (target.get("name") or "").strip() or None
    anchor = (target.get("anchor") or "").strip() or None
    frame = (target.get("frame") or "").strip() or None

    rungs: list[Descriptor] = []
    if role and name:
        rungs.append(
            Descriptor(
                strategy="role_name",
                role=role,
                name=name,
                name_param=name_param,
                frame=frame,
            )
        )
    if role and anchor:
        rungs.append(
            Descriptor(
                strategy="role_anchor", role=role, anchor=anchor, frame=frame
            )
        )
    if name:
        # Text alone, unscoped: weakest of the semantic rungs, but it survives a
        # role change -- which is exactly what happens when a <button> becomes a
        # <div onclick> between vendor versions.
        rungs.append(Descriptor(strategy="text", name=name, name_param=name_param))
    if not rungs:
        return None

    return Target(primary=rungs[0], fallbacks=rungs[1:])


def _anchor_score(output_name: str, anchor: str) -> int:
    """How well a label explains an output, weighted by word order.

    ``available_balance`` sits next to a cell reading "Available" and also
    matches one reading "Balance"; the leading word is the discriminating one,
    so earlier tokens count for more. Without this, two outputs holding the
    same value both bind to whichever label happened to come first.
    """
    tokens = [t for t in re.split(r"[^a-z0-9]+", output_name.lower()) if t]
    label = _normalise_label(anchor)
    return sum(
        len(tokens) - position for position, token in enumerate(tokens) if token == label
    )


def _normalise_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _derive_extraction(
    output_name: str, value: str, final_nodes: list[dict]
) -> "Extraction | None":
    """Work out how to read a value again, from the screen that showed it once.

    The run reported "$4820.55". Somewhere on the final screen is a cell holding
    exactly that, and beside it a cell reading "Balance". That pairing is the
    extraction rule, and deriving it from the observed screen beats guessing
    from the output's name.

    Two rules keep this honest. Only *exact* cell matches count, because a value
    found inside a flattened blob gives no reusable handle. And if no anchor is
    found, the output gets no extraction at all -- a regex pinned to the
    recorded value would silently freeze the capability to the record the
    discovery run happened to open.
    """
    if not value or not final_nodes:
        return None

    cleaned = value.strip()

    # How often each piece of text appears on the screen. A column *header*
    # appears once; a column *value* repeats once per row. Only the former is a
    # usable anchor -- "Credit" happens to sit beside the amount on the recorded
    # run, but on a record whose newest row is a debit it points at a different
    # row entirely, and the capability quietly returns the wrong number.
    occurrences: dict[str, int] = {}
    for node in final_nodes:
        text = (node.get("name") or "").strip()
        if text:
            occurrences[text] = occurrences.get(text, 0) + 1

    candidates: list[tuple[int, str, str | None]] = []
    for position, node in enumerate(final_nodes):
        if position == 0:
            continue
        if (node.get("name") or "").strip() != cleaned:
            continue
        anchor = (final_nodes[position - 1].get("name") or "").strip()
        if not anchor or anchor == cleaned:
            continue
        if occurrences.get(anchor, 0) > 1:
            continue  # repeats down a column: data, not a label
        candidates.append((_anchor_score(output_name, anchor), anchor, node.get("frame")))

    if not candidates:
        # Better to declare no extraction than to ship one that is wrong on the
        # second record. A table-positional rule ("Amount column of the first
        # data row") is what this case really wants; see the artifact's notes.
        return None

    candidates.sort(key=lambda c: -c[0])
    _score, anchor, frame = candidates[0]
    return Extraction(method="anchor_cell", anchor=anchor, frame=frame)


def _screen_marker(
    final_nodes: list[dict], data_frame: str | None = None
) -> tuple[str, str | None] | None:
    """A short piece of text that identifies the destination screen.

    Screen titles come first in document order, so the first short name on the
    final screen is a far better identifier than a column header -- a header
    like "Account #" appears on the list screen too, and asserting it lets a
    half-loaded page pass as success.
    """
    # Scope to the frame the data lives in. Chrome and navigation frames carry
    # the institution wordmark and menu, which are present on every screen and
    # therefore identify nothing.
    for node in final_nodes:
        if data_frame and node.get("frame") != data_frame:
            continue
        name = (node.get("name") or "").strip()
        if name and len(name) <= 40 and not name.startswith("$"):
            return name, node.get("frame")
    return None


def _success_condition(outputs: list[OutputSpec], target_url: str) -> Checkpoint:
    """Assert we reached the screen the data actually lives on.

    A URL assertion is a poor proxy in a frameset app: the top-level URL barely
    changes while the content frame navigates. Asserting the presence of a label
    we extract from is both stronger and more meaningful -- it says "the screen
    showing the answer is on screen" rather than "the address looks right".
    """
    marker = _success_condition.marker
    if marker:
        value, frame = marker
        return Checkpoint(
            kind="text_present", value=value, frame=frame, timeout_ms=10_000
        )
    for spec in outputs:
        if spec.extract and spec.extract.method == "anchor_cell" and spec.extract.anchor:
            return Checkpoint(
                kind="text_present",
                value=spec.extract.anchor,
                frame=spec.extract.frame,
                timeout_ms=10_000,
            )
    return Checkpoint(kind="url_contains", value=target_url.rsplit("/", 1)[-1])


_success_condition.marker = None


def _risk_for(tool: str, target: dict[str, Any] | None) -> Risk:
    if tool == "type_secret":
        return Risk.CREDENTIAL
    if tool == "request_human":
        return Risk.IRREVERSIBLE
    if tool in {"type", "select"}:
        return Risk.REVERSIBLE
    if tool == "click":
        label = ((target or {}).get("name") or "").lower()
        if any(k in label for k in ("submit", "confirm", "delete")):
            return Risk.IRREVERSIBLE
        if label in {"continue", "log in", "sign on"}:
            return Risk.MUTATING
    return Risk.READ


def _infer_type(value: Any) -> str:
    text = str(value)
    if re.match(r"^\$?[\d,]+\.\d{2}$", text):
        return "number"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return "date"
    if re.match(r"^\d+$", text):
        return "integer"
    return "string"


def _sensitivity_for(name: str) -> Sensitivity:
    lowered = name.lower()
    if any(k in lowered for k in ("account", "member", "ssn", "customer")):
        return Sensitivity.PII
    return Sensitivity.INTERNAL


def declared_outcomes() -> list[OutcomeSpec]:
    """Every non-success condition the target really produces, in one place.

    Each carries three things: how to detect it, what should have been true
    instead (`expectation`), and what it means for the caller (`message`).
    Keeping the expectation next to the detector is what lets a failure report
    say "the credentials to be accepted" instead of restating that a rule
    matched -- the detector knows what it is looking for, so it is the only
    thing that knows what it was hoping not to find.
    """
    return [
        OutcomeSpec(
            code="record_not_available",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(kind="text_present", value="__step_level__"),
            expectation=(
                "the requested record to be listed for the signed-in operator"
            ),
            message=(
                "No such record is listed for the signed-in operator: it does not "
                "exist, or they are not entitled to see it."
            ),
        ),
        OutcomeSpec(
            code="account_not_found",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(kind="text_present", value="Account Not Found"),
            expectation="the supplied account id to match an existing account",
            message="No account matching the supplied id exists.",
        ),
        OutcomeSpec(
            code="access_denied",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(kind="text_present", value="Access Denied"),
            expectation=(
                "the account to belong to the signed-in customer, so it could be "
                "opened"
            ),
            message="The signed-in operator may not view this account.",
        ),
        OutcomeSpec(
            code="no_transactions",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present", value="No transactions have posted"
            ),
            expectation="the account to have at least one posted transaction",
            message="The account exists but has no posted activity.",
        ),
        OutcomeSpec(
            code="validation_error",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(kind="text_present", value="Please enter a valid amount"),
            expectation=(
                "the supplied amount to be a positive number the application "
                "would accept"
            ),
            message=(
                "Wrong amount: the application would not accept the value "
                "supplied for the transfer. No transfer was staged."
            ),
        ),
        OutcomeSpec(
            code="invalid_account_selection",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present",
                value="source and destination accounts must be different",
            ),
            expectation=(
                "the source and destination accounts to be two different accounts"
            ),
            message="The source and destination accounts must not be the same.",
        ),
        OutcomeSpec(
            code="insufficient_funds",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present", value="Insufficient available funds"
            ),
            expectation=(
                "the source account to hold at least the requested amount"
            ),
            message="The source account does not have enough available balance.",
        ),
        OutcomeSpec(
            code="unexpected_interstitial",
            kind=OutcomeKind.RECOVERABLE,
            detector=Checkpoint(kind="text_present", value="Security Notice"),
            recovery="dismiss",
            recovery_target="Acknowledge",
            max_attempts=2,
            expectation="the next screen to appear without an intervening notice",
            message="An unexpected notice appeared; acknowledge it and continue.",
        ),
        OutcomeSpec(
            code="app_server_error",
            kind=OutcomeKind.FATAL,
            detector=Checkpoint(kind="text_present", value="Internal Server Error"),
            expectation="the application to serve the page rather than an error",
            message="The application returned a server error.",
        ),
        OutcomeSpec(
            code="session_expired",
            kind=OutcomeKind.RECOVERABLE,
            detector=Checkpoint(
                kind="text_present", value="session has ended due to inactivity"
            ),
            expectation="the signed-in session to still be active",
            message="Re-authenticate and resume at the current step.",
            recovery="reauth",
            max_attempts=2,
        ),
        OutcomeSpec(
            code="wrong_password",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present", value="password entered for that username is not correct"
            ),
            expectation=(
                "the password entered at the pause to match the username that "
                "was entered with it"
            ),
            message=(
                "Wrong password: the username is a real account, but the "
                "password typed at the pause does not match it. Nothing was "
                "retried -- repeating a rejected password risks a lockout."
            ),
        ),
        OutcomeSpec(
            code="unknown_username",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present", value="username was not recognised"
            ),
            expectation=(
                "the username entered at the pause to be an account this "
                "institution recognises"
            ),
            message=(
                "Unknown username: no account by that name exists at this "
                "institution, so the password was never the problem. Check the "
                "username before trying again."
            ),
        ),
        # The generic case, kept last: some tenants deliberately refuse to say
        # which half failed. When the screen will not tell us, we do not guess.
        OutcomeSpec(
            code="login_rejected",
            kind=OutcomeKind.BUSINESS,
            detector=Checkpoint(
                kind="text_present", value="could not be verified"
            ),
            expectation=(
                "the username and password entered at the pause to be accepted "
                "and the session to reach the signed-in screen"
            ),
            # The app will not say which of the two was wrong, and neither
            # should we: guessing "wrong password" when the username was the
            # problem sends someone to reset a credential that was fine.
            message=(
                "Wrong credentials: the username or password entered at the "
                "pause was not accepted, so no sign-in happened. Nothing was "
                "retried -- repeating a rejected credential risks a lockout."
            ),
        ),
    ]


def compile_run(
    journal_path: Path,
    capability_id: str,
    product: str = "meridian-core",
    version: str = "1.0.0",
) -> CapabilityArtifact:
    records = [json.loads(line) for line in Path(journal_path).read_text().splitlines()]

    started = next((r for r in records if r["event"] == "run_started"), {})
    finished = next((r for r in records if r["event"] == "run_finished"), {})
    decisions = [r for r in records if r["event"] == "decided"]

    goal = started.get("goal", "")
    target_url = started.get("target", "")
    tenant = "meridian"
    if "/cascade/" in target_url:
        tenant = "cascade"

    steps: list[Step] = []
    inputs: dict[str, ParamSpec] = {}
    index = 0

    # If the run entered credentials field by field, those steps carry real
    # targets and can be satisfied by a vault OR by an operator. A synthetic
    # handoff step on top of them would force a human unconditionally.
    has_field_level_credentials = any(
        r.get("event") == "acted" and r.get("tool") == "type_secret" and r.get("ok")
        for r in records
    )

    # The run always begins by loading the entry point.
    if target_url:
        index += 1
        steps.append(
            Step(
                id=f"s{index}_navigate",
                intent="open the application entry point",
                action=Action(kind="navigate", url=target_url),
                risk=Risk.READ,
                postcondition=Checkpoint(kind="url_contains", value="index.htm"),
            )
        )

    # artifact step id -> the agent step it was recorded during.
    origin_of: dict[str, int] = {}

    for record in records:
        event = record["event"]

        if event == "intervention_raised":
            kind = record.get("kind")
            if kind == "human_action" and any(
                r.get("event") == "acted"
                and r.get("tool") == "request_human"
                and r.get("ok")
                for r in records
            ):
                continue  # the request_human action already represents this
            index += 1
            if kind == "credential_handoff" and has_field_level_credentials:
                index -= 1  # already represented by the type_secret steps
                continue
            if kind == "credential_handoff":
                steps.append(
                    Step(
                        id=f"s{index}_authenticate",
                        intent="sign in to the application as the servicing operator",
                        action=Action(
                            kind="credential_entry",
                            instruction="operator enters the credentials on the live session",
                        ),
                        risk=Risk.CREDENTIAL,
                        postcondition=Checkpoint(
                            kind="url_contains", value="main.htm", timeout_ms=15_000
                        ),
                    )
                )
            else:
                steps.append(
                    Step(
                        id=f"s{index}_operator_action",
                        intent=record.get("reason", "an operator must perform this step"),
                        action=Action(
                            kind="human_action",
                            instruction=record.get("reason", ""),
                        ),
                        risk=Risk.IRREVERSIBLE,
                    )
                )
            continue

        if event != "acted" or not record.get("ok"):
            continue  # failed attempts are evidence, not part of the flow

        tool = record["tool"]
        args = record.get("args", {})
        target = record.get("target")
        intent = args.get("intent") or f"{tool} step"

        if tool == "type_secret":
            secret = args.get("secret_name", "")
            anchor = ((target or {}).get("anchor") or "").strip().lower()
            if secret and secret not in anchor:
                # The captured descriptor does not mention the credential it is
                # for, so it was taken after the screen moved on -- which happens
                # whenever a person signs in by hand and the agent reports the
                # remaining fields afterwards. Discard it entirely and describe
                # the field the login screen actually has; keeping any part of a
                # descriptor that points at a later screen makes replay click
                # something arbitrary.
                target = {
                    "role": "textbox",
                    "name": "",
                    "anchor": f"{secret.capitalize()}:",
                    "frame": "",
                }
            # A real step with a real target, so replay can either inject the
            # value from a vault or hand the field to an operator.
            index += 1
            steps.append(
                Step(
                    id=f"s{index}_enter_{_slug(args.get('secret_name', 'credential'))}",
                    intent=intent,
                    action=Action(
                        kind="type_secret", secret_ref=args.get("secret_name")
                    ),
                    target=_descriptor(target),
                    risk=Risk.CREDENTIAL,
                )
            )
            continue

        index += 1
        step_id = f"s{index}_{_slug(intent)[:34]}"
        # Which agent step produced this artifact step. Needed to work out
        # which recorded screen corresponds to which point in the replay.
        origin_of[step_id] = record.get("step", 0)
        action: Action
        name_param: str | None = None

        if tool == "click":
            action = Action(kind="click")
            # If the control's visible name is itself a data value -- a link
            # whose text is the account number -- bind it to a parameter rather
            # than freezing the recorded value into the locator.
            hit = _looks_like_data((target or {}).get("name", ""))
            if hit:
                name_param, param_type = hit
                inputs.setdefault(
                    name_param,
                    ParamSpec(
                        name=name_param,
                        type=param_type,
                        description="identifies which record this capability opens",
                        example=(target or {}).get("name"),
                        sensitivity=_sensitivity_for(name_param),
                    ),
                )
        elif tool in {"type", "select"}:
            raw = args.get("text") if tool == "type" else args.get("value")
            label = (target or {}).get("anchor") or (target or {}).get("name") or ""
            param_name, param_type = _param_for(str(raw), label)
            inputs.setdefault(
                param_name,
                ParamSpec(
                    name=param_name,
                    type=param_type,
                    description=f"value for {label or 'the field'}",
                    example=str(raw),
                    sensitivity=_sensitivity_for(param_name),
                ),
            )
            action = Action(
                kind="type" if tool == "type" else "select", param=param_name
            )
        elif tool == "request_human":
            action = Action(kind="human_action", instruction=args.get("instruction"))
        else:
            continue

        steps.append(
            Step(
                id=step_id,
                intent=intent,
                action=action,
                target=_descriptor(target, name_param=name_param),
                risk=_risk_for(tool, target),
                # If the control that selects a record is missing, the record is
                # not available to this operator. That is an answer the caller
                # needs, not a defect in the capability.
                on_unresolved="record_not_available" if name_param else None,
            )
        )

    # ---- the contract half -------------------------------------------------

    raw_outputs = finished.get("outputs") or {}
    final_nodes = next(
        (r["nodes"] for r in records if r["event"] == "final_screen"), []
    )
    last_step = steps[-1].id if steps else None

    def _step_before(agent_step: int) -> str | None:
        """The artifact step after which a given observation was taken.

        The agent observes at the *start* of a step, so the screen recorded at
        agent step k is the state left behind by agent step k-1. Replay
        snapshots after each step, so this is what lines the two up.
        """
        latest = None
        for step_id, origin in origin_of.items():
            if origin < agent_step:
                latest = step_id
        return latest or (steps[0].id if steps else None)

    # Every screen the run recorded, oldest first, each tagged with the
    # artifact step that leaves the page in that state.
    screens: list[tuple[str | None, list[dict]]] = []
    for record in records:
        if record["event"] == "observed" and record.get("screen"):
            screens.append((_step_before(record.get("step", 0)), record["screen"]))
        elif record["event"] == "final_screen":
            screens.append((last_step, record["nodes"]))

    def _resolve_output(name: str, value: str):
        """Find the most recent screen that yields a real rule for this value.

        Searching newest-first matters: a value visible both mid-flow and at the
        end should be read at the end, where the run finished. But a value that
        only ever existed mid-flow -- a balance noted *before* a transfer -- is
        found further back, and is tied to the step that was on screen then.
        Without this, such an output is declared and never returned.
        """
        for step_id, nodes in reversed(screens):
            rule = _derive_extraction(name, value, nodes)
            if rule is not None:
                return rule, (step_id or last_step)
        return None, last_step

    outputs = []
    for name, value in raw_outputs.items():
        rule, at_step = _resolve_output(name, str(value))
        outputs.append(
            OutputSpec(
                name=name,
                type=_infer_type(value),
                description=f"observed on the recorded run as: {value}",
                sensitivity=_sensitivity_for(name),
                extracted_at_step=at_step,
                extract=rule,
            )
        )

    # Outcomes the target app really produces, with how to detect each.
    # These are the screens the target's own tests pin down.
    outcomes = declared_outcomes()

    provenance = Provenance(
        discovered_by_model=started.get("model", "unknown"),
        run_id=started.get("run_id", ""),
        created_at=utc_now(),
        recorded_on_tenant=tenant,
        evidence_ref=str(Path(journal_path).parent),
        request_ids=[d["request_id"] for d in decisions if d.get("request_id")],
        model_calls=finished.get("model_calls", len(decisions)),
        tokens_in=finished.get("tokens_in", 0),
        tokens_out=finished.get("tokens_out", 0),
    )

    data_frame = next(
        (o.extract.frame for o in outputs if o.extract and o.extract.frame), None
    )
    marker = _screen_marker(final_nodes, data_frame)
    _success_condition.marker = marker
    if marker and steps:
        value, frame = marker
        steps[-1].postcondition = Checkpoint(
            kind="text_present", value=value, frame=frame, timeout_ms=12_000
        )

    human_signed_in = any(
        r.get("event") == "intervention_raised"
        and r.get("kind") == "credential_handoff"
        for r in records
    )
    if human_signed_in:
        credential_steps = [s for s in steps if s.action.kind == "type_secret"]
        if credential_steps:
            credential_steps[-1].action.submit_form = True
            credential_steps[-1].postcondition = Checkpoint(
                kind="text_absent", value="Online Servicing Sign On", timeout_ms=12_000
            )

    return CapabilityArtifact(
        capability=CapabilityMeta(
            id=capability_id,
            name=capability_id.replace(".", " ").replace("_", " ").title(),
            summary=goal,
            version=version,
        ),
        app=AppProfile(product=product, surface_kind="legacy_web"),
        inputs=list(inputs.values()),
        outputs=outputs,
        steps=steps,
        outcomes=outcomes,
        success=_success_condition(outputs, target_url),
        provenance=provenance,
    )
