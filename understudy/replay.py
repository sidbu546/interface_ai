"""Deterministic replay: the production execution path.

No model. No randomness. No fixed sleeps. Given the same artifact and the same
inputs, this does the same thing every time -- which is the entire point of
having recorded the run at all.

Three things make it deterministic rather than merely repeatable:

**Ordered locator resolution.** Each step carries a ranked ladder of semantic
descriptors. Replay tries them top-down, scores the match, and refuses anything
below the step's confidence floor. Two candidates with no way to tell them
apart is an error, not a coin flip.

**Conditions, never sleeps.** Every wait is a wait *for* something, with an
explicit timeout. A fixed sleep is how flakiness gets designed in.

**Detectors after every step.** The screen is checked against the artifact's
declared outcomes before the next step runs, so a business outcome is reported
as an answer and a failure is caught where it happened rather than four steps
later as something incomprehensible.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .artifact import (
    CapabilityArtifact,
    Checkpoint,
    Descriptor,
    Extraction,
    OutcomeKind,
    Risk,
    Step,
    Target,
)
from pathlib import Path

from .policy import Policy, PolicyViolation
from .surface import Surface, UINode, UISnapshot

# How much a match by each strategy is worth. Ordered by how much meaning the
# evidence carries: a role plus an accessible name is strong; raw geometry is
# a last resort and is policy-gated.
CONFIDENCE = {
    "role_name": 1.00,
    "role_anchor": 0.90,
    "text": 0.80,
    "ordinal": 0.65,
    "coords": 0.45,
}


# ----------------------------------------------------------------- results


@dataclass
class ReplayResult:
    """The contract a calling agent sees.

    The four statuses are deliberately distinct. Conflating a business outcome
    with a failure is, per the brief, the most common design mistake here: "no
    such account" is a legitimate answer the caller needs, not a crash.
    """

    status: str  # ok | business_outcome | needs_human | failed
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome_code: str | None = None
    reason: str = ""
    step: str | None = None
    expected: str | None = None
    observed: str | None = None
    steps_run: int = 0
    duration_ms: int = 0
    # Which rung of each ladder actually matched. A capability that starts
    # leaning on fallbacks still works, but it is drifting -- and this is the
    # signal that says so before it breaks.
    locator_rungs: dict[str, int] = field(default_factory=dict)
    evidence_ref: str | None = None

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "business_outcome": 2, "needs_human": 3, "failed": 4}[
            self.status
        ]

    @property
    def fallback_rate(self) -> float:
        if not self.locator_rungs:
            return 0.0
        used = sum(1 for rung in self.locator_rungs.values() if rung > 0)
        return round(used / len(self.locator_rungs), 3)

    def render(self) -> str:
        lines = [f"status      {self.status}"]
        if self.outcome_code:
            lines.append(f"outcome     {self.outcome_code}")
        if self.reason:
            lines.append(f"reason      {self.reason}")
        if self.step:
            label = "failed at" if self.status == "failed" else "determined at"
            lines.append(f"{label:<11} {self.step}")
        if self.expected:
            lines.append(f"  expected  {self.expected}")
        if self.observed:
            lines.append(f"  observed  {self.observed}")
        if self.outputs:
            lines.append("outputs")
            for key, value in self.outputs.items():
                lines.append(f"  {key:<28} {value}")
        lines.append(f"steps run   {self.steps_run}")
        lines.append(f"duration    {self.duration_ms} ms")
        lines.append(f"model calls 0")
        if self.locator_rungs:
            lines.append(f"fallbacks   {self.fallback_rate:.0%} of steps")
        if self.evidence_ref:
            lines.append(f"evidence    {self.evidence_ref}")
        return "\n".join(lines)


class StepFailure(Exception):
    """A hard failure. `code` names the declared fatal outcome, when one matched.

    A caller filtering on `outcome_code` needs to tell a known outage from a
    checkpoint that simply never arrived; burying that distinction in the prose
    of `observed` makes it unqueryable.
    """

    def __init__(
        self, step_id: str, expected: str, observed: str, code: str | None = None
    ):
        super().__init__(f"{step_id}: expected {expected}, observed {observed}")
        self.step_id, self.expected, self.observed = step_id, expected, observed
        self.code = code


class BusinessOutcome(Exception):
    """A declared, legitimate non-success answer -- not a failure.

    It still carries where it was determined, what the step was trying to do,
    and what the screen showed. "No such account" is an answer, but a caller
    debugging an unexpected one needs the same detail as a failure.
    """

    def __init__(
        self,
        code: str,
        reason: str,
        step_id: str = "",
        expected: str = "",
        observed: str = "",
    ):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.step_id, self.expected, self.observed = step_id, expected, observed


class NeedsHuman(Exception):
    def __init__(
        self, step_id: str, reason: str, expected: str = "", observed: str = ""
    ):
        super().__init__(reason)
        self.step_id, self.reason = step_id, reason
        self.expected = expected or "an operator to complete this step"
        self.observed = observed or "no operator is attached to this run"


# ---------------------------------------------------------------- matching


def _normalise(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _matches(node: UINode, rung: Descriptor, params: dict[str, Any]) -> bool:
    if rung.frame and _normalise(node.frame) != _normalise(rung.frame):
        return False
    if rung.role and _normalise(node.role) != _normalise(rung.role):
        return False

    # A parameterised name is substituted at resolve time, so the artifact is
    # not pinned to whichever record the discovery run happened to open.
    wanted = rung.name
    if rung.name_param:
        if rung.name_param not in params:
            return False
        wanted = str(params[rung.name_param])

    if wanted is not None:
        if rung.name_match == "exact":
            if node.name != wanted:
                return False
        elif rung.name_match == "contains":
            if _normalise(wanted) not in _normalise(node.name):
                return False
        else:
            if _normalise(node.name) != _normalise(wanted):
                return False

    if rung.anchor and _normalise(node.anchor) != _normalise(rung.anchor):
        return False
    return True


def resolve(
    snapshot: UISnapshot, target: Target, params: dict[str, Any]
) -> tuple[UINode, int, float]:
    """Walk the ladder top-down. Returns (node, rung_index, confidence)."""
    problems: list[str] = []

    for index, rung in enumerate([target.primary, *target.fallbacks]):
        candidates = [n for n in snapshot.nodes if _matches(n, rung, params)]
        confidence = CONFIDENCE.get(rung.strategy, 0.5)

        if not candidates:
            problems.append(f"rung {index} ({rung.describe()}): no match")
            continue
        if len(candidates) > 1:
            if rung.ordinal is not None and rung.ordinal < len(candidates):
                return candidates[rung.ordinal], index, confidence * 0.9
            # Ambiguity is a defect, not something to guess through.
            problems.append(
                f"rung {index} ({rung.describe()}): {len(candidates)} candidates, ambiguous"
            )
            continue
        if confidence < target.min_confidence:
            problems.append(
                f"rung {index} ({rung.describe()}): confidence {confidence} below floor "
                f"{target.min_confidence}"
            )
            continue
        return candidates[0], index, confidence

    raise LookupError("; ".join(problems) or "no descriptor resolved")


# --------------------------------------------------------------- extraction


def extract(snapshot: UISnapshot, spec: Extraction) -> str | None:
    nodes = snapshot.nodes
    if spec.frame:
        scoped = [n for n in nodes if _normalise(n.frame) == _normalise(spec.frame)]
        nodes = scoped or nodes

    if spec.method == "anchor_cell":
        for position, node in enumerate(nodes):
            if _normalise(node.name) == _normalise(spec.anchor):
                for following in nodes[position + 1 :]:
                    if (following.name or "").strip():
                        return following.name.strip()
        return None

    if spec.method == "regex" and spec.pattern:
        blob = "\n".join(n.name for n in nodes if n.name)
        found = re.search(spec.pattern, blob)
        if not found:
            return None
        return found.group(spec.group) if spec.group else found.group(0)

    if spec.method == "node_text" and spec.anchor:
        for node in nodes:
            if _normalise(spec.anchor) in _normalise(node.name):
                return node.name.strip()
    return None


# ------------------------------------------------------------------ engine


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        secrets: dict[str, str] | None = None,
        operator: Callable[[str, str], bool] | None = None,
        on_event: Callable[[str], None] = print,
        evidence_dir: "Path | None" = None,
    ):
        self.surface = surface
        self.policy = policy
        self.secrets = secrets or {}
        # Supplied only when a person is attached. Absent means unattended, and
        # any step needing a human stops the run rather than being skipped.
        self.operator = operator
        self.on_event = on_event
        self._operator_signed_in = False
        # Set once an operator completes a sign-in, so the remaining steps of
        # that credential block are skipped rather than failing to resolve.
        self._skip_completed_by_operator = False
        # The artifact currently running, so a failure raised deep in step
        # execution can look up what the declared outcome expected.
        self._artifact: CapabilityArtifact | None = None
        self.evidence_dir = evidence_dir

    def _wait(self, milliseconds: int) -> None:
        """Pause via the surface. Falls back for surfaces without a clock."""
        waiter = getattr(self.surface, "wait", None)
        if callable(waiter):
            waiter(milliseconds)
        else:
            time.sleep(milliseconds / 1000)

    # ------------------------------------------------------------ checkpoint

    def _check(self, snapshot: UISnapshot, checkpoint: Checkpoint) -> bool:
        if checkpoint.kind == "url_contains":
            urls = [snapshot.url, *getattr(snapshot, "frame_urls", [])]
            return any(checkpoint.value in url for url in urls)
        blob = getattr(snapshot, "text", "") or "\n".join(
            n.name for n in snapshot.nodes if n.name
        )
        if checkpoint.kind == "text_present":
            return _normalise(checkpoint.value) in _normalise(blob)
        if checkpoint.kind == "text_absent":
            return _normalise(checkpoint.value) not in _normalise(blob)
        if checkpoint.kind == "node_present":
            return any(_normalise(checkpoint.value) == _normalise(n.name) for n in snapshot.nodes)
        return False

    def _await(self, checkpoint: Checkpoint) -> tuple[bool, UISnapshot]:
        """Wait *for a condition*, with a timeout. Never a fixed sleep."""
        deadline = time.time() + checkpoint.timeout_ms / 1000
        snapshot = self.surface.observe()
        while time.time() < deadline:
            if self._check(snapshot, checkpoint):
                return True, snapshot
            self._wait(200)
            snapshot = self.surface.observe()
        return self._check(snapshot, checkpoint), snapshot

    # -------------------------------------------------------------- outcomes

    def _declared(self, code: str):
        """The declared outcome with this code, if the running artifact has one."""
        for outcome in getattr(self._artifact, "outcomes", []) or []:
            if outcome.code == code:
                return outcome
        return None

    def _detect(self, artifact: CapabilityArtifact, snapshot: UISnapshot):
        """Check the screen against every declared outcome, after every step."""
        for outcome in artifact.outcomes:
            if outcome.detector.value == "__step_level__":
                continue  # raised by a step, not detected on screen
            if self._check(snapshot, outcome.detector):
                return outcome
        return None

    # ------------------------------------------------------------------ run

    def run(
        self, artifact: CapabilityArtifact, params: dict[str, Any]
    ) -> ReplayResult:
        started = time.time()
        self._artifact = artifact
        rungs: dict[str, int] = {}
        seen: dict[str, UISnapshot] = {}
        steps_run = 0

        missing = [
            p.name for p in artifact.inputs if p.required and p.name not in params
        ]
        if missing:
            return ReplayResult(
                status="failed",
                reason=f"missing required input(s): {', '.join(missing)}",
                step="input_validation",
                expected="inputs: "
                + ", ".join(f"{p.name}:{p.type}" for p in artifact.inputs),
                observed="supplied: " + (", ".join(sorted(params)) or "nothing"),
                duration_ms=int((time.time() - started) * 1000),
            )

        try:
            for step in artifact.steps:
                if self._skip_completed_by_operator:
                    # The operator already did this part by hand. Skip while the
                    # steps are unresolvable; the first one that resolves means
                    # we are past the block they completed.
                    if not self._resolves(step, params):
                        self.on_event(f"[{step.id}] skipped - completed by the operator")
                        continue
                    self._skip_completed_by_operator = False

                steps_run += 1
                self.on_event(f"[{step.id}] {step.intent}")
                snapshot = self._execute(step, params, rungs)
                # An output read mid-flow is gone by the end of the run, so the
                # screen each step leaves behind is kept until extraction.
                seen[step.id] = snapshot

                outcome = self._detect(artifact, snapshot)
                if outcome:
                    if outcome.kind is OutcomeKind.BUSINESS:
                        self.on_event(f"  -> business outcome: {outcome.code}")
                        return self._captured_outcome(ReplayResult(
                            status="business_outcome",
                            outcome_code=outcome.code,
                            reason=outcome.message,
                            step=step.id,
                            expected=self._expected_for(outcome, step),
                            observed=self._describe_screen(snapshot, outcome),
                            steps_run=steps_run,
                            duration_ms=int((time.time() - started) * 1000),
                            locator_rungs=rungs,
                        ))
                    if outcome.kind is OutcomeKind.RECOVERABLE:
                        snapshot = self._recover(
                            artifact, outcome, step, params, rungs
                        )
                    if outcome.kind is OutcomeKind.FATAL:
                        raise StepFailure(
                            step.id,
                            expected=self._expected_for(outcome, step),
                            observed=self._describe_screen(snapshot, outcome),
                            code=outcome.code,
                        )

                if step.postcondition and not self._check(snapshot, step.postcondition):
                    ok, snapshot = self._await(step.postcondition)
                    if not ok:
                        raise StepFailure(
                            step.id,
                            expected=(
                                f"{step.intent!r} then "
                                f"{step.postcondition.kind}="
                                f"{step.postcondition.value!r}"
                                + (f" in frame {step.postcondition.frame!r}"
                                   if step.postcondition.frame else "")
                            ),
                            observed=self._describe_screen(snapshot),
                        )

            # ---- success condition and outputs ----------------------------
            final = self.surface.observe()
            if artifact.success and not self._check(final, artifact.success):
                ok, final = self._await(artifact.success)
                if not ok:
                    raise StepFailure(
                        "success_condition",
                        expected=(
                            f"{artifact.success.kind}={artifact.success.value!r}"
                            + (f" in frame {artifact.success.frame!r}"
                               if artifact.success.frame else "")
                        ),
                        observed=self._describe_screen(final),
                    )

            outputs: dict[str, Any] = {}
            for spec in artifact.outputs:
                if not spec.extract:
                    continue
                # Read each output from the screen it was declared on. Reading
                # everything off the final screen returns the wrong number for
                # anything captured earlier -- a balance noted before a transfer
                # would come back as the balance after it.
                source = seen.get(spec.extracted_at_step or "", final)
                value = extract(source, spec.extract)
                if value is None and source is not final:
                    value = extract(final, spec.extract)
                if value is not None:
                    outputs[spec.name] = value

            return self._capture_success(
                ReplayResult(
                    status="ok",
                    outputs=outputs,
                    steps_run=steps_run,
                    duration_ms=int((time.time() - started) * 1000),
                    locator_rungs=rungs,
                )
            )

        except BusinessOutcome as exc:
            self.on_event(f"  -> business outcome: {exc.code}")
            return self._captured_outcome(ReplayResult(
                status="business_outcome",
                outcome_code=exc.code,
                reason=exc.reason,
                step=exc.step_id or None,
                expected=exc.expected,
                observed=exc.observed,
                steps_run=steps_run,
                duration_ms=int((time.time() - started) * 1000),
                locator_rungs=rungs,
            ))
        except NeedsHuman as exc:
            return self._captured(ReplayResult(
                status="needs_human",
                reason=exc.reason,
                step=exc.step_id,
                expected=exc.expected,
                observed=exc.observed,
                steps_run=steps_run,
                duration_ms=int((time.time() - started) * 1000),
                locator_rungs=rungs,
            ))
        except StepFailure as exc:
            declared = self._declared(exc.code) if exc.code else None
            return self._captured(ReplayResult(
                status="failed",
                outcome_code=exc.code,
                reason=(declared.message if declared else "")
                or "checkpoint not satisfied",
                step=exc.step_id,
                expected=exc.expected,
                observed=exc.observed,
                steps_run=steps_run,
                duration_ms=int((time.time() - started) * 1000),
                locator_rungs=rungs,
            ))
        except (LookupError, PolicyViolation, ValueError) as exc:
            current = locals().get("step")
            return self._captured(ReplayResult(
                status="failed",
                reason=str(exc),
                step=current.id if current else None,
                expected=(current.intent if current else "") or "the step to complete",
                observed=f"{type(exc).__name__}: {exc}",
                steps_run=steps_run,
                duration_ms=int((time.time() - started) * 1000),
                locator_rungs=rungs,
            ))

    def _captured(self, result: "ReplayResult") -> "ReplayResult":
        self._capture_failure(result)
        return result

    def _captured_outcome(self, result: "ReplayResult") -> "ReplayResult":
        self._capture_failure(result, basename="outcome")
        return result

    def _capture_success(self, result: "ReplayResult") -> "ReplayResult":
        """Record a successful run too, not just a broken one.

        Evidence that only exists when something goes wrong cannot answer the
        question people actually ask first -- "did it do the right thing?" A
        run that returned the correct number off the wrong screen looks
        identical in a log and obvious in a screenshot.
        """
        if self.evidence_dir is None or self.surface is None:
            return result
        try:
            directory = Path(self.evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.surface.screenshot(str(directory / "final.png"))
            snapshot = self.surface.observe()
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        "status": result.status,
                        "outputs": result.outputs,
                        "steps_run": result.steps_run,
                        "duration_ms": result.duration_ms,
                        "model_calls": 0,
                        "locator_rungs": result.locator_rungs,
                        "fallback_rate": result.fallback_rate,
                        "url": snapshot.url,
                        "frame_urls": list(getattr(snapshot, "frame_urls", [])),
                    },
                    indent=2,
                )
            )
            result.evidence_ref = str(directory)
        except Exception:
            pass  # evidence is best-effort; never fail a good run over it
        return result

    def _resolves(self, step: Step, params: dict[str, Any]) -> bool:
        if step.target is None:
            return True
        try:
            resolve(self.surface.observe(), step.target, params)
            return True
        except LookupError:
            return False


    def _capture_failure(
        self, result: "ReplayResult", basename: str = "failure"
    ) -> None:
        """Screenshot plus context, written next to the result.

        Reading the message tells you what the engine concluded. The screenshot
        tells you whether it concluded *correctly* -- which is the difference
        between debugging the app and debugging the artifact.

        This runs for business outcomes as well as failures, under the name
        ``outcome``. A declared outcome is a claim about what the application
        said, and in a regulated setting "the app told us there were
        insufficient funds" is exactly the claim someone will later want to see
        a screenshot of.
        """
        if self.evidence_dir is None or self.surface is None:
            return
        try:
            directory = Path(self.evidence_dir)
            directory.mkdir(parents=True, exist_ok=True)
            shot = directory / f"{basename}.png"
            self.surface.screenshot(str(shot))
            snapshot = self.surface.observe()
            (directory / f"{basename}.json").write_text(
                json.dumps(
                    {
                        "status": result.status,
                        "outcome_code": result.outcome_code,
                        "step": result.step,
                        "reason": result.reason,
                        "expected": result.expected,
                        "observed": result.observed,
                        "url": snapshot.url,
                        "frame_urls": getattr(snapshot, "frame_urls", []),
                        "nodes": [
                            {"role": n.role, "name": n.name, "frame": n.frame}
                            for n in snapshot.nodes
                        ],
                    },
                    indent=2,
                )
            )
            result.evidence_ref = str(directory)
        except Exception:
            pass  # evidence is best-effort; never mask the original failure

    @staticmethod
    def _expected_for(outcome, step) -> str:
        """What should have been true, in the terms of the condition that fired.

        The outcome knows what it was looking for, so it is the only thing that
        can say what it hoped not to find. Falling back to the step's intent
        keeps older artifacts -- compiled before expectations were declared --
        producing something readable rather than an empty field.
        """
        if getattr(outcome, "expectation", ""):
            return f"{outcome.expectation} (during {step.intent!r})"
        return f"{step.intent!r} to complete without the application reporting a condition"

    @staticmethod
    def _quote_app_message(outcome, snapshot) -> str | None:
        """The application's own sentence, lifted from the screen.

        A detector matches a fragment ("could not be verified"); the operator
        wants the whole line the app actually printed. Preferring an AX node
        over the raw text keeps it to one message rather than a paragraph.
        """
        needle = (getattr(outcome.detector, "value", "") or "").lower()
        if not needle or needle.startswith("__"):
            return None
        # Shortest match wins. On a table-based layout the same text belongs to
        # a cell, a row and the page body alike; the smallest node holding it is
        # the message itself rather than everything printed around it.
        matches = [
            name for node in snapshot.nodes
            if needle in (name := (node.name or "").strip()).lower()
        ]
        if matches:
            return min(matches, key=len)[:200]
        for line in (getattr(snapshot, "text", "") or "").splitlines():
            line = line.strip()
            if needle in line.lower():
                return line[:200]
        return None

    def _describe_screen(self, snapshot, outcome=None) -> str:
        """What is actually on screen, in a form someone can act on.

        A code alone ("app_server_error") says a rule matched. This leads with
        the application's own words, then says what the page was and where --
        which is what you need to tell a real outage from a changed label.
        """
        content = [
            n.name for n in snapshot.nodes
            if n.name and n.frame not in ("navframe", "menu", "statusframe", "footer")
        ]
        text = " / ".join(dict.fromkeys(content)) or (
            getattr(snapshot, "text", "") or ""
        ).replace("\n", " / ")
        excerpt = text[:200] + ("..." if len(text) > 200 else "")
        parts = []
        code = outcome if isinstance(outcome, str) else getattr(outcome, "code", None)
        if not isinstance(outcome, str) and outcome is not None:
            quoted = self._quote_app_message(outcome, snapshot)
            if quoted:
                parts.append(f"the application said: {quoted!r}")
        if code:
            parts.append(f"detector {code!r} matched")
        parts.append(f"url={snapshot.url}")
        if getattr(snapshot, "title", ""):
            parts.append(f"title={snapshot.title!r}")
        parts.append(f"screen text: {excerpt or '(empty)'}")
        return " | ".join(parts)

    # -------------------------------------------------------------- recovery

    def _recover(self, artifact, outcome, step, params, rungs):
        """Respond to a recoverable condition deliberately, then re-run the step.

        Bounded on purpose. A condition that survives its allowed attempts is
        escalated rather than retried forever -- an unbounded retry loop turns a
        clear failure into a hang, which is strictly worse to debug.
        """
        for attempt in range(1, outcome.max_attempts + 1):
            self.on_event(
                f"  -> recoverable: {outcome.code} "
                f"(attempt {attempt}/{outcome.max_attempts}, action={outcome.recovery})"
            )

            if outcome.recovery == "dismiss":
                if not self._dismiss(outcome.recovery_target):
                    raise NeedsHuman(
                        step.id,
                        f"{outcome.code}: could not find "
                        f"{outcome.recovery_target!r} to dismiss it",
                    )
                # Do NOT re-run the step. The interstitial appeared *after* the
                # step succeeded, so repeating it would be wrong -- and on a
                # mutating step it would be a double submission.
                snapshot = self.surface.observe()
            else:
                if outcome.recovery == "reauth":
                    self._reauthenticate(artifact, params, rungs, step)
                # The step did not take effect, so running it again is the
                # recovery.
                snapshot = self._execute(step, params, rungs)
            if self._detect(artifact, snapshot) is not outcome:
                self.on_event(f"  -> recovered from {outcome.code}")
                return snapshot

        raise NeedsHuman(
            step.id,
            f"{outcome.code} persisted after {outcome.max_attempts} recovery attempt(s)",
        )

    def _dismiss(self, control_name: str | None) -> bool:
        """Clear an unexpected interstitial by clicking the control that closes it."""
        if not control_name:
            return False
        snapshot = self.surface.observe()
        for node in snapshot.nodes:
            if _normalise(node.name) == _normalise(control_name):
                self.surface.click(node.ref)
                return True
        return False

    def _reauthenticate(self, artifact, params, rungs, failed_step) -> None:
        """Re-run the credential steps, then resume at the step that was interrupted.

        A session that expires mid-flow is the classic case: nothing is broken,
        the run simply needs to sign in again and carry on from where it was.
        """
        self.on_event("  -> re-authenticating")
        self._operator_signed_in = False
        for step in artifact.steps:
            if step.id == failed_step.id:
                break
            if step.risk is Risk.CREDENTIAL or step.action.kind in {
                "navigate",
                "credential_entry",
            }:
                try:
                    self._execute(step, params, rungs)
                except (LookupError, NeedsHuman):
                    continue
            elif step.action.kind == "click":
                try:
                    self._execute(step, params, rungs)
                except LookupError:
                    continue

    # ----------------------------------------------------------------- step

    def _execute(
        self, step: Step, params: dict[str, Any], rungs: dict[str, int]
    ) -> UISnapshot:
        action = step.action

        if action.kind == "navigate":
            self.policy.check_url(action.url or "")
            self.surface.navigate(action.url or "")
            return self.surface.observe()

        # Steps that need a person are declared in the artifact, so an
        # unattended caller can be told before it invokes rather than
        # discovering it half way through.
        if action.kind in {"credential_entry", "human_action"} or (
            step.risk is Risk.IRREVERSIBLE and action.kind == "click"
        ):
            instruction = action.instruction or step.intent
            if self.operator is None:
                raise NeedsHuman(
                    step.id, f"step requires an operator: {instruction}"
                )
            self.on_event(f"  -> handing control to the operator: {instruction}")
            if not self.operator(step.id, instruction):
                raise NeedsHuman(
                    step.id,
                    "the operator did not complete the step",
                    expected=f"an operator to: {instruction}",
                    observed=self._describe_screen(self.surface.observe()),
                )
            return self.surface.observe()

        snapshot = self.surface.observe()

        if step.target is None:
            return snapshot

        # Resolving a control is a wait-for, not a single look. A control that is
        # not there yet and one that is not there at all look identical in a
        # single snapshot, and a page still loading is the common case. Re-observe
        # until the window closes, then decide.
        # Resolving a control is a wait-for, not a single look. A control that is
        # not there *yet* and one that is not there *at all* look identical in a
        # single snapshot, and a page still loading is the common case. Re-observe
        # until the window closes, then decide.
        node = rung_index = None
        failure: LookupError | None = None
        deadline = time.time() + min(step.timeout_ms, 4000) / 1000
        while True:
            try:
                node, rung_index, _confidence = resolve(snapshot, step.target, params)
                failure = None
                break
            except LookupError as exc:
                failure = exc
                if time.time() >= deadline:
                    break
                self._wait(200)
                snapshot = self.surface.observe()

        if failure is not None:
            exc = failure
            if step.on_unresolved:
                # Say what should have been true first, then how we looked for
                # it. The ladder detail matters when debugging a broken locator;
                # the expectation is what tells the caller this was an answer.
                declared = self._declared(step.on_unresolved)
                expectation = getattr(declared, "expectation", "") if declared else ""
                raise BusinessOutcome(
                    step.on_unresolved,
                    f"the control selecting this record is not present on {snapshot.url}",
                    step_id=step.id,
                    expected=(
                        f"{expectation} (during {step.intent!r}); tried {exc}"
                        if expectation else str(exc)
                    ),
                    observed=self._describe_screen(snapshot),
                )
            # Every rung that was tried, and what the screen actually offered --
            # a bare "not found" is not debuggable.
            # Prefer controls sharing the role we were looking for -- those are
            # the near misses. Fall back to anything named, because "none" is a
            # useless diagnostic on a screen full of divs acting as buttons.
            wanted_role = step.target.primary.role
            nearby = [
                f"{n.role}:{n.name or n.anchor}"
                for n in snapshot.nodes
                if wanted_role and n.role == wanted_role
            ] or [
                f"{n.role}:{n.name or n.anchor}"
                for n in snapshot.nodes
                if n.interactive or n.name
            ]
            nearby = nearby[:8]
            raise StepFailure(
                step.id,
                expected=str(exc),
                observed=f"url={snapshot.url} | interactive controls present: "
                + (", ".join(nearby) or "none"),
            )
        rungs[step.id] = rung_index
        if rung_index > 0:
            self.on_event(f"  -> matched on fallback rung {rung_index}")

        if action.kind == "click":
            self.policy.check_click(node.name or node.anchor)
            self.surface.click(node.ref)
        elif action.kind == "type":
            self.surface.type_text(node.ref, str(params[action.param]))
        elif action.kind == "select":
            self.surface.select_option(node.ref, str(params[action.param]))
        elif action.kind == "type_secret":
            name = action.secret_ref or ""
            if self.secrets.get(name):
                # Vault-style: the value is injected and never logged.
                self.surface.type_text(node.ref, self.secrets[name])
                if action.submit_form:
                    self.surface.submit_form(node.ref)
            elif self.operator is not None:
                # Handoff: the system holds no credential at all. A person signs
                # in on this same session. They do it in one go, while the
                # artifact models sign-in as several steps, so the remaining
                # steps of that block are skipped once they hand control back.
                if not self._operator_signed_in:
                    if not self.operator(
                        step.id, "enter the credentials and sign in, then hand control back"
                    ):
                        raise NeedsHuman(step.id, "the operator did not sign in")
                    self._operator_signed_in = True
                    self._skip_completed_by_operator = True
            else:
                raise NeedsHuman(step.id, f"no credential available for {name!r}")

        return self.surface.observe()
