# ShiftSwap Intelligence - Explainable, Rule-Aware Staff Scheduling

CityCare Clinic staff post shifts they cannot work, colleagues claim them, and managers approve or
reject the exchange. Every action is checked by a deterministic rule engine, every refusal explains
*why*, and every attempt (successful or refused) is recorded in an audit history.

## What I built
- **Who are you acting as?** The app opens on a start screen with a card per person (8 staff, 2 managers).
  Pick one to open their workspace; **Switch person** in the sidebar goes back. There is no login.
- **Full listing workflow:** Post, Withdraw, Claim, Unclaim (staff) and Approve, Reject (managers), with
  listing states `OPEN`, `CLAIMED`, `WITHDRAWN`, `APPROVED`. At most one active listing per shift
  (enforced in code and by a partial unique index).
- **Explainable eligibility:** a live ✅/❌ checklist per rule, listing *every* violated rule with the
  conflicting shift ID, required vs. actual rest, and projected weekly hours.
- **Workload preview:** e.g. "24 hours currently -> 32 hours after this shift" (read-only; the claim
  transaction re-checks everything).
- **Smart alternatives:** other open shifts the person can claim right now, each with an explanation.
- **Auditable approval:** managers see owner, proposed owner, shift, certification, current rule results
  and workload impact. Approval re-checks Rules 2-5 and refuses (listing stays `CLAIMED`) if any fail.
- **Action history:** actor, action, shift, outcome, time and details, including every refusal reason.
- **Permissions are enforced in the backend services**, not just by hiding buttons.

## Tech stack
Python 3, Streamlit, SQLite (`sqlite3`), `datetime`/`timedelta`, pytest. No paid services, API keys or AI
models: eligibility is plain deterministic code.

## File structure
```
ShiftSwap/
  app.py              Streamlit UI (only calls services; contains no rules)
  database.py         schema, connections (foreign keys ON, explicit transactions)
  seed_loader.py      idempotent seed loading
  eligibility.py      Rules 1-5 engine (no Streamlit, no wall clock)
  services.py         post/withdraw/claim/unclaim/approve/reject + read services
  access.py           optional access codes (off by default)
  requirements.txt  README.md  AI_USAGE.md  pytest.ini  .gitignore
  data/seed.json      the provided roster
  tests/              conftest.py, helpers.py, test_eligibility.py, test_workflow.py, test_access.py
```

## Install and run
```powershell
cd ShiftSwap
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```
Then open http://localhost:8501. Run the tests with `pytest -q`.

The seed is read from **`data/seed.json`** (a copy of the provided `SHiftSwap_seed.json`). If it is missing,
the app shows the exact expected path and stops; it never substitutes fake data.

## Quick demo (seed data)
1. **Asha (S1)** -> My Roster -> post **SH004** (ICU, Mon 2 Mar 15:00-23:00).
2. **Ben (S2)** -> Browse Shifts -> SH004 is blocked: Rule 2 (no ICU) and Rule 4 (0 h rest after SH001,
   8 h before SH007). Rule 3 correctly passes (SH001 ends exactly when SH004 starts).
3. **Vikram (S8)** posts **SH013** (PEDS); Ben sees Rule 2 and Rule 3 (overlaps SH012).
4. **Tarun (S6)** claims SH004 (24 h -> 32 h that week); **Meera (M1)** approves it in the Approval Queue.
5. Open **Action History** to see every attempt, including the refused claim.

## How to use each action
- **Post:** staff -> *My Roster* -> pick one of your shifts -> *Post listing*.
- **Withdraw:** *My Roster* -> *Withdraw a listing* (your own `OPEN` listings only).
- **Claim:** *Browse Shifts* -> expand a listing (checklist + workload preview) -> *Claim this shift*.
  Ineligible claims are refused with all reasons, recorded, and eligible alternatives are shown.
- **Unclaim:** *My Claims* -> *Unclaim* (listing returns to `OPEN`).
- **Approve / Reject:** manager -> *Approval Queue* -> review -> *Approve* or *Reject*
  (reject returns the listing to `OPEN` and clears the claimer).

## Database and seeding
SQLite file `data/shiftswap.db`, created on first start. The seed is loaded **once**, in a single
transaction, guarded by a `meta` flag. Restarts never reload it, not even if `listings` is empty, so
saved assignments and history persist. To reset everything, stop the app and delete the `.db` file.

## Eligibility rules (all boundaries inclusive)
1. Claimer is not the owner. If violated, **only** Rule 1 is reported.
2. If the shift needs a certification, the claimer must hold that exact one.
3. The shift must not overlap any shift assigned to the claimer (`A.start < B.end and B.start < A.end`).
4. For every assigned, non-overlapping shift: at least 11 h between the earlier end and later start (both directions).
5. The claimer's hours in the Monday 00:00 - next Monday 00:00 week in which the shift **starts**,
   including it, must be at most 40. An overnight shift counts fully in its start week.

Claim order: input validation -> role (staff only) -> listing not `OPEN` returns exactly
`no longer available` (nothing else checked) -> Rule 1 alone -> Rules 2-5, all violations reported.
Approval re-checks Rules 2-5 against the claimer's *current* assignments inside `BEGIN IMMEDIATE`.

## Transactions and audit
Every write runs in `BEGIN IMMEDIATE`. On success the success audit row is written in the *same*
transaction as the change. On refusal or error the transaction is rolled back and the failure row is
written separately, so a success is never logged unless it committed and refusals leave no partial state.

## Tests
`pytest -q` runs 61 core tests (rules and boundaries, workflow, permissions, approval-time revalidation,
rollback, persistence, seed idempotency) on temporary SQLite databases, plus 3 tests for the optional
access codes. The real database is never used by tests.

## Optional access codes (off by default) future implimentation
The spec says there is no login, so anyone can pick any person by default. To require a code to open
a person's workspace, set `SHIFTSWAP_REQUIRE_PIN=1` before `streamlit run app.py`
(PowerShell: `$env:SHIFTSWAP_REQUIRE_PIN="1"`). Random 6-digit codes are created in the git-ignored
`data/access_codes.json`. This only gates the UI; the services enforce roles and ownership regardless.

## Assumptions and limitations
- A shift counts as assigned to its current owner (also while listed) until approval; a pending claim does not count.
- Managers own no shifts and cannot post or claim; staff cannot approve or reject.
- Unknown IDs and malformed values are "input errors", separate from rule violations, and are also logged.
- Timestamps are naive local clinic times. `created_at` uses the machine clock for display only and
  never affects eligibility; all shifts are treated as upcoming.
- Rejecting or unclaiming clears the claimer reference, so the history is the record of who claimed.
- There is no authentication by default (per the spec). SQLite allows one writer at a time, which is
  fine for this scope.
- The UI flow was smoke-tested with Streamlit's headless test runner and manually in the browser;
  there are no automated browser tests and the app is not deployed.