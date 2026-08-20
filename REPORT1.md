# Understudy : design write-up

A system that lets an LLM learn a back-office task once, turns that run into a
typed capability, and executes it thereafter with no model in the decision loop.
Built against a deliberately hostile legacy banking UI: framesets, table layout,
no test IDs, unlabelled inputs, a `<div>` acting as a button, and an idle
session timeout.

---

## 1. Architecture

Five stages, each with one job, connected by data rather than by calls:

```
goal + target → DISCOVERY (LLM) → JOURNAL → COMPILER → ARTIFACT → REPLAY (no LLM)
                     │                                     │
                     └──────── policy on the call path ────┘
```

**Discovery** runs an observe → decide → act loop. The model reads an
accessibility-tree rendering of the screen, picks one of six tools (`click`,
`type`, `type_secret`, `select`, `request_human`, `finish`), and the driver
executes it. It stops on success, a step budget, a wall-clock budget, a model
refusal, or text without an action.

**The journal** is the only thing discovery produces. Every observation (with
the screen itself), every decision (with the server-issued `request_id`), every
action, every human intervention. It is append-only JSONL: evidence, and the
compiler's sole input.

**The compiler** reads that journal offline and emits a contract. This is the
stage that makes the project more than a macro recorder — it is where a
transcript becomes a typed capability with parameters, outputs, checkpoints and
declared outcomes.

**Replay** takes an artifact plus arguments and executes. It imports no model
client. That is the
production path.

**The catalog** exposes artifacts as callable tools — Python tool definitions,
an HTTP API, or a CLI router — with the contract *derived from the artifact*, so
what is advertised cannot drift from what replay does.

### Key decisions and their costs

**The accessibility tree, not the DOM.** Roles and accessible names are what a
desktop automation API (UIA, AX, AT-SPI) exposes too, so the artifact shape
extends to surfaces with no DOM at all. **Cost:** the AX tree is coarser than
the DOM, and CDP's tree does not descend into frames automatically — same-origin
child frames share the parent's session and must be walked explicitly via
`Page.getFrameTree`. On this frameset target, getting that wrong produced zero
nodes, silently.

**Compilation is offline and separate from discovery.** Discovery could emit an
artifact directly and save a stage. Keeping them apart means the compiler can be
fixed and re-run against journals already recorded — which happened twice during
this build, once to add mid-run output extraction. **Cost:** an extra command,
and a journal format that has to carry enough to reconstruct from.

**Policy is on the call path, not in the prompt.** A rule in a system prompt is
advice a model can talk itself out of. `policy.py` sits between every tool and
the browser. **Cost:** the model sometimes gets a refusal it did not anticipate,
and must be given a coherent error to reason about rather than a stack trace.

**Four result statuses, not two.** `ok` / `business_outcome` / `needs_human` /
`failed`, with distinct exit codes. **Cost:** callers must handle four cases.
That cost is the point — see §3.

---

## 2. Artifact schema

Pydantic v2, JSON-Schema-exportable, versioned `capability.id@version`. Shaped
around one question: *what does a calling agent need in order to invoke this
safely without reading the code?*

```
CapabilityArtifact
├── capability   id · version · summary · approval(draft|approved)
├── app          product · family            ← vendor product, not institution
├── inputs       [ParamSpec]                 name · type · required · sensitivity
├── outputs      [OutputSpec]                name · type · extract · extracted_at_step
├── steps        [Step]                      intent · action · target · risk ·
│                                            postcondition · on_unresolved
├── outcomes     [OutcomeSpec]               code · kind · detector · expectation ·
│                                            recovery · max_attempts
├── success      Checkpoint
└── provenance   model · run_id · request_ids · token counts
```

**Targets are a ranked ladder, not a selector.** Each step carries ordered
descriptors — `role_name` (1.00) → `role_anchor` (0.90) → `text` (0.80) →
`ordinal` (0.65) → `coords` (0.45, policy-gated) — with a confidence floor of
0.8. Replay walks it top-down. A test asserts no `css`, `xpath`,
`queryselector`, or `#id` ever appears in an artifact. The ladder is not just
robustness; *which rung matched* is the drift signal (§4).

**Data is parameterised, never frozen.** The recorded run clicked a link whose
visible text was `13566`. Storing that would pin the capability to one account
forever, so the descriptor stores `name_param: account_id` and resolves at
invocation. This is the difference between a recording and a capability.

**Outputs know where they were seen.** `extracted_at_step` exists because the
pre-transfer balance lives on a screen that no longer exists at the end of the
run — by then the same cell reads the new figure. Reading every output from the
final screen returns nothing for it at best and the wrong number at worst.

**Extraction is derived, and refuses to guess.** The compiler locates the
reported value on a recorded screen and derives an anchor from the neighbouring
cell. If it can find no anchor — or only anchors that *repeat down a column*,
which means they are data rather than labels — it emits **no rule at all**. An
earlier version bound the transaction amount to `'Credit'` and returned $285.00
instead of $512.60 on the next record. Declaring an output the engine cannot
fetch is bad; returning a confidently wrong number is worse.

**Risk is per step**, not per capability: `read`, `reversible`, `mutating`,
`credential`, `irreversible`. A caller can see *where* a capability becomes
dangerous, not just that it is.

**Everything compiles to `draft`.** Parameter naming, risk classification and
outcome detectors are inferred. Inference is good enough to be useful and not
good enough to trust unattended, and the schema should say so.

---

## 3. Determinism & error handling

### Determinism

Three properties, in order of how much trouble each prevents:

**Ordered resolution with a floor.** Two candidates that cannot be told apart is
an **error**, not a coin flip. Guessing is how a capability silently transfers
money from the wrong account.

**Conditions, never sleeps.** Every wait is a wait *for* something with an
explicit timeout. A fixed sleep is how flakiness is designed in.

**Detectors after every step.** The screen is checked against every declared
outcome before the next step runs, so a condition is caught where it happened
rather than four steps later as something incomprehensible.

Supporting details: viewport, locale and timezone are pinned; the artifact is
the only input besides arguments; replay reports `fallback_rate` so a run that
succeeded *by leaning on lower rungs* is visible.

### Runtime conditions

The interesting failures on a stable UI are runtime states, not layout drift.
Each is declared once per app, with a detector, an `expectation`, and — if
recoverable — a specific action:

| Condition | Tier | Response | Exit |
|---|---|---|---|
| validation error, insufficient funds, record not found, permission denial, unknown username, wrong password | **business** | stop deliberately, return the code | 2 |
| unexpected interstitial | **recoverable** | `dismiss` the named control, continue | 0 |
| session timeout | **recoverable** | `reauth`, resume at the interrupted step | 0 |
| slow load | **recoverable** | wait *for* the checkpoint | 0 |
| server error | **fatal** | stop, screenshot + context | 4 |

**A business outcome is not a failure.** "No such member" is a legitimate answer
the caller needs. Conflating it with a crash is the single most common design
mistake here, and it is why the result contract has four statuses.

**Recovery is per-condition and bounded.** Collapsing "dismiss the dialog",
"sign in again" and "wait" into one generic retry leaves the first two spinning
until the budget runs out — retrying a click cannot clear a modal that is
swallowing it. Each condition declares `max_attempts`; exceeding it escalates
rather than looping.

**Dismiss does not repeat the step.** An interstitial appearing *after* a step
succeeded is dismissed and the run continues. Re-running the step there would be
a double submission on a transfer.

Every non-success carries **what step, what was expected, what was observed**,
where "expected" is the outcome's own words rather than boilerplate:

```
outcome     wrong_password
reason      Wrong password: the username is a real account, but the password
            typed at the pause does not match it. Nothing was retried --
            repeating a rejected password risks a lockout.
determined at s2_enter_username
  expected  the password entered at the pause to match the username entered with it
  observed  the application said: 'The password entered for that username is not correct.'
```

**Undeclared conditions still fail safe.** With Transfer Funds silently serving
the wrong page — no error text, nothing obviously broken — replay stops at the
next step with `code=None` and the full locator ladder in `expected`. It refuses
to type an amount into a screen with no amount field. `code=None` is the honest
signal: stopped safely, but nobody declared this.

### UI drift (secondary)

The ladder absorbs it and `fallback_rate` measures it. A capability that starts
matching on rung 2 instead of rung 0 still works but is working harder — which
is the moment to re-record, rather than after it breaks.

---

## 4. Heterogeneity & multi-tenant

### The surface seam

The boundary is a four-method protocol:

```python
class Surface(Protocol):
    def observe(self) -> UISnapshot: ...
    def act(self, action: str, **kwargs) -> str: ...
    def screenshot(self, path: str) -> None: ...
    def wait(self, milliseconds: int) -> None: ...
```

`UISnapshot` is `url · title · nodes · frame_urls · text`, and a `UINode` is
`role · name · value · frame · anchor · interactive`. It also carries a `ref` —
an index into the current snapshot, plus a CDP backend id — but those are
ephemeral addressing handles that expire with the snapshot and are **never
persisted into an artifact**; a compiled step refers to a control only by role,
name, anchor and frame. No selector, no tag name, no durable element handle
crosses the seam. The recorded flow is expressed entirely in those terms, so
**everything above the seam is surface-agnostic**: the compiler, the schema, the
resolution ladder, the detectors, the recovery model.

This is enforced, not aspirational. When `surface.page` once leaked into the
replay engine to implement a wait, a test caught it and the fix was to add
`wait()` to the protocol.

**Extending to a desktop surface** means implementing those four methods over
UIA/AX/AT-SPI. `role` and `name` map almost directly — that is where the
accessibility-tree choice pays off. Two things need genuine work: `frame`
generalises to a window/pane identifier, and `anchor` (nearby text that gives an
unnamed control meaning) needs a spatial heuristic rather than table adjacency.
For a surface with neither — a Citrix session, a canvas app — the ladder already
has `coords` as a policy-gated bottom rung, plus screenshots for a
vision-model-assisted rung. The honest limitation: **only `WebSurface` exists
today**, so the seam is well-designed but demonstrated once.

### Multi-tenant reuse

Hundreds of tenants running ~20 apps, many sharing a vendor product. Re-recording
per tenant is the obvious approach and it does not scale: 2,000 recordings, each
independently rotting.

The artifact is therefore keyed to the **vendor product** (`app.product`), not
the institution, and per-institution differences live in a small overlay:

```
TenantBinding
├── tenant_id · capability_id · capability_version
├── base_url
├── overrides      step_id → partial descriptor/timeout overrides
├── insert_before  step_id → extra steps (a tenant-only interstitial)
├── last_verified · fallback_rate
└── status         healthy | needs_review | broken
```

Small enough that a human can review one per tenant in about a minute — which is
what makes per-tenant review affordable at all. The target app models exactly
this drift: two tenants of one fictional product where Cascade relabels every
control (`Member ID:` vs `Username:`, `Move Money` vs `Transfer Funds`), renames
all three frames (`menu`/`body`/`footer` vs `navframe`/`contentframe`/`statusframe`),
and inserts a compliance interstitial mid-flow.

**Drift management** has two signals, one built and one designed:

- *Built:* `fallback_rate` per run. Rising fallback usage on one tenant means
  that tenant's binding needs review; rising across all tenants means the vendor
  shipped an upgrade and the base flow needs re-recording. The distinction
  matters — it is the difference between one fix and two thousand.
- *Designed:* a scheduled canary replay per tenant against a read-only
  capability, writing `last_verified` and flipping `status`. Cheap, because
  replay is ~2 seconds and needs no model.

**Honest status:** `TenantBinding` is modelled and unit-tested but never
produced by the compiler or applied by replay, and no Cascade artifact exists.
The reuse story is a design claim backed by a schema, not a demonstration. The
cheapest way to close it is `apply_binding(artifact, binding)` plus one replay
of the Meridian artifact against Cascade — roughly 30 lines and no discovery run.

---

## 5. Escalation & handoff

One mechanism, two triggers, both structural rather than heuristic:

**Credential entry.** The system never holds the password. `type_secret`
receives the *name* of a secret, never its value; with no vault entry, the run
pauses and a person types it on the live session.

**Irreversible action.** Any control whose visible text matches
`irreversible_labels` — "submit transfer", "close account" — is refused. In the
transfer capability the model reached the confirmation screen and stopped on its
own: *"Submitting the transfer is an irreversible funds movement, which I am not
permitted to press."*

A third trigger exists for genuine stuckness: exhausting a bounded recovery, or
a locator resolving below the confidence floor, raises `needs_human` rather than
guessing.

**Taking control.** The pause hands over the *same live browser session* — same
cookies, same session, same page state — not a fresh one. The request carries
the capability, the step id, the current URL, what the automation was trying to
do, and why it stopped. The person acts; the terminal prompt is the resume
signal.

**Handing back.** On resume, replay re-observes rather than assuming: the
operator's actions are recorded as `intervention_resolved` with
`actor: "human"`, and the agent is explicitly told the screen may have changed
and to re-read it. Replay additionally skips steps the operator already
completed by hand, resuming at the first step that resolves again.

**Who is in control** is answerable at every moment from the journal —
`intervention_raised` / `intervention_resolved` carry `control_transferred_to`
and the actor. Two properties fall out of that seam:

- **Evidence capture is suspended during a handoff** (`capture: "suspended"`),
  so a screenshot cannot catch a password mid-typing. The gap in the evidence is
  itself recorded, which is better than a gap nobody can explain.
- **The same escalation works over HTTP.** An unattended API call to the
  transfer capability returns `needs_human` at `s10` rather than pressing Submit
  — and the caller knew before invoking, because `requires_operator` is in the
  advertised contract.

The operator surface is deliberately minimal: a real browser window plus a
terminal prompt. A production console would add authenticated queueing, a
co-browsing view, and per-step approval — but the *control-transfer model* is
the part worth getting right, and that part is real.

---

## 6. Safety

**Allowlist.** Origins and action kinds. `/_reset` is on `denied_paths` — the
automation must never be able to reset the world it is operating on. Enforced on
the call path, so neither loop can route around it.

**Risk classes** on every step, with the irreversible class **blocked rather
than confirmed**. Blocking is the conservative choice because a confirmation
prompt is one more thing an agent can learn to click through; a structural
refusal is not. `mutating` (staging a transfer) is allowed because it is
reversible up to the final commit — that is precisely where the line falls.

**Secrets never exist to be leaked.** The strongest control is architectural:
`type_secret` takes a name, not a value, so there is nothing in the loop to
redact. Journal redaction of `password`/`token`/`pin`/`secret` is belt-and-braces,
and capture suspension during handoff covers the screenshot path. A test asserts
no credential survives compilation.

**Ambiguity refusal is a safety control**, not an ergonomic one. Below the
confidence floor, replay stops rather than acting on the best guess.

### Limits — where this model does not hold

- **`approval: draft` is advisory.** The compiler warns; nothing enforces it. A
  draft artifact — parameter names and detectors inferred by a model, never
  reviewed — is still invocable through the catalog and the API. This is the
  most substantive gap in the guardrail model.
- **`requires_operator` is unconditional.** It is `true` whenever operator steps
  exist, even where a vault covers them. Conservative, but the contract says
  something untrue about itself, which matters more here than elsewhere because
  the artifact *is* the contract.
- **Irreversibility is detected by label text.** A control named "Finalize" or
  localised into another language is not on the list. Label matching is a
  backstop; the durable answer is a per-capability declaration reviewed at
  approval time.
- **PII redaction is key-based, not content-based.** An account number rendered
  in a screen dump is retained; a full name in an accessible name is retained.
  Sufficient for credentials, insufficient for a genuine PII regime.
- **Evidence is written unencrypted to the local filesystem** with no retention
  policy — screenshots of account screens included.

---

## 7. Cuts

**Deliberately not built.**

*A co-browsing operator console.* Explicitly out of scope in the brief. The
control-transfer model is the part worth getting right; a real browser window
plus a terminal prompt is a legitimate minimal operator surface.

*A second surface implementation.* The seam is designed, enforced by tests, and
demonstrated once. A stub desktop surface would have proven the interface
compiles, not that the abstraction is right.

*Vision-assisted locating.* The AX tree carried this target completely. Adding a
screenshot rung before it was needed would have been speculative.

*Applying `TenantBinding` at replay.* The schema is designed; the application
path is not written. This is the cut I am least comfortable with, because it
leaves §3.7's second bullet as a claim.

*Parallelism, queueing, and a scheduler.* Single-run correctness first.

**What I would build next, in order.**

1. **`apply_binding()` plus a Cascade replay.** Highest value per line: turns
   multi-tenant reuse from a schema into a demonstration, needs no discovery run,
   and the target app already models the drift.
2. **Enforce approval.** Refuse unattended invocation of a `draft` artifact;
   add a `review` command that shows a human exactly what was inferred —
   parameter names, risk classes, detectors — and promotes to `approved`. This
   is what makes "the artifact is a contract" true rather than aspirational.
3. **Table-aware extraction (`row_cell`).** Several outputs are declared and
   dropped because they sit in table rows with no safe anchor. The compiler
   correctly refuses to guess; it should instead learn to express "the Amount
   cell of the row whose Reference is X".
4. **A canary scheduler.** Per-tenant read-only replay on a schedule, writing
   `last_verified` and `fallback_rate` into each binding. Turns drift detection
   from a number on a run into an operational signal.
5. **Content-based redaction and evidence retention.** Required before this
   touches real regulated data.

**One thing I would reconsider entirely.** Discovery currently records what the
model *did*, including incidental choices — on one run it set the From account
explicitly, on another it left the screen default and the compiler produced a
capability with one fewer parameter. Both are faithful recordings; only one is a
good capability. A "rehearsal" pass — replay the fresh artifact immediately with
perturbed inputs and require it to still succeed — would catch that class of
latent fragility at compile time rather than in production.
