"""Tests for deterministic replay.

These drive the resolver, the extractor and the result contract directly with
synthetic snapshots -- no browser, no model, no network -- so they pin the
decision logic rather than the plumbing. The end-to-end run against the live
target is exercised separately by the demo path in the README.

What they hold in place:

  * a business outcome is never reported as a failure, and vice versa
  * ambiguity is an error rather than a guess
  * a parameterised locator follows the input, not the recorded value
  * confidence floors are enforced
  * which ladder rung matched is recorded, because that is the drift signal
"""

from __future__ import annotations

import pytest

from understudy.artifact import Descriptor, Extraction, Target
from understudy.replay import CONFIDENCE, ReplayResult, extract, resolve
from understudy.surface import UINode, UISnapshot


def node(ref, role, name="", anchor="", frame="contentframe"):
    return UINode(ref=ref, role=role, name=name, anchor=anchor, frame=frame)


def snap(*nodes, url="http://127.0.0.1:8099/meridian/main.htm"):
    return UISnapshot(url=url, title="t", nodes=list(nodes), frame_urls=[url])


# ------------------------------------------------------------------ resolving


def test_primary_rung_wins_and_is_recorded_as_rung_zero():
    screen = snap(node(1, "link", "13566"), node(2, "link", "13344"))
    target = Target(primary=Descriptor(strategy="role_name", role="link", name="13566"))
    found, rung, confidence = resolve(screen, target, {})
    assert found.ref == 1
    assert rung == 0
    assert confidence == CONFIDENCE["role_name"]


def test_parameterised_locator_follows_the_input_not_the_recording():
    """The recorded run opened 13566; asking for 13344 must open 13344."""
    screen = snap(node(1, "link", "13566"), node(2, "link", "13344"))
    target = Target(
        primary=Descriptor(
            strategy="role_name", role="link", name="13566", name_param="account_id"
        )
    )
    found, _rung, _c = resolve(screen, target, {"account_id": "13344"})
    assert found.ref == 2, "the frozen recorded value must not win"


def test_falls_through_to_the_anchor_rung_when_the_name_is_gone():
    """An unnamed input is located by the text beside it."""
    screen = snap(node(1, "textbox", "", anchor="Username:"))
    target = Target(
        primary=Descriptor(strategy="role_name", role="textbox", name="User Name"),
        fallbacks=[Descriptor(strategy="role_anchor", role="textbox", anchor="Username:")],
    )
    found, rung, confidence = resolve(screen, target, {})
    assert found.ref == 1
    assert rung == 1, "using a fallback is what the drift signal counts"
    assert confidence == CONFIDENCE["role_anchor"]


def test_ambiguity_is_an_error_not_a_coin_flip():
    screen = snap(node(1, "link", "Details"), node(2, "link", "Details"))
    target = Target(primary=Descriptor(strategy="role_name", role="link", name="Details"))
    with pytest.raises(LookupError) as caught:
        resolve(screen, target, {})
    assert "ambiguous" in str(caught.value)


def test_ordinal_disambiguates_when_it_is_declared():
    screen = snap(node(1, "link", "Details"), node(2, "link", "Details"))
    target = Target(
        primary=Descriptor(strategy="role_name", role="link", name="Details", ordinal=1)
    )
    found, _rung, _c = resolve(screen, target, {})
    assert found.ref == 2


def test_confidence_floor_is_enforced():
    """A weak rung must not satisfy a step that demands strong evidence."""
    screen = snap(node(1, "cell", "Submit Transfer"))
    target = Target(
        primary=Descriptor(strategy="coords", name="Submit Transfer"),
        min_confidence=0.9,
    )
    with pytest.raises(LookupError) as caught:
        resolve(screen, target, {})
    assert "below floor" in str(caught.value)


def test_frame_scoping_excludes_a_same_named_control_elsewhere():
    screen = snap(
        node(1, "link", "Accounts Overview", frame="navframe"),
        node(2, "link", "Accounts Overview", frame="contentframe"),
    )
    target = Target(
        primary=Descriptor(
            strategy="role_name", role="link", name="Accounts Overview", frame="navframe"
        )
    )
    found, _rung, _c = resolve(screen, target, {})
    assert found.ref == 1


def test_unresolvable_target_reports_every_rung_it_tried():
    screen = snap(node(1, "link", "Something else"))
    target = Target(
        primary=Descriptor(strategy="role_name", role="link", name="13566"),
        fallbacks=[Descriptor(strategy="text", name="13566")],
    )
    with pytest.raises(LookupError) as caught:
        resolve(screen, target, {})
    message = str(caught.value)
    assert "rung 0" in message and "rung 1" in message


# ----------------------------------------------------------------- extraction


def test_anchor_cell_reads_the_value_beside_the_label():
    screen = snap(
        node(1, "cell", "Balance"),
        node(2, "cell", "$4820.55"),
        node(3, "cell", "Available"),
        node(4, "cell", "$4700.00"),
    )
    assert extract(screen, Extraction(method="anchor_cell", anchor="Balance")) == "$4820.55"
    assert extract(screen, Extraction(method="anchor_cell", anchor="Available")) == "$4700.00"


def test_extraction_returns_none_when_the_label_is_absent():
    screen = snap(node(1, "cell", "Something"))
    assert extract(screen, Extraction(method="anchor_cell", anchor="Balance")) is None


def test_extraction_is_frame_scoped():
    screen = snap(
        node(1, "cell", "Balance", frame="navframe"),
        node(2, "cell", "WRONG", frame="navframe"),
        node(3, "cell", "Balance", frame="contentframe"),
        node(4, "cell", "$4820.55", frame="contentframe"),
    )
    spec = Extraction(method="anchor_cell", anchor="Balance", frame="contentframe")
    assert extract(screen, spec) == "$4820.55"


# ------------------------------------------------------------------ contract


@pytest.mark.parametrize(
    "status,code",
    [("ok", 0), ("business_outcome", 2), ("needs_human", 3), ("failed", 4)],
)
def test_exit_codes_are_distinct_per_status(status, code):
    """A caller must be able to tell an answer from a defect by exit code alone."""
    assert ReplayResult(status=status).exit_code == code


def test_a_business_outcome_is_not_a_failure():
    result = ReplayResult(status="business_outcome", outcome_code="record_not_available")
    assert result.exit_code != ReplayResult(status="failed").exit_code
    assert result.outcome_code == "record_not_available"


def test_fallback_rate_is_the_drift_signal():
    clean = ReplayResult(status="ok", locator_rungs={"s1": 0, "s2": 0, "s3": 0})
    drifting = ReplayResult(status="ok", locator_rungs={"s1": 0, "s2": 1, "s3": 2})
    assert clean.fallback_rate == 0.0
    assert drifting.fallback_rate == pytest.approx(2 / 3, abs=0.01)


def test_failure_reports_step_expected_and_observed():
    result = ReplayResult(
        status="failed",
        step="s5_open_account",
        expected="text_present='Balance'",
        observed="url=.../main.htm",
    )
    rendered = result.render()
    assert "s5_open_account" in rendered
    assert "expected" in rendered and "observed" in rendered


def test_result_always_reports_zero_model_calls():
    """The headline property of the production path."""
    assert "model calls 0" in ReplayResult(status="ok").render()


# ------------------------------------------------------- operator in replay


class _Surface:
    """Minimal surface for exercising the operator seam without a browser."""

    def __init__(self, url="http://127.0.0.1:8099/meridian/index.htm"):
        self.url = url
        self.typed: list[str] = []

    def observe(self):
        return snap(node(1, "textbox", "", anchor="Username:"), url=self.url)

    def navigate(self, url):
        self.url = url

    def type_text(self, ref, text):
        self.typed.append(text)
        return "typed"

    def click(self, ref):
        return "clicked"


def _artifact_with_credential_step():
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Risk, Step,
    )

    return CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[
            Step(
                id="s1_enter_username",
                intent="Enter username",
                action=Action(kind="type_secret", secret_ref="username"),
                target=Target(
                    primary=Descriptor(
                        strategy="role_anchor", role="textbox", anchor="Username:"
                    )
                ),
                risk=Risk.CREDENTIAL,
            )
        ],
    )


def test_credential_step_is_declared_before_the_run():
    art = _artifact_with_credential_step()
    assert art.requires_operator
    assert art.operator_steps == ["s1_enter_username"]
    assert art.tool_signature()["requires_operator"] is True


def test_unattended_replay_stops_at_a_credential_step():
    """No operator, no vault value: stop rather than proceed blind."""
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    engine = ReplayEngine(
        _Surface(), default_policy("http://127.0.0.1:8099/"),
        secrets={}, operator=None, on_event=lambda _m: None,
    )
    result = engine.run(_artifact_with_credential_step(), {})
    assert result.status == "needs_human"
    assert result.exit_code == 3


def test_attended_replay_hands_control_to_the_operator():
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    surface = _Surface()
    calls: list[str] = []

    def operator(step_id, _instruction):
        calls.append(step_id)
        surface.url = "http://127.0.0.1:8099/meridian/main.htm"
        return True

    engine = ReplayEngine(
        surface, default_policy("http://127.0.0.1:8099/"),
        secrets={}, operator=operator, on_event=lambda _m: None,
    )
    result = engine.run(_artifact_with_credential_step(), {})
    assert calls == ["s1_enter_username"]
    assert result.status == "ok"
    assert surface.typed == [], "the system must type no credential in handoff mode"


def test_operator_declining_stops_the_run():
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    engine = ReplayEngine(
        _Surface(), default_policy("http://127.0.0.1:8099/"),
        secrets={}, operator=lambda _s, _i: False, on_event=lambda _m: None,
    )
    result = engine.run(_artifact_with_credential_step(), {})
    assert result.status == "needs_human"


def test_vault_value_is_used_without_pausing():
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    surface = _Surface()
    engine = ReplayEngine(
        surface, default_policy("http://127.0.0.1:8099/"),
        secrets={"username": "jsmith"},
        operator=lambda _s, _i: pytest.fail("must not pause when a vault value exists"),
        on_event=lambda _m: None,
    )
    result = engine.run(_artifact_with_credential_step(), {})
    assert result.status == "ok"
    assert surface.typed == ["jsmith"]


# --------------------------------------------------- recovery semantics


def test_dismiss_does_not_repeat_the_step():
    """An interstitial appears AFTER a step succeeds.

    Repeating the step would be wrong in general and dangerous on a mutating
    one -- a dismissed confirmation followed by a repeat is a double submission.
    """
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Checkpoint,
        OutcomeKind, OutcomeSpec, Step,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    executed: list[str] = []

    class S:
        url = "http://127.0.0.1:8099/meridian/main.htm"
        showing_dialog = True

        def observe(self):
            text = "Security Notice" if self.showing_dialog else "clear"
            snapshot = snap(node(1, "button", "Acknowledge"), url=self.url)
            snapshot.text = text
            return snapshot

        def navigate(self, url):
            executed.append(f"navigate:{url}")

        def click(self, ref):
            executed.append("click")
            self.showing_dialog = False

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[Step(id="s1", intent="go", action=Action(kind="navigate", url="http://127.0.0.1:8099/x"))],
        outcomes=[
            OutcomeSpec(
                code="interstitial",
                kind=OutcomeKind.RECOVERABLE,
                detector=Checkpoint(kind="text_present", value="Security Notice"),
                recovery="dismiss",
                recovery_target="Acknowledge",
            )
        ],
    )

    engine = ReplayEngine(S(), default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None)
    result = engine.run(artifact, {})
    assert result.status == "ok"
    # navigate ran once; the dismiss click is the only extra action.
    assert executed.count("navigate:http://127.0.0.1:8099/x") == 1
    assert executed.count("click") == 1


def test_a_business_outcome_reports_its_own_expectation():
    """`expected` must name the condition, not restate that a rule matched.

    "the credentials to be accepted" tells an operator what to do next.
    "to complete without the application reporting a business condition" tells
    them nothing they did not already know from the status line.
    """
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Checkpoint,
        OutcomeKind, OutcomeSpec, Step,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    class S:
        url = "http://127.0.0.1:8099/meridian/login.htm"

        def observe(self):
            snapshot = snap(
                node(1, "StaticText", "The username and password could not be verified."),
                node(2, "LayoutTableCell", "Sign On The username and password could "
                                           "not be verified. Test environment."),
                url=self.url,
            )
            snapshot.text = "could not be verified"
            return snapshot

        def navigate(self, url):
            pass

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[Step(id="s1", intent="sign in",
                    action=Action(kind="navigate", url="http://127.0.0.1:8099/x"))],
        outcomes=[
            OutcomeSpec(
                code="login_rejected",
                kind=OutcomeKind.BUSINESS,
                detector=Checkpoint(kind="text_present", value="could not be verified"),
                expectation="the username and password entered at the pause to be accepted",
                message="The supplied credentials were rejected.",
            )
        ],
    )

    result = ReplayEngine(S(), default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None).run(artifact, {})

    assert result.status == "business_outcome"
    assert "username and password entered at the pause to be accepted" in result.expected
    assert "business condition" not in (result.expected or "")
    # The app's own sentence, and the tightest node carrying it -- not the
    # table cell that happens to wrap half the page.
    assert "The username and password could not be verified." in result.observed
    assert "Test environment" not in result.observed.split("|")[0]


def test_recovery_is_bounded_then_escalates():
    """A condition that will not clear must escalate, not loop forever."""
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Checkpoint,
        OutcomeKind, OutcomeSpec, Step,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    attempts = {"n": 0}

    class S:
        url = "http://127.0.0.1:8099/meridian/main.htm"

        def observe(self):
            snapshot = snap(node(1, "cell", "x"), url=self.url)
            snapshot.text = "session has ended due to inactivity"
            return snapshot

        def navigate(self, url):
            attempts["n"] += 1

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[Step(id="s1", intent="go", action=Action(kind="navigate", url="http://127.0.0.1:8099/x"))],
        outcomes=[
            OutcomeSpec(
                code="session_expired",
                kind=OutcomeKind.RECOVERABLE,
                detector=Checkpoint(kind="text_present", value="session has ended"),
                recovery="retry",
                max_attempts=2,
            )
        ],
    )

    engine = ReplayEngine(S(), default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None)
    result = engine.run(artifact, {})
    assert result.status == "needs_human"
    assert "persisted after 2" in result.reason
    assert attempts["n"] <= 4, "recovery must be bounded"


def test_fatal_outcome_stops_with_a_debuggable_error():
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Checkpoint,
        OutcomeKind, OutcomeSpec, Step,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    class S:
        url = "http://127.0.0.1:8099/meridian/main.htm"

        def observe(self):
            snapshot = snap(node(1, "cell", "x"), url=self.url)
            snapshot.text = "500 Internal Server Error"
            return snapshot

        def navigate(self, url):
            pass

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[Step(id="s1", intent="go", action=Action(kind="navigate", url="http://127.0.0.1:8099/x"))],
        outcomes=[
            OutcomeSpec(
                code="app_server_error",
                kind=OutcomeKind.FATAL,
                detector=Checkpoint(kind="text_present", value="Internal Server Error"),
            )
        ],
    )

    result = ReplayEngine(S(), default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None).run(artifact, {})
    assert result.status == "failed"
    assert result.exit_code == 4
    assert result.step == "s1"
    # Not just the code: what matched, where, and what was on screen.
    assert "app_server_error" in result.observed
    assert "url=" in result.observed
    assert "s1" not in result.expected and "go" in result.expected


def test_detectors_see_text_that_is_not_a_node_name():
    """A condition rendered as plain text must still be detected."""
    from understudy.artifact import Checkpoint
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    screen = snap(node(1, "cell", "unrelated"))
    screen.text = "Security Notice\nplease acknowledge"
    engine = ReplayEngine(None, default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None)
    assert engine._check(screen, Checkpoint(kind="text_present", value="Security Notice"))


# ------------------------------------------- structured result completeness


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_every_non_success_carries_step_expected_and_observed(status):
    """The brief asks for what step, what was expected, what was observed."""
    result = ReplayResult(
        status=status,
        step="s5_open_account",
        expected="link named {account_id}",
        observed="url=.../main.htm | controls: link:13344, link:13901",
    )
    assert result.step and result.expected and result.observed
    rendered = result.render()
    for fragment in ("s5_open_account", "expected", "observed"):
        assert fragment in rendered


def test_resolution_failure_lists_what_was_on_screen():
    """A bare 'not found' is not debuggable; say what was actually there."""
    from understudy.artifact import (
        Action, AppProfile, CapabilityArtifact, CapabilityMeta, Step,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    class S:
        url = "http://127.0.0.1:8099/meridian/main.htm"

        def observe(self):
            return snap(
                node(1, "link", "13344"), node(2, "link", "13901"), url=self.url
            )

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        steps=[
            Step(
                id="s1_open",
                intent="open the record",
                action=Action(kind="click"),
                target=Target(
                    primary=Descriptor(strategy="role_name", role="link", name="99999")
                ),
            )
        ],
    )

    result = ReplayEngine(S(), default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None).run(artifact, {})
    assert result.status == "failed"
    assert "rung 0" in result.expected
    assert "13344" in result.observed, "must show what the screen offered instead"


def test_missing_input_names_what_was_expected_and_supplied():
    from understudy.artifact import (
        AppProfile, CapabilityArtifact, CapabilityMeta, ParamSpec,
    )
    from understudy.policy import default_policy
    from understudy.replay import ReplayEngine

    artifact = CapabilityArtifact(
        capability=CapabilityMeta(id="x.y", name="X", summary="s"),
        app=AppProfile(product="p"),
        inputs=[ParamSpec(name="account_id", type="integer")],
    )
    result = ReplayEngine(None, default_policy("http://127.0.0.1:8099/"),
                          on_event=lambda _m: None).run(artifact, {})
    assert result.status == "failed"
    assert "account_id" in result.expected
    assert "nothing" in result.observed
