# Understudy

An LLM rehearses a back-office task once. A deterministic understudy performs it
every time after that — and calls a human on stage when it can't.

This is a backend integration layer for AI agents operating legacy bank and
credit-union applications that expose **no API**. The model discovers how to
accomplish a goal by driving a real UI; the successful run is compiled into a
typed, versioned **capability artifact**; and that artifact is replayed
deterministically, with no model in the decision loop, as the production
execution path.

> **The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the AI agent invokes it in production.**

---

## Contents

- [The goal this was built around](#the-goal-this-was-built-around)
- [Setup](#setup)
- [Demo path](#demo-path) — the exact commands
- [System design](#system-design)
- [What each file is for](#what-each-file-is-for)
- [Design decisions worth defending](#design-decisions-worth-defending)
- [Known limitations](#known-limitations)

---

## The goal this was built around

> Log in. Open savings account 13566 and note its current balance. Then open
> Transfer Funds, enter an amount of 25, set the From account to checking 13344
> and the To account to savings 13566, and continue to the confirmation screen.
> Ask the operator to press Submit Transfer. After they hand control back,
> re-open savings account 13566 and report its updated balance.

This goal was chosen because it exercises every hard part at once:

| The goal says | Which forces |
|---|---|
| "Log in" | a human-in-the-loop credential handoff — the system never holds the password |
| "note its current balance" | an output captured **mid-run**, on a screen that no longer exists by the end |
| "enter an amount… set the From… set the To…" | typed input parameters, not values frozen into the recording |
| "Ask the operator to press Submit Transfer" | a second handoff, for an **irreversible** action the agent is forbidden to perform |
| "After they hand control back" | pause → cede control → resume **on the same live session** |
| "report its updated balance" | a second output, read after the mutation, that must differ from the first |

The compiled capability is
[`artifacts/meridian.transfer.execute@1.2.0.json`](artifacts/) — 12 steps, 4
typed inputs, 6 typed outputs, 13 declared outcomes.

---

## Setup

Python 3.11+ (developed on 3.13).

```bash
git clone <this repo> && cd interface

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium        # the browser itself, ~150 MB
```

### Configuration

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | **discovery only** | Never needed for replay. See below. |
| `BANK_USER` / `BANK_PASSWORD` | unattended replay only | An attended run asks you to type them instead. |
| `BANK_IDLE_TIMEOUT` | optional | Session timeout in seconds (default 90). |
| `API_PORT` | optional | Capability API port (default 8100). |

There are no other keys, no cloud services, and no external network calls. The
target application runs locally.

### Running without live services

**Everything except discovery runs with no API key at all.** Replay, the error
taxonomy, the capability catalog, the HTTP API, and the whole test suite are
model-free by construction — `understudy/replay.py` does not import `anthropic`.
You can prove it rather than trust it:

```bash
env -u ANTHROPIC_API_KEY python3 -m pytest tests/ -q      # 78 passed
```

If you have no API key, skip [step 2](#2-discovery-the-llm-drives-the-real-ui)
of the demo — the compiled artifact is committed, so replay works immediately.

### Check the machine before you start

```bash
python3 scripts/doctor.py          # interpreter → packages → browser →
                                   # credentials → app → surface → artifacts
                                   # → replay → tests, in dependency order
python3 scripts/check_model_access.py   # one real API call; prints the
                                        # server-issued request_id proving it
```

`doctor.py` prints the exact command that fixes whatever failed, in dependency
order, because a missing browser makes the nine checks after it fail for
uninteresting reasons.

---

## Demo path

### 0. Start the target application

```bash
python3 -m targets.legacy_bank.app         # http://127.0.0.1:8099
```

Leave it running in its own terminal. Sign-in for the Meridian tenant is
`jsmith` / `demo1234`.

This is a deliberately **hostile** app: framesets, table layout, no test IDs,
inputs with no `<label>` association, repeated visible text, a `<div>` acting as
a button, full page loads, and an idle session timeout. Every anti-pattern is
intentional — see [`targets/legacy_bank/README.md`](targets/legacy_bank/README.md).

### 1. Reset to known balances — before *every* run

```bash
curl -X POST http://127.0.0.1:8099/_reset
```

Savings 13566 = `$4820.55`, checking 13344 = `$1250.00`.

**Run this before each of the steps below, not just once.** A transfer really
moves money, so a second run starts where the first one finished: reset once and
discovery reports `$4820.55 → $4845.55` while the replay after it reports
`$4845.55 → $4870.55`. Both are correct — the capability reads the balance off
the screen rather than replaying a recorded number — but comparing two runs is
only meaningful from the same starting state.

`/_reset` sits **outside the agent's allowlist** (`denied_paths` in
`policy.py`), so the automation can never reset the world it operates on. That
is also why neither CLI takes a `--reset` flag: putting one there would make the
guardrail look decorative for the sake of saving a command. The demo scripts in
step 5 call it themselves before every single case, which is why their numbers
are identical run to run.

### 2. Discovery: the LLM drives the real UI

```bash
curl -X POST http://127.0.0.1:8099/_reset      # start from $4820.55

python3 -m understudy.discover --credentials human --goal "Log in. Open savings account 13566 and note its current balance. Then open Transfer Funds, enter an amount of 25, set the From account to checking 13344 and the To account to savings 13566, and continue to the confirmation screen. Ask the operator to press Submit Transfer. After they hand control back, re-open savings account 13566 and report its updated balance"
```

A browser opens. **The run pauses twice and hands you control:**

1. **at the sign-on screen** — you type the username and password. The system
   never sees them.
2. **at Submit Transfer** — the agent refuses an irreversible funds movement and
   asks you to press it.

Press Enter in the terminal after each. Expect ~9 model calls and ~35 s of
model time; wall-clock is longer, because it waits for you at both handoffs.
It ends with:

```
[discovery] status success
[discovery] outputs
              balance_before: $4820.55
              balance_after:  $4845.55
[discovery] evidence evidence/discovery-<timestamp>/journal.jsonl
```

### 3. Compile the run into a capability

```bash
python3 -m understudy.compile_run --capability meridian.transfer.execute --version 1.2.0
```

Defaults to the most recent run; pass `--run <id>` to pick another. It prints
the contract — inputs, outputs, outcomes, steps, which steps need an operator —
and writes `artifacts/meridian.transfer.execute@1.2.0.json`.

### 4. Deterministic replay — no model

```bash
curl -X POST http://127.0.0.1:8099/_reset      # same starting state as step 2

python3 -m understudy.replay_run \
  --artifact artifacts/meridian.transfer.execute@1.2.0.json \
  --input account_id=13566 --input amount=25 \
  --input from_account=13344 --input to_account=13566
```

Attended is the default: a browser opens, you type the credentials, you press
Submit Transfer. The agent still won't.

```
status      ok
outputs
  balance_before               $4820.55
  transfer_amount              $25.00
  from_account                 13344 (CHECKING)
  to_account                   13566 (SAVINGS)
  balance_after                $4845.55
  available_after              $4845.55
steps run   12
duration    2138 ms
model calls 0
fallbacks   0% of steps
```

**2.1 seconds against ~35, and zero model calls.** Add `--headless` to hide the
browser and `--json` for machine-readable output.

`--unattended` takes credentials from `BANK_USER`/`BANK_PASSWORD` instead of
asking. It does **not** make the transfer complete on its own — that is the
point:

```
status      needs_human
determined at s10_request_human_step
  expected  an operator to complete this step
  observed  no operator is attached to this run
steps run   10          (of 12)
```

The credential steps are covered by a vault; the irreversible one is not. An
unattended caller gets `needs_human` (exit 3) rather than a machine pressing
Submit Transfer, and it knew before invoking — `requires_operator` is in the
capability's advertised contract.

### 5. See errors handled

```bash
python3 scripts/demo_transfer_errors.py    # 8 real replays, one per condition
python3 scripts/demo_bad_inputs.py         # 8 replays with deliberately wrong inputs
python3 scripts/demo_errors.py             # 9 across both capabilities
```

All three run with `ANTHROPIC_API_KEY` unset.

```
BASELINE            exit 0    transfer completes         $4820.55 → $4845.55
BUSINESS OUTCOMES   exit 2    validation_error · insufficient_funds ·
                              record_not_available · access_denied
RECOVERABLE         exit 0    unexpected dialog · transient slowness ·
                              session timeout        (all three complete anyway)
HARD FAILURE        exit 4    app_server_error + screenshot
```

To trigger one by hand, change a single input or inject one fault:

```bash
--input amount=999999            # insufficient_funds       exit 2
--input amount=abc               # validation_error         exit 2
--input account_id=99999         # record_not_available     exit 2
--input from_account=13566       # invalid_account_selection exit 2
--fault-dialog overview.htm      # dismissed, run completes exit 0
--fault-delay transfer.htm:1500  # waited out, run completes exit 0
--fault-status transfer.htm:500  # app_server_error         exit 4
```

Or type a wrong password at the attended pause and watch it stop at
`s2_enter_username` with `wrong_password`.

### 6. The agent-facing capability catalog

```bash
python3 -m understudy.api          # 127.0.0.1:8100

curl -s localhost:8100/capabilities | python3 -m json.tool
curl -s -X POST localhost:8100/capabilities/meridian_account_balance_and_last_activity/invoke \
  -H 'Content-Type: application/json' -d '{"inputs":{"account_id":"13566"}}'
```

```json
{ "status": "ok",
  "outputs": { "account": "13566", "current_balance": "$4820.55",
               "available_balance": "$4820.55",
               "latest_transaction_date": "2026-08-14" },
  "model_calls_inside_capability": 0,
  "duration_ms": 1137 }
```

No model is invoked by any endpoint. Selection without a model is also available
as a CLI:

```bash
python3 scripts/route_invoke.py --list
python3 scripts/route_invoke.py --intent read_balance --input account_id=13566
```

### 7. Tests

```bash
python3 -m pytest tests/ -q        # 78 passed
```

---

## System design

```
     GOAL (natural language)  +  TARGET (url)
                    │
                    ▼
        ┌───────────────────────┐
        │   DISCOVERY  (LLM)    │   understudy/agent.py
        │  observe→decide→act   │   understudy/discover.py
        └───────────┬───────────┘
                    │  writes every observation, decision and action
                    ▼
        ┌───────────────────────┐
        │   JOURNAL  (JSONL)    │   understudy/journal.py
        │  + screens, request_ids│
        └───────────┬───────────┘
                    │  read once, offline
                    ▼
        ┌───────────────────────┐
        │      COMPILER         │   understudy/compiler.py
        │ transcript → contract │
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────┐
        │  CAPABILITY ARTIFACT  │   understudy/artifact.py
        │  typed · versioned    │   artifacts/*.json
        └───────────┬───────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
┌───────────────┐      ┌────────────────┐
│    CATALOG    │      │ DETERMINISTIC  │   understudy/replay.py
│  tools / HTTP │─────▶│     REPLAY     │   ← no model, ever
└───────────────┘      └───────┬────────┘
 catalog.py · api.py           │
                               ▼
                    ┌──────────────────────┐
                    │  SURFACE (protocol)  │   understudy/surface.py
                    │  observe/act/wait/   │
                    │  screenshot          │
                    └──────────┬───────────┘
                               ▼
                        the real application
```

Two rules hold everywhere:

**Policy sits on the only path to the surface.** `understudy/policy.py` is not
advice in a prompt the model can talk itself out of — it is on the call path, so
neither discovery nor replay can route around it.

**The artifact is the contract.** The tool definition an agent sees is *derived
from the artifact*, so what is advertised cannot drift from what replay does.

### How elements are identified (§3.2's "reasoning about robustness")

No CSS. No XPath. No element handles. A test asserts none ever appear in an
artifact. Instead each step carries a **ranked ladder** of semantic descriptors:

| Rung | Strategy | Confidence | Survives |
|---|---|---|---|
| 0 | `role_name` | 1.00 | layout changes |
| 1 | `role_anchor` | 0.90 | an input gaining/losing its label |
| 2 | `text` | 0.80 | a `<button>` becoming a `<div onclick>` |
| 3 | `ordinal` | 0.65 | — |
| 4 | `coords` | 0.45 | *reserved, not implemented* — scored below the floor so it cannot be used silently |

Replay walks the ladder top-down and refuses anything below the step's
confidence floor (0.8). **Two candidates with no way to tell them apart is an
error, not a coin flip.** Which rung matched is reported as `fallback_rate` —
the drift signal that says a flow still works but is working harder.

Data is *parameterised*, not frozen: the recorded run clicked a link named
`13566`, and the artifact stores `name_param: account_id`. Otherwise every
replay would open exactly one account forever.

### The result contract (§3.3)

| Tier | Meaning | What replay does | Exit |
|---|---|---|---|
| `ok` | success | returns typed outputs | 0 |
| `business_outcome` | a legitimate answer | stops on purpose, returns the code | 2 |
| `needs_human` | an operator is required | escalates with context | 3 |
| `failed` | hard failure | stops + screenshot + context | 4 |

Recoverable conditions never surface as a status — they are *handled* and the
run completes as `ok`. Recovery is per-condition and **bounded**: an interstitial
is `dismiss`ed, an expired session is `reauth`ed, a stall is waited out. A
condition that survives its attempt budget escalates rather than looping.

Every non-success carries **what step, what was expected, what was observed**:

```
status      business_outcome
outcome     wrong_password
reason      Wrong password: the username is a real account, but the password
            typed at the pause does not match it. Nothing was retried --
            repeating a rejected password risks a lockout.
determined at s2_enter_username
  expected  the password entered at the pause to match the username that was
            entered with it (during 'Enter username')
  observed  the application said: 'The password entered for that username is
            not correct.' | detector 'wrong_password' matched | url=...
```

### Safety (§3.4)

- **Allowlist** — origins *and* action kinds. `/_reset` is on `denied_paths`.
- **Risk classes** on every step: `read`, `reversible`, `mutating`,
  `credential`, `irreversible`. The irreversible class is **blocked**, not
  confirmed — a control matching `irreversible_labels` ("submit transfer",
  "close account", …) is handed to a person. Blocking rather than confirming is
  the conservative choice because a confirmation prompt is one more thing an
  agent can learn to click through; a structural refusal is not.
- **Secrets never exist to be leaked.** `type_secret` receives the *name* of a
  secret, never its value. Redaction of `password`/`token`/`pin`/`secret` in the
  journal is belt-and-braces on top of that. During a handoff, evidence capture
  is **suspended** (`capture: "suspended"`) so a screenshot cannot catch a
  password mid-typing.

### Human-in-the-loop (§3.6)

One mechanism, two triggers: **credential entry** and **irreversible action**.
Both pause the automation, hand you the *same live browser session* (not a fresh
one), wait, and resume. The journal records `intervention_raised` and
`intervention_resolved` with the actor, so who was in control at each moment is
always answerable. It works identically in discovery and in replay.

---

## What each file is for

### `understudy/` — the system

| File | Lines | What it does |
|---|---|---|
| `surface.py` | 403 | **The seam.** `Surface` protocol (`observe`/`act`/`screenshot`/`wait`) + `WebSurface`, which reads the CDP accessibility tree — including inside framesets — rather than the DOM. Replay depends on the protocol, not on Playwright. |
| `policy.py` | 72 | Allowlist, action kinds, irreversible labels, step and time budgets. On the call path. |
| `agent.py` | 500 | The LLM loop. Tools: `click`, `type`, `type_secret`, `select`, `request_human`, `finish`. Captures a semantic descriptor of every target *before* acting. |
| `discover.py` | 113 | Discovery CLI. `--goal`, `--target`, `--credentials {env,human}`, `--max-steps`. |
| `journal.py` | 195 | JSONL evidence: observations (with screens), decisions (with `request_id`), actions, interventions, final screen. Redaction lives here. |
| `compiler.py` | 754 | Transcript → contract. Derives locator ladders, parameterises data, infers types, derives extraction rules, declares outcomes. |
| `compile_run.py` | 80 | Compiler CLI. |
| `artifact.py` | 416 | The **schema** — Pydantic v2, JSON-Schema-exportable, `tool_signature()` for agents. |
| `replay.py` | 859 | The production path. Resolution, checkpoints, detectors, recovery, extraction, escalation. Imports no model client. |
| `replay_run.py` | 171 | Replay CLI, including `--fault-*` injection. |
| `catalog.py` | 143 | Artifacts as callable tools. |
| `api.py` | 118 | The same catalog over HTTP. |
| `faults.py` | 141 | Fault injection **beneath** the surface driver, so replay cannot tell an injected failure from a real one. |

### `targets/legacy_bank/` — the target application

Not part of the system; it exists to be driven. Two tenants (Meridian, Cascade)
of the same fictional vendor product, differing in labels, frame names, and one
extra interstitial — the real shape of multi-tenant drift.

### `scripts/` — demos and diagnostics

| Script | Purpose |
|---|---|
| `doctor.py` | Nine checks in dependency order; prints the fix, not a stack trace. |
| `check_model_access.py` | One real API call, prints the `request_id` that proves it. |
| `demo_transfer_errors.py` | The full taxonomy against the transfer capability. |
| `demo_bad_inputs.py` | Deliberately wrong inputs. |
| `demo_errors.py` | The taxonomy across both capabilities. |
| `route_invoke.py` | Capability selection with **no model** — by name or intent tag. |

### `tests/` — 78 tests

| File | Tests | Covers |
|---|---|---|
| `test_target_app.py` | 19 | that each declared outcome has a real, distinguishable screen behind it |
| `test_artifact.py` | 17 | schema, compilation, no selectors, no credentials, mid-flow extraction |
| `test_replay.py` | 29 | resolution, ambiguity, recovery bounds, dismiss-without-repeat, outcome wording |
| `test_handoff.py` | 7 | pause, control transfer, resume, capture suspension |

---

## Design decisions worth defending

**The accessibility tree, not the DOM.** The brief asks for an approach that
still works when the surface has no clean DOM. Roles and accessible names are
what a *desktop* automation API exposes too, so the same artifact shape extends
to a surface with no DOM at all.

**Ambiguity is an error.** Guessing between two matching controls is how a
capability silently transfers money from the wrong account.

**A business outcome is not a failure.** "No such member" is a legitimate
result. Conflating it with a crash is the mistake the brief calls out, and it is
why there are four statuses rather than two.

**Recovery is bounded and per-condition.** Collapsing "dismiss the dialog",
"sign in again", and "wait" into one generic retry leaves the first two spinning
until the budget runs out.

**Dismiss does not repeat the step.** An interstitial appearing *after* a step
succeeded is dismissed and the run continues. Re-running the step there would be
a double submission on a transfer.

**Outputs are read where they were seen.** The pre-transfer balance lives on a
screen that no longer exists at the end of the run. Every output records the
step it was captured on, and replay keeps that screen. Reading everything off
the final screen returns nothing for it — or worse, the post-transfer figure
labelled "before".

**Artifacts compile to `draft`.** Inference is good enough to be useful and not
good enough to trust unattended.

---

## Known limitations

Stated plainly rather than discovered by a reviewer.

- **`approval: draft` is advisory.** `compile_run` warns; nothing enforces it.
  A draft artifact is still invocable through the catalog and the API. There is
  no draft → approved promotion path.
- **`requires_operator` is unconditional.** It is `true` whenever operator steps
  exist, even when a vault could supply the credentials — as it does for the
  balance capability, which completes unattended despite advertising otherwise.
  Conservative, but the contract says something untrue about itself.
- **`TenantBinding` is schema-only.** The overlay is modelled (base URL,
  per-step overrides, `insert_before` for a tenant's extra interstitial, drift
  status) and unit-tested, but never produced by the compiler or applied by
  replay. No Cascade artifact exists, so multi-tenant reuse is a design claim,
  not a demonstration.
- **One surface implementation.** The `Surface` protocol is real and enforced,
  but only `WebSurface` implements it.
- **`demo_errors.py` prints `exit=3 ?`** for the needs-human row — a formatting
  bug in that script's renderer, not in the engine.
- **The two tenants share one `ACCOUNTS` dict** in the target app's seed data.
  A target-app wart, not a system one.
