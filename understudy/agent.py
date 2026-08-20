"""The discovery loop: observe -> decide -> act, with a real model in the loop.

The model never sees HTML. It sees a normalised accessibility-tree snapshot
plus a screenshot, and it acts through typed tools whose arguments are already
the vocabulary a capability artifact records. That is the point: the run
transcript compiles into an artifact instead of having to be scraped out of
prose, and the same step grammar can later drive a desktop surface.

Secrets are handled by name. ``type_secret`` tells us *which* credential to
enter; the value is injected at the driver and never enters the model's
context, the journal, or a screenshot.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import anthropic

from .journal import Journal
from .policy import Policy, PolicyViolation
from .surface import Surface

MODEL = "claude-opus-5"

SYSTEM = """You are operating a legacy bank back-office web application through its \
user interface, the way a human operator would. The application has no API.

You see the screen as an accessibility-tree snapshot: a numbered list of nodes, each \
with a role, an accessible name, and sometimes a value. Some controls have NO \
accessible name -- legacy forms often leave inputs unlabelled. For those, use the \
"labelled by" hint, which is the visible text immediately before the control.

You also get a screenshot of the same screen. Use it to disambiguate when the tree \
is unclear.

Rules:
- Act only through the provided tools, one step at a time. After each action you \
receive a fresh snapshot.
- Refer to controls by their [ref] number from the CURRENT snapshot. Refs change \
between screens, so never reuse an old one.
- Never guess a password. To sign in, use type_secret with the name of the \
credential; the value is supplied outside your context.
- Some controls are classified irreversible (committing a funds transfer, deleting \
records). You must never press them yourself. If the goal requires one, stage \
everything up to that point, then call request_human with a precise instruction. \
The operator will perform it on the same session and hand control back. Then \
re-read the screen, confirm what actually changed, and continue the goal.
- Only call finish with "needs_human" if no operator is available to help.
- When you have accomplished the goal, call finish with the data you were asked to \
report, as structured fields.

Work efficiently. Prefer the most direct route to the goal."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click a control on the current screen, by its [ref] number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer", "description": "The [ref] of the control."},
                "intent": {
                    "type": "string",
                    "description": "Why you are clicking this, in a short phrase. Recorded in the artifact.",
                },
            },
            "required": ["ref", "intent"],
        },
    },
    {
        "name": "type",
        "description": "Type non-secret text into a field, by its [ref] number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "text": {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["ref", "text", "intent"],
        },
    },
    {
        "name": "type_secret",
        "description": (
            "Enter a credential you are not permitted to see. Give the NAME of the "
            "secret (for example 'username' or 'password'); the value is injected "
            "outside your context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "secret_name": {"type": "string", "enum": ["username", "password"]},
                "intent": {"type": "string"},
            },
            "required": ["ref", "secret_name", "intent"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown, by its [ref] number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "integer"},
                "value": {"type": "string"},
                "intent": {"type": "string"},
            },
            "required": ["ref", "value", "intent"],
        },
    },
    {
        "name": "request_human",
        "description": (
            "Hand control of the live session to a human operator so they can "
            "perform an action you are not permitted to perform, such as "
            "committing an irreversible transfer. The automation pauses, the "
            "operator acts in the same browser session, and control returns to "
            "you. Afterwards, re-read the screen and verify what changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you cannot do this yourself.",
                },
                "instruction": {
                    "type": "string",
                    "description": "Exactly what the operator should do, in one or two sentences.",
                },
            },
            "required": ["reason", "instruction"],
        },
    },
    {
        "name": "finish",
        "description": (
            "End the run. Use status 'success' with the requested data in outputs, "
            "'business_outcome' if the application gave a legitimate non-success "
            "answer, or 'needs_human' if you cannot safely proceed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "business_outcome", "needs_human"],
                },
                "outputs": {
                    "type": "object",
                    "description": "The data the goal asked for, as named fields.",
                },
                "reason": {"type": "string"},
            },
            "required": ["status", "reason"],
        },
    },
]


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        journal: Journal,
        secrets: dict[str, str],
        use_vision: bool = True,
        credential_mode: str = "env",
    ):
        self.surface = surface
        self.policy = policy
        self.journal = journal
        self.secrets = secrets
        self.use_vision = use_vision
        # "env"   -> the driver injects the value; the model never sees it
        # "human" -> automation pauses and a person types it into the live
        #            session, so the system never possesses it at all
        self.credential_mode = credential_mode
        self.client = anthropic.Anthropic()
        self.steps: list[dict[str, Any]] = []
        self._human_signed_in = False
        self._current_step = 0

    # ------------------------------------------------------------------ tools

    def _describe(self, ref: int) -> dict[str, Any] | None:
        """Capture what the acted-on control *is*, not which slot it occupied.

        A ref is an index into one snapshot and is meaningless on the next run.
        The semantic descriptor -- role, accessible name, anchoring text, frame
        -- is what a capability artifact records and what replay resolves
        against, so it has to be captured at the moment of the action.
        """
        try:
            node = self.surface.resolve(ref)
        except Exception:
            return None
        return {
            "role": node.role,
            "name": node.name,
            "anchor": node.anchor,
            "frame": node.frame,
            "interactive": node.interactive,
        }

    def _execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool call through the policy. Returns (result_text, ok)."""
        # Describe the target before acting -- afterwards the screen may have
        # navigated away and the node no longer exists.
        if "ref" in args:
            self._last_target = self._describe(args["ref"])
        else:
            self._last_target = None

        try:
            self.policy.check_action(name)

            if name == "click":
                node = self.surface.resolve(args["ref"])
                self.policy.check_click(node.name or node.anchor)
                return self.surface.click(args["ref"]), True

            if name == "type":
                return self.surface.type_text(args["ref"], args["text"]), True

            if name == "type_secret":
                if self.credential_mode == "human":
                    return self._human_credential_handoff(args)
                secret_name = args["secret_name"]
                if secret_name not in self.secrets:
                    return f"no secret named {secret_name!r} is available", False
                # The value is injected here and never returned to the model.
                self.surface.type_text(args["ref"], self.secrets[secret_name])
                return f"entered the {secret_name} credential", True

            if name == "select":
                return self.surface.select_option(args["ref"], args["value"]), True

            if name == "request_human":
                return self._human_action_handoff(args)

            return f"unknown tool {name!r}", False

        except PolicyViolation as exc:
            return f"REFUSED BY POLICY: {exc}", False
        except Exception as exc:  # surfaced to the model so it can adapt
            return f"action failed: {type(exc).__name__}: {exc}", False

    # -------------------------------------------------------------- handoff

    def _pause_for_human(
        self, kind: str, reason: str, instructions: list[str]
    ) -> tuple[bool, str]:
        """Hand control of the live session to a person, then take it back.

        One mechanism, two triggers. A credential step and an irreversible
        action are both *declared* interventions -- known before the run starts,
        not failures -- so they share the same pause, the same control transfer,
        and the same audit records. Only the reason and the instructions differ.

        Nothing is captured while the human holds control.
        """
        step = self._current_step
        self.journal.intervention_raised(step, kind=kind, reason=reason)

        print("\n" + "=" * 66)
        print("  AUTOMATION PAUSED - control handed to you")
        print("=" * 66)
        for line in instructions:
            print(f"  {line}")
        print()
        print("  Evidence capture is suspended until you hand control back.")
        print("=" * 66)

        try:
            input("  Press Enter here when you are done... ")
        except EOFError:
            note = "no operator was attached (stdin closed); handoff abandoned"
            self.journal.intervention_resolved(step, kind, note)
            return False, note

        snapshot = self.surface.observe()
        note = f"operator handed control back; screen is now {snapshot.url}"
        self.journal.intervention_resolved(step, kind, note)
        print("=" * 66)
        print(f"  CONTROL RETURNED TO THE AGENT - {snapshot.url}")
        print("=" * 66)
        return True, note

    def _human_action_handoff(self, args: dict[str, Any]) -> tuple[str, bool]:
        """A person performs an action the agent is not permitted to perform."""
        instruction = args.get("instruction", "complete the pending action")
        reason = args.get("reason", "an action requiring a human was reached")

        ok, note = self._pause_for_human(
            kind="human_action",
            reason=reason,
            instructions=[
                "The agent has reached an action it is not permitted to perform:",
                f"    {reason}",
                "",
                "In the browser window that is open:",
                f"    {instruction}",
            ],
        )
        if not ok:
            return f"HANDOFF FAILED: {note}", False
        return (
            f"The operator performed the action. {note}. "
            "Re-read the screen and verify what changed before continuing.",
            True,
        )

    def _human_credential_handoff(self, args: dict[str, Any]) -> tuple[str, bool]:
        """Pause the automation and let a person sign in on the same live session.

        This is a *declared* intervention, not a failure: the step is marked
        as needing a human before the run starts, so pausing here is normal
        operation rather than something going wrong.

        Nothing is captured while the human holds control -- no screenshot, no
        keystrokes, no DOM snapshot. The journal records that authentication
        happened and by whom, and nothing else. That is stronger than redacting
        a password after the fact, because the value never enters the system.
        """
        if self._human_signed_in:
            return "the operator has already signed in; continue from here", True

        ok, note = self._pause_for_human(
            kind="credential_handoff",
            reason="a credential step was reached; a person must enter it",
            instructions=[
                "The agent has reached the sign-on screen and is not permitted",
                "to know the credentials.",
                "",
                "In the browser window that is open:",
                "    1. type the username and password",
                "    2. click Log In",
            ],
        )
        if not ok:
            return f"HANDOFF FAILED: {note}", False

        # Verify rather than assume. The human may have signed in, or typed the
        # wrong password, or wandered somewhere else entirely.
        signed_in = "index.htm" not in self.surface.observe().url
        self._human_signed_in = signed_in

        if not signed_in:
            return (
                "the operator returned control but the sign-on screen is still "
                "showing; the credentials may have been rejected",
                False,
            )
        return (
            "the operator entered the credentials and signed in. You are now "
            "past the sign-on screen; continue with the goal.",
            True,
        )

    # ------------------------------------------------------------------- loop

    def run(self, goal: str) -> dict[str, Any]:
        started = time.time()
        messages: list[dict[str, Any]] = []
        step = 0

        while True:
            step += 1
            self._current_step = step
            if step > self.policy.max_steps:
                return self._stop("failed", f"step budget of {self.policy.max_steps} exhausted")
            if time.time() - started > self.policy.max_seconds:
                return self._stop("failed", "wall-clock budget exhausted")

            # ---- observe -------------------------------------------------
            snapshot = self.surface.observe()
            self.journal.observed(
                step,
                snapshot.url,
                # Same shape the final screen is recorded in, so the compiler
                # derives extraction rules identically wherever it looks.
                [{"role": n.role, "name": n.name, "frame": n.frame}
                 for n in snapshot.nodes],
            )
            shot_path = self.journal.shots / f"step_{step:02d}.png"
            self.surface.screenshot(str(shot_path))
            print(f"\n[step {step}] observe  → {len(snapshot.nodes)} nodes · {snapshot.url}")

            content: list[dict[str, Any]] = [
                {"type": "text", "text": f"GOAL: {goal}\n\n{snapshot.render()}"}
            ]
            if self.use_vision:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.standard_b64encode(
                                shot_path.read_bytes()
                            ).decode(),
                        },
                    }
                )
            messages.append({"role": "user", "content": content})

            # ---- decide --------------------------------------------------
            call_started = time.time()
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=8000,
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=messages,
            )
            latency_ms = int((time.time() - call_started) * 1000)
            self.journal.decided(step, response, latency_ms)

            if response.stop_reason == "refusal":
                return self._stop("failed", "the model declined this request")

            usage = response.usage
            print(
                f"         decide   → {response.model} · {latency_ms/1000:.1f}s · "
                f"in {usage.input_tokens:,} / out {usage.output_tokens:,} tok"
            )

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                text = next((b.text for b in response.content if b.type == "text"), "")
                return self._stop("failed", f"model stopped without acting: {text[:200]}")

            # ---- act -----------------------------------------------------
            results = []
            for block in tool_uses:
                args = dict(block.input)

                if block.name == "finish":
                    status = args.get("status", "success")
                    try:
                        final = self.surface.observe()
                        self.journal.final_screen(
                            [
                                {"role": n.role, "name": n.name, "frame": n.frame}
                                for n in final.nodes
                            ]
                        )
                    except Exception:
                        pass
                    print(f"         finish   → {status}: {args.get('reason','')}")
                    return self._stop(
                        status, args.get("reason", ""), args.get("outputs", {})
                    )

                intent = args.get("intent", "")
                result, ok = self._execute(block.name, args)
                self.journal.acted(
                    step, block.name, args, result, ok, target=self._last_target
                )
                self.steps.append(
                    {"step": step, "tool": block.name, "intent": intent, "args": args, "ok": ok}
                )
                mark = "ok" if ok else "!!"
                print(f"         action   → {block.name}({intent!r}) [{mark}] {result}")

                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                        "is_error": not ok,
                    }
                )

            messages.append({"role": "user", "content": results})

    def _stop(
        self, status: str, reason: str, outputs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.journal.run_finished(status, outputs)
        return {"status": status, "reason": reason, "outputs": outputs or {}}
