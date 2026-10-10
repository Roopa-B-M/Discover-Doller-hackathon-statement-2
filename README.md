# ShiftSwap Intelligence - Explainable, Rule-Aware Staff Scheduling

CityCare Clinic staff post shifts they cannot work, colleagues claim them, and managers approve
or reject the exchange. Every action is validated by a deterministic rule engine, every refusal
explains *why*, and every attempt (successful or refused) lands in an audit history.

## Core features
- Person picker (no login): staff see staff tools, managers see manager tools; **all permissions are
  also enforced in the backend services**.
- Full listing workflow: Post, Withdraw, Claim, Unclaim, Approve, Reject (states `OPEN`, `CLAIMED`,
  `WITHDRAWN`, `APPROVED`), at most one active listing per shift (also enforced by a partial unique index).
- **Explainable eligibility**: live per-rule checklist with every violated rule, conflicting shift IDs,
  required vs. actual rest and projected hours.
- **Workload impact preview**: "24 hours currently -> 32 hours after this shift" (read-only; the claim
  transaction re-checks everything).
- **Smart alternatives**: other open listings you can claim right now, with an explanation.
- **Auditable approval**: managers see owner, proposed owner, certification, current rule results and
  workload impact; approval re-validates Rules 2-5 and refuses (listing stays `CLAIMED`) if they fail.
- **Transparent history**: actor, action, shift, outcome, time and details (including all refusal reasons).

## Stack
Python 3, Streamlit, SQLite (`sqlite3`), `datetime`/`timedelta`, pytest. No external services or API keys.

## File structure
```
ShiftSwap/
  app.py              Streamlit UI (only calls services)
  database.py         schema, connections (foreign keys ON, explicit transactions)
  seed_loader.py      idempotent seed loading
  eligibility.py      Rules 1-5 engine (pure backend; no Streamlit, no wall clock)
  services.py         post/withdraw/claim/unclaim/approve/reject + read services
  requirements.txt  README.md  AI_USAGE.md  pytest.ini  .gitignore
  data/seed.json      the provided roster (copy of SHiftSwap_seed.json)
  tests/              conftest.py, helpers.py, test_eligibility.py, test_workflow.py
```
The seed is read from **`data/seed.json`**. The provided `SHiftSwap_seed.json` is already copied there;
if you replace it, the file must be at exactly `<project>/data/seed.json`. If it is missing the app shows
the exact expected path and stops - it never substitutes fake data. (Override with env vars
`SHIFTSWAP_SEED=/path/to/seed.json` and `SHIFTSWAP_DB=/path/to/db`.)

## Install and run
```bash
cd ShiftSwap
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```
Run the tests: `pytest -q`

## Using the app
1. Pick a person in the sidebar ("Acting as"). Staff and managers get different navigation.
2. **Post**: Staff -> *My Roster* -> choose one of your shifts -> *Post listing*.
3. **Withdraw**: *My Roster* -> *Withdraw a listing* (only your own `OPEN` listings).
4. **Claim**: *Browse Shifts* -> expand a listing to see the checklist and workload preview -> *Claim this
   shift*. Ineligible claims are refused by the backend, listed with every reason, and recorded; the UI
   then offers eligible alternatives.
5. **Unclaim**: *My Claims* -> *Unclaim* (returns the listing to `OPEN`).
6. **Approve / Reject**: Manager -> *Approval Queue* -> review details -> *Approve* or *Reject*
   (reject returns the listing to `OPEN` and clears the claimer).
7. *Action History* shows every attempt, newest first (ordered by autoincrement id, not by timestamp).

## Database and seeding
SQLite file: `data/shiftswap.db` (created on first start). The seed is loaded **once**, inside one
transaction, guarded by a `meta` flag (`seed_initialized`). Restarts never reload it - not even when
`listings` is empty - so saved assignments and history persist. To reset, delete the `.db` file.
Listings and history start empty.

## Eligibility rules (all boundaries inclusive)
1. Claimer is not the owner. If violated, **only** Rule 1 is reported.
2. If the shift needs a certification, the claimer must hold that exact one.
3. The shift must not overlap any shift assigned to the claimer (`A.start < B.end and B.start < A.end`).
4. For every assigned, non-overlapping shift, >= 11 h between the earlier end and later start (both directions).
5. Claimer's hours in the Monday 00:00-Monday 00:00 week in which the shift **starts**, including it, <= 40.
   An overnight shift counts fully in its start week.

Claim order: input validation -> role (STAFF only) -> listing not `OPEN` returns exactly
`no longer available` (nothing else checked) -> Rule 1 alone -> Rules 2-5 all reported.
Approval re-checks Rules 2-5 against the claimer's *current* assignments inside `BEGIN IMMEDIATE`.

## Transactions and audit
Every write runs in `BEGIN IMMEDIATE`. On success the success audit row is written in the *same*
transaction as the change; on refusal/error the business transaction is rolled back and the failure row
is written in a separate short transaction, so a success is never logged unless it committed and refusals
never leave partial changes.

## Assumptions and limitations
- A shift counts as assigned to its current owner (also while listed) until approval; pending claims do not count.
- A person cannot post/claim shifts while holding the other role; managers own no shifts.
- Unknown IDs / malformed values are "input errors" distinct from rule violations (and are also logged).
- Timestamps are naive local times; `created_at` uses the machine clock for display only and never
  influences eligibility.
- When a listing is rejected or unclaimed the claimer reference is cleared, so history is the record of who claimed.
- No authentication (by design); the sidebar picker is trusted. SQLite is single-writer; fine for this scope.
- Streamlit's headless `AppTest` was used to smoke-test the UI flow; there are no browser-level (Selenium) tests.
