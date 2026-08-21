# Evidence

**[Watch the demo video for this evidence](https://drive.google.com/file/d/1LT_ytfz1XR7PL8wJT_XPkdoWgbaD8j3Y/view?usp=sharing)**

Five retained runs. Each is the log of a command below.

Read them in order: the discovery run that taught the capability, the replay
that performs it, two replays where the application declined, and one where it
broke. Together they cover every exit code the result contract can produce —
**0** success, **2** business outcome, **4** hard failure.

**Reset before every run.** `curl -X POST http://127.0.0.1:8099/_reset` restores
savings 13566 to `$4820.55`. A transfer really moves money, so without a reset
the second run of the day starts where the first finished. All three runs below
were made from a reset state, which is why discovery and replay report the same
figures — and why they can be compared at all.

---

## `discovery-20260819-173231/` — the LLM learning the flow

Produced `artifacts/meridian.transfer.execute@1.2.0.json`. The artifact's
`provenance.run_id` names this directory, so the capability can always be traced
back to the run that taught it.

```bash
python3 -m targets.legacy_bank.app          # terminal 1
curl -X POST http://127.0.0.1:8099/_reset

python3 -m understudy.discover --credentials human --goal "Log in. Open savings account 13566 and note its current balance. Then open Transfer Funds, enter an amount of 25, set the From account to checking 13344 and the To account to savings 13566, and continue to the confirmation screen. Ask the operator to press Submit Transfer. After they hand control back, re-open savings account 13566 and report its updated balance"
```

**36 journal records · 9 screenshots · 9 model calls.**

```
run_started 1 · observed 9 · decided 9 · acted 11
intervention_raised 2 · intervention_resolved 2 · final_screen 1 · run_finished 1
```

The run stopped twice and handed control to a person. Both appear in the journal
with `capture: "suspended"` — evidence capture pauses while a human is at the
keyboard, so no screenshot can catch a password:

```
credential_handoff : a credential step was reached; a person must enter it
human_action       : Submitting the transfer is an irreversible funds movement
                     that I am not permitted to press.
```

The second is the model's own wording. It reached the confirmation screen,
recognised the control as irreversible, and refused it. The guardrail lives in
`policy.py`; the model articulated the reason.

Worth opening:

- `decided` records carry `message_id` and the server-issued `request_id`, so
  every model call is independently verifiable.
- All **9** `observed` records carry the full screen. That is what lets the
  compiler derive an extraction rule for `balance_before`, a value that exists
  only mid-run and is overwritten by the end.
- `grep -ci demo1234 journal.jsonl` → **0**. `type_secret` receives the *name*
  of a secret, never its value.

Compiled with:

```bash
python3 -m understudy.compile_run --run discovery-20260819-173231 \
  --capability meridian.transfer.execute --version 1.2.0
```

## `replay-success/` — the capability performed, no model involved

```bash
curl -X POST http://127.0.0.1:8099/_reset

python3 -m understudy.replay_run \
  --artifact artifacts/meridian.transfer.execute@1.2.0.json \
  --input account_id=13566 --input amount=25 \
  --input from_account=13344 --input to_account=13566 \
  --evidence evidence/replay-success
```

Attended by default: a person types the credentials and presses Submit Transfer,
exactly as in discovery. **exit 0 · 12/12 steps · 2179 ms · zero model calls**,
against roughly 35 s of model time for the run that learned it.

- `final.png` — the Account Detail screen it finished on, Balance **$4845.55**
- `result.json` — outputs, timings, and `locator_rungs`

The outputs cover both sides of the transfer:

```
balance_before                $4820.55     read at step 4, before the transfer
balance_after                 $4845.55     read at step 12, after it
checking_13344_balance_after  $1225.00     read at step 11, from the overview
```

`$1250.00 − $25.00 = $1225.00`. Three screens, three capture points, every value
re-read at run time rather than replayed from the recording.

Two fields in `result.json` matter more than the numbers:

```json
"locator_rungs": { "s2_enter_username": 0, ... },
"fallback_rate": 0.0,
"model_calls": 0
```

Every located control matched on **rung 0** — by role and accessible name, the
strongest descriptor available. A rising fallback rate is the early warning that
a capability is drifting while still passing.

> **Reading the screenshot:** Balance shows $4845.55 while the last row of
> Posted Activity still ends at $4820.55. That is the target app, not the
> capability — `seed.apply_transfer()` moves balances without posting a
> transaction row.

## `outcome-unknown-username/` and `outcome-insufficient-funds/` — the app declining

Neither of these is a bug. The application was asked something it would not do,
and said so; replay stopped and reported which. **exit 2** in both cases, and
each bundle holds `outcome.png` + `outcome.json` — the screen the app was
showing when the decision was made.

```bash
curl -X POST http://127.0.0.1:8099/_reset

# type a username that does not exist at the sign-on pause
python3 -m understudy.replay_run \
  --artifact artifacts/meridian.transfer.execute@1.2.0.json \
  --input account_id=13566 --input amount=25 \
  --input from_account=13344 --input to_account=13566 \
  --evidence evidence/outcome-unknown-username

# sign in correctly, but ask to move more than the account holds
python3 -m understudy.replay_run \
  --artifact artifacts/meridian.transfer.execute@1.2.0.json \
  --input account_id=13566 --input amount=99999 \
  --input from_account=13344 --input to_account=13566 \
  --evidence evidence/outcome-insufficient-funds
```

```
unknown_username     exit 2   stopped at s2_enter_username    2/12 steps
  expected  the username entered at the pause to be an account this
            institution recognises
  observed  the application said: 'That username was not recognised.'

insufficient_funds   exit 2   stopped at s9_continue_to...    8/12 steps
  expected  the source account to hold at least the requested amount
  observed  the application said: 'Insufficient available funds for this transfer.'
```

Two things these bundles are meant to show.

**They stop at different steps.** The credential problem is caught at step 2,
before any account is opened; the funds problem at step 9, after the form is
filled but before anything is committed. Neither run reached the operator
handoff at step 10, so **no money moved in either case**.

**Nothing was retried.** A rejected username is not a transient fault — three
automated attempts is a locked account. The engine treats these as answers to
return, not conditions to recover from, which is why they exit 2 rather than 4.

`outcome.png` for the funds case shows the app's own red banner above the
transfer form. That matters more than the log line: the claim being made is
about what the *application* said, and a screenshot is the only form of that
claim an audit can check independently.

## `replay-failure-app-500/` — what a hard failure leaves behind

The same capability with a server error injected at the Transfer Funds page:

```bash
curl -X POST http://127.0.0.1:8099/_reset

python3 -m understudy.replay_run \
  --artifact artifacts/meridian.transfer.execute@1.2.0.json \
  --input account_id=13566 --input amount=25 \
  --input from_account=13344 --input to_account=13566 \
  --unattended --headless \
  --fault-status transfer.htm:500 \
  --evidence evidence/replay-failure-app-500
```

**exit 4**, stopped at step 5 of 12, zero model calls.

- `failure.png` — the screen at the moment it stopped
- `failure.json` — `step`, `expected`, `observed`, `reason`, `url`,
  `frame_urls`, and the full node list

```
failed at s5_open_transfer_funds
  expected  the application to serve the page rather than an error
  observed  the application said: '500 Internal Server Error'
```

The fault is injected beneath the surface driver, at the transport layer, so the
replay engine cannot distinguish it from a real outage.

---

## Known gap

`artifacts/meridian.account.balance_and_last_activity@1.0.0.json` — the second,
read-only capability — carries a `provenance.evidence_ref` pointing at a
discovery run that has been pruned. The artifact is still valid and still
replays; its lineage is simply no longer on disk, which is a fair thing to hold
against it. One attended run rebuilds it:

```bash
curl -X POST http://127.0.0.1:8099/_reset
python3 -m understudy.discover --credentials human \
  --goal "Log in and open account 13566, then report its balance and latest posted transaction"
python3 -m understudy.compile_run \
  --capability meridian.account.balance_and_last_activity --version 1.0.0
```

## Reproducing the rest

Business outcomes and recoverable conditions leave no bundle — they either
return an answer or complete normally. To see all eight conditions:

```bash
python3 scripts/demo_transfer_errors.py     # resets before every case
```
