# AI_USAGE.md

## Tools used
- **ChatGPT** - produced the first long specification prompt for the project.
- **Claude (Anthropic, claude.ai)** - reviewed that prompt against the problem PDF and the seed JSON,
  then generated the implementation (backend, Streamlit UI, tests, documentation).

## Where I used AI
- Reviewing the specification prompt and checking its scenario numbers against the real seed data.
- Generating the SQLite schema, idempotent seed loader, eligibility engine (Rules 1-5), the service layer
  (transactional approval, audit logging), the Streamlit UI and the pytest suite.
- Follow-up changes I asked for: a "who are you acting as" start screen, and an optional access-code feature.

## What I accepted
- The overall structure: `database.py`, `seed_loader.py`, `eligibility.py`, `services.py`, `app.py`.
- A deterministic rule engine that returns structured violations and a per-rule checklist.
- Writing the success audit row in the same transaction as the change, and logging refusals after rollback.
- The test suite for rules, boundaries, workflow, persistence and revalidation at approval.

## What I changed or decided myself
- Asked for the start screen where I first choose who I am acting as, then see that person's details.
- Considered requiring an access code for manager accounts. Because the problem statement says
  "There is no login", I decided to keep it **off by default** (it exists only as an optional setting).
- Found that the AI-written prompt had a wrong number: with the real data Tarun has 24 hours in the week
  of 2 March, so Scenario A shows 24 -> 32 hours, not 32 -> 40.

## What I rejected
- I did not reject any generated module outright. 

## How I verified the result
- Ran `pytest -q` on my own machine: **61 passed** (the optional access tests add 3 more when included).
- Ran the app locally and checked the dashboard against the JSON: 8 staff, 2 managers, 61 shifts.
- Walked through the scenarios in the browser: posting SH004, Ben's blocked claim (Rule 2 and Rule 4
  reasons), the SH013 case (Rules 2 and 3), a valid claim and a manager approval.
- Not done: automated browser tests and deployment.