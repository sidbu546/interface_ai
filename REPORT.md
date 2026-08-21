# Understudy — design write-up

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
client; `grep -c anthropic understudy/replay.py` returns 0. That is the
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
which means they are data rather than labels — it emits **no rule at all**.
Declaring an output the engine cannot fetch is bad; returning a confidently
wrong number is worse.

**Risk is per step**, not per capability: `read`, `reversible`, `mutating`,
`credential`, `irreversible`. A caller can see *where* a capability becomes
dangerous, not just that it is.

**Everything compiles to `draft`.** Parameter naming, risk classification and
outcome detectors are inferred. Inference is good enough to be useful and not
good enough to trust unattended, and the schema should say so.

---

## 3. Determinism & error handling

Everything below is measured from `meridian.transfer.execute@1.2.0` — 12 steps,
run against the live app. The baseline it is all compared to:

```
exit=0  ok   steps=12/12   2113 ms   fallbacks=0%   model calls=0
outputs  account 13566 (SAVINGS) · balance_before $4820.55 · transfer_amount $25.00
         balance_after $4845.55 · available_after $4845.55
         checking_13344_balance_after $1225.00   ← the other side of the transfer
```

### What actually makes it repeatable

**Nothing is located by guessing.** Each step resolves through its ranked
descriptors and refuses anything scoring under 0.8. Across every run in this
section — success, business outcome, recovery, hard failure — `fallbacks` came
back **0%**, meaning every control was found on rung 0 by role and accessible
name. That number is not decoration: it is the difference between a run that
worked and a run that worked *by falling back*, and it is reported on every
result so the second kind is visible before it becomes the third kind.

**Absence is concluded, not assumed.** The `record_not_available` run took
**4343 ms** against the baseline's 2113 — because when the account link did not
appear, resolution kept re-observing until its timeout before deciding it was
genuinely absent. A cheap `if not found: fail` would have returned in 200 ms and
been wrong on any slow page. Every wait in the engine is a wait *for* a named
condition with a timeout; there is not one fixed sleep in the replay path.

**Failures land where they happen.** Because the screen is re-checked against
every declared condition after each step, the eight runs stop at five different
places — s4, s5, s6, s9 — each the step that actually produced the problem:

| Run | Stops at | steps | ms | exit |
|---|---|---|---|---|
| baseline | — | 12/12 | 2113 | 0 |
| `record_not_available` | `s4_open_savings_account_13566_detail` | 4/12 | 4343 | 2 |
| `app_server_error` | `s5_open_transfer_funds` | 5/12 | 912 | 4 |
| undeclared wrong page | `s6_enter_transfer_amount_25` | 6/12 | 5129 | 4 |
| `validation_error` | `s9_continue_to_confirmation_screen` | 9/12 | 1234 | 2 |
| `insufficient_funds` | `s9_continue_to_confirmation_screen` | 9/12 | 1230 | 2 |

Without per-step detection, the 500 at step 5 would not surface until step 6
tried to type into a page that was never served, and the report would blame the
wrong step.

### What the caller experiences, by tier

**Recoverable — the run finishes and the caller sees nothing unusual.** Two
conditions were injected mid-flow. Both completed all 12 steps and returned
outputs *byte-identical to the baseline*:

```
session_expired          exit=0  12/12  2979 ms   balance_after $4845.55
  -> recoverable: session_expired (attempt 1/2, action=reauth)
  -> re-authenticating
  -> recovered from session_expired
  -> handing control to the operator: press "Submit Transfer" ...

unexpected_interstitial  exit=0  12/12  2387 ms   balance_after $4845.55
  -> recoverable: unexpected_interstitial (attempt 1/2, action=dismiss)
  -> recovered from unexpected_interstitial
```

The only trace in the result is the clock: 2979 ms and 2387 ms against 2113. The
session was destroyed underneath a running transfer and the transfer still
completed, with the operator handoff still happening afterwards in the right
place.

The two responses are deliberately different code paths. Re-authentication
replays the credential steps and resumes at the interrupted one; dismissal
clicks the notice's own control and carries on. A single generic "retry" would
have re-clicked Transfer Funds at a dead session forever, and would have
re-clicked *through* a modal that was swallowing the click. Each condition
declares its own `max_attempts` — both showed `attempt 1/2` — and exhausting it
escalates to a human rather than looping.

After a dismissal the run **continues**; it does not re-run the step. The notice appeared *after* `s8` had already succeeded.
Repeating that step on a transfer flow is how you submit twice.

**Business outcome — the run stops early and answers.** These are not errors.
The app was asked something it declined, and the caller needs to know which:

```
insufficient_funds   at s9_continue_to_confirmation_screen
  expected  the source account to hold at least the requested amount
  observed  the application said: 'Insufficient available funds for this transfer.'

record_not_available at s4_open_savings_account_13566_detail
  expected  the requested record to be listed for the signed-in operator;
            tried rung 0 (link named {account_id}): no match
  observed  screen text: Accounts Overview / Total Balance: $6070.55 ...
```

Note what `expected` says. It is the *condition's* own sentence — the compiler
stores an expectation beside each detector — not a generic "the step should have
worked". An operator reading `insufficient_funds` learns what would have had to
be true; reading the earlier boilerplate they learned only that a rule matched.
And for `record_not_available` the ladder is printed too, because "the record is
genuinely absent" and "your locator broke" look identical from outside, and the
rungs are how you tell them apart.

**Hard failure — stop, and leave something to debug with.**

```
app_server_error  exit=4  at s5_open_transfer_funds  after 912 ms
  expected  the application to serve the page rather than an error
  observed  the application said: '500 Internal Server Error'
  evidence  evidence/report-v3   (failure.png + failure.json)
```

It fails in 912 ms — faster than a success — because there is nothing sensible
to retry. Retrying an outage burns time and produces the same report later.

### The case nobody declared

The three tiers only cover conditions someone wrote down. So the flow was
attacked with one nobody had: the Transfer Funds link was made to answer with
the **Accounts Overview page** — HTTP 200, valid HTML, simply the wrong screen.
No detector fires here, and none could: every detector looks for something wrong
on screen, and nothing is wrong. It is just not the screen the flow expects.

```
exit=4  code=None  at s6_enter_transfer_amount_25
  expected  rung 0 (textbox labelled by '$'): no match
  observed  interactive controls present: ... link:Accounts Overview,
            link:Transfer Funds, link:Log Out
```

It stopped anyway, because the ladder is not hunting for "an input" but for *a
textbox anchored by a cell reading `$`*, and no such thing exists on the
overview screen. `code=None` is the honest signal — stopped safely, but this is
not a condition anyone anticipated. The alternative is typing `25` into whatever
field happens to be present and clicking whatever resembles Continue.

### UI drift, secondarily

The ladder absorbs drift and `fallback_rate` measures it. A relabelled control
falls to the anchor rung, a `<button>` becoming a `<div onclick>` falls to the
text rung, and the run still succeeds — but its fallback rate rises from the 0%
recorded here. That is the signal to re-record: the capability still works, it
is working harder, and the number says so before anything breaks.

---

## 4. Heterogeneity & multi-tenant

### The surface seam

The boundary is one protocol, declaring exactly what the engines call:

```python
class Surface(Protocol):
    # perceiving
    def observe(self) -> UISnapshot: ...
    def resolve(self, ref: int) -> UINode: ...
    # acting
    def navigate(self, url: str) -> str: ...
    def click(self, ref: int) -> str: ...
    def type_text(self, ref: int, text: str) -> str: ...
    def select_option(self, ref: int, value: str) -> str: ...
    def submit_form(self, ref: int) -> str: ...
    # housekeeping
    def screenshot(self, path: str) -> None: ...
    def wait(self, milliseconds: int) -> None: ...
```

That list is the result of a correction worth recording. An earlier version
declared a single generic `act(action, **kwargs)` — which **nothing ever
called**, while discovery and replay used concrete methods directly, one of them
private (`_resolve`). The declared seam was narrower than the real one, so
anyone implementing a second surface against it would have satisfied the
protocol and still failed. Declaring the true interface is what makes the
abstraction checkable rather than decorative.

`UISnapshot` is `url · title · nodes · frame_urls · text`, and a `UINode` is
`role · name · value · frame · anchor · interactive`. It also carries a `ref` —
an index into the current snapshot, plus a CDP backend id — but those are
ephemeral addressing handles that expire with the snapshot and are **never
persisted into an artifact**; a compiled step refers to a control only by role,
name, anchor and frame. No selector, no tag name, no durable element handle
crosses the seam. The recorded flow is expressed entirely in those terms, so
**everything above the seam is surface-agnostic**: the compiler, the schema, the
resolution ladder, the detectors, the recovery model.

This is enforced, not aspirational, in three ways. Both engines are annotated
against `Surface`, never the concrete `WebSurface`. Neither touches a
Playwright-only attribute — when `surface.page` once leaked into replay to
implement a wait, a test caught it and the fix was to add `wait()` to the
protocol. And the engines already run against non-browser implementations: seven
plain-Python surface doubles in the test suite drive the full replay engine —
resolution, detectors, recovery, handoff — with no browser present.

**Extending to a desktop surface** means implementing those four methods over
UIA/AX/AT-SPI. `role` and `name` map almost directly — that is where the
accessibility-tree choice pays off. Two things need genuine work: `frame`
generalises to a window/pane identifier, and `anchor` (nearby text that gives an
unnamed control meaning) needs a spatial heuristic rather than table adjacency.
For a surface that exposes neither an accessibility tree nor stable text — a
remoted session, a canvas-drawn UI — the design intent is a geometric bottom
rung and a vision-assisted rung above it. **Neither is built.** The strategy
enum reserves a `coords` slot and scores it 0.45, deliberately *below* the 0.8
confidence floor, so such a rung would be refused rather than silently trusted;
but `Descriptor` carries no coordinates, `_matches()` never consults geometry,
and there is no policy gate for it. `Surface.screenshot()` exists and is used
for failure evidence, not for locating.

The ladder's state, exactly: the top three rungs are derived by the compiler and resolved by replay; `ordinal` is resolved (it disambiguates
when several candidates match) but never emitted by the compiler; `coords` is
neither. So the honest limitation is larger than one missing surface: **only
`WebSurface` exists, and the rung that a DOM-less surface would lean on hardest
is the one that is only a reserved slot.** The seam is well-designed and
enforced by tests, but it has been demonstrated exactly once, against a surface
that happens to expose a good accessibility tree.

### Multi-tenant reuse

Hundreds of tenants running ~20 apps, many sharing a vendor product. Re-recording
per tenant is the obvious approach and it does not scale: 2,000 recordings, each
independently rotting.

The artifact should therefore be keyed to the **vendor product**, not the
institution, with per-institution differences in a small overlay. The schema is
built for that: `AppProfile` carries `product` / `version_range` /
`surface_kind`, and the recording tenant is held separately in
`provenance.recorded_on_tenant`, where it belongs — an artifact's identity is
what it automates, not where it happened to be recorded.

**The compiled instances do not yet honour this.** `app.product` defaults to
`"meridian-core"` and capability ids read `meridian.transfer.execute` — both
named after the tenant the run happened on. Onboarding Cascade today would
therefore produce a *separate* capability rather than a binding, which is
exactly the outcome this design exists to avoid. It is a compiler default and an
id convention, not a structural constraint: the fields that make product-keying
possible already exist and nothing above them assumes otherwise. Until it is
changed, though, the artifacts on disk carry the tenant's name where the
product's belongs.

The overlay itself:

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

**Status:** `TenantBinding` is modelled and unit-tested but never produced by
the compiler or applied by replay, and no Cascade artifact exists.
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
own: *"Submitting the transfer is an irreversible funds movement that I am not
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

### What was left unbuilt, and what stands in its place

**The operator surface is a terminal prompt.** Escalation routes through
`_pause_for_human()` in `agent.py` and `_terminal_operator()` in
`replay_run.py`: the run prints the step id, the reason, and what needs doing,
then blocks on `input()` while the person works in the browser window that is
already open. No queue, no console, no authentication of who picked it up. What
that buys is the part that is hard to retrofit — control transfer on the *same*
session, `capture: "suspended"` while a human types, and an
`intervention_raised` / `intervention_resolved` pair in the journal recording
who held control. A console is a front end onto that; the seam underneath it is
what would have been expensive to get wrong.

**One surface implementation.** `WebSurface` is the only class implementing the
`Surface` protocol. The engines are annotated against the protocol and touch no
Playwright attribute, and seven plain-Python doubles in the test suite already
drive the full replay engine without a browser — so the boundary is exercised,
not merely declared. A stub desktop surface would have shown the protocol can be
implemented twice, which is not the same as showing the vocabulary
(`role · name · anchor · frame`) survives contact with UIA or AT-SPI.

**The `coords` rung is a reserved slot.** It sits in the strategy enum scored
0.45, below the 0.8 floor, so a descriptor using it would be refused rather than
trusted. `Descriptor` carries no coordinates and `_matches()` never consults
geometry. The accessibility tree resolved every control in this target on rung
0 — `fallback_rate` was 0% on every run in §3 — so building a geometric rung
would have meant writing code no run could exercise.

**The second tenant was built and never driven.** Cascade exists in
`tenants.py` as a full configuration of the same vendor product. It differs
from Meridian in 23 configuration fields — branding, column headers and
interstitial copy among them. The ten that matter for locating a control are
these, chosen because they are what breaks naive replay:

```
label_username   'Username:'         ->  'Member ID:'
label_password   'Password:'         ->  'PIN / Password:'
label_signin     'Log In'            ->  'Sign On'
nav_transfer     'Transfer Funds'    ->  'Move Money'
nav_overview     'Accounts Overview' ->  'My Accounts'
frame_nav        'navframe'          ->  'menu'
frame_content    'contentframe'      ->  'body'
frame_status     'statusframe'       ->  'footer'
interstitial      False              ->  True      (a compliance screen mid-flow)
product_version  '8.2.1'             ->  '8.2.4'
```

Four tests in `test_target_app.py` pin these down — the interstitial forcing a
redirect, the relabelled columns, the renamed frames, and Cascade's refusal to
say which half of a credential failed. `doctor.py` checks both tenants are
serving. `compiler.py` even detects the tenant from the target URL and records
it in provenance.

What never happened: no discovery run was made against `/cascade/`, no Cascade
artifact exists, and no replay has ever pointed at it. Every artifact, every
evidence bundle, and every demo in this repository is Meridian. So the hardest
part of the multi-tenant claim — that a Meridian-recorded flow can be *reused*
rather than re-recorded — has a target built to test it and no test run against
it.

**`apply_binding()` does not exist either.** `TenantBinding` validates,
serialises, and has a test asserting it stays an overlay rather than a copy of
the flow, but nothing produces one and replay cannot consume one. Of everything
here this is the weakest cut: a Cascade binding is roughly 500 bytes against an
18 KB artifact and touches 4 of the 12 steps — the two credential steps for the
relabelled fields, and the two nav clicks for `Move Money` and `My Accounts`.
The other eight steps are unchanged, which is the entire hypothesis, unverified.

**No concurrency anywhere.** One run, one browser, one process; evidence written
to the filesystem. Every timing in §3 is a single-run measurement. Correctness
of one replay is the thing the rest would be built on top of.

### Next, in order

1. **Key artifacts to the product, then bind.** `app.product` is
   `"meridian-core"` and the capability id is `meridian.transfer.execute` —
   both named after the tenant the recording happened on, while
   `provenance.recorded_on_tenant` already holds that fact separately. Fixing
   the naming is a compiler default and an id convention. It has to land before
   `apply_binding()`, because overlaying Cascade onto an artifact called
   `meridian.*` would preserve the confusion instead of removing it. The
   demonstration that closes this is one command: replay the existing
   Meridian-recorded artifact against `http://127.0.0.1:8099/cascade/` through a
   binding, and show it completing — no discovery run, no model, no API key.
   Cascade's mid-flow interstitial makes it a genuine test rather than a
   relabelling exercise, since the binding has to insert a step the recorded
   flow never had.

2. **Make `approval` mean something.** `compile_run` prints a draft warning;
   `catalog.py` and `api.py` *report* the approval state in the tool
   description, and neither checks it before invoking. So an artifact whose
   parameter names, risk classes and outcome detectors were inferred by a model
   and never reviewed is callable unattended, including one containing an
   `irreversible` step. The work is a gate at `Catalog.invoke()` plus a `review`
   command that prints what was inferred and promotes `draft` to `approved`.

3. **Table-aware extraction.** The compiler derives an anchor from the
   neighbouring cell and emits nothing when the only candidates repeat down a
   column. On the read capability that costs three outputs — `type`,
   `latest_transaction_amount`, `latest_transaction_description` — all of them
   sitting in the Posted Activity table with column values, not labels, beside
   them. The rule those need is positional within a row: *the Amount cell of the
   row whose Reference is X*. Refusing to guess is right; a `row_cell` method
   would let it stop refusing.

4. **Turn `fallback_rate` into a signal.** It is computed on every result and
   read by whoever happens to look. A scheduled read-only replay per tenant,
   writing `last_verified` and `fallback_rate` back into each binding and
   flipping `status` to `needs_review`, is what makes rung-0 drift visible
   before a capability breaks rather than after.

5. **Redaction by content, not by key.** `SECRET_KEYS` in `journal.py` matches
   on argument names — `password`, `token`, `pin`, `secret`. It cannot see an
   account number inside a screen dump or a customer name in an accessible name,
   and evidence bundles are written unencrypted with no retention policy.
   Sufficient for the credential path, insufficient for the data this would
   really run against.

### One thing to reconsider

The compiler records what the model did, including choices it made for reasons
that were not load-bearing. Two discovery runs against the same goal, minutes
apart, produced different capabilities: one set the From account explicitly and
compiled to 12 steps with a `from_account` parameter; the other saw the dropdown
already showing `13344 (CHECKING)`, skipped the step, and compiled to 11 steps
without that parameter. Both recordings are accurate. Only one produces a
capability that still transfers from the right account if the app's default ever
changes.

Nothing in the pipeline can currently tell those apart, because both look like
successful runs. A rehearsal pass would: immediately after compiling, replay the
artifact with perturbed inputs and require it to still reach its success
condition. The eleven-step version would have failed that check the moment the
default moved, at compile time, instead of quietly transferring from whatever
the screen happened to be showing.

