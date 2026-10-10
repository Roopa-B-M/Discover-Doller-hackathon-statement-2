# AI_USAGE.md

## Tools used
- **ChatGPT** - generated the first long specification prompt for the project.
- **Claude (Anthropic, claude.ai)** - reviewed that prompt against the problem PDF and seed JSON,
  then generated the complete implementation (backend, Streamlit UI, tests, README).

## Where I used AI
- Reviewing the specification prompt and the seed data (checking the scenario numbers against the real data).
- Generating the SQLite schema, idempotent seed loader, eligibility engine (Rules 1-5), service layer
  with transactional approval and audit logging, the Streamlit UI and the pytest suite.
- Follow-up changes: a "who are you acting as" landing page, and an optional access-code feature.

## What I accepted
- The overall architecture: `database.py`, `seed_loader.py`, `eligibility.py`, `services.py`, `app.py`.
- Deterministic rule engine with structured violations and per-rule checklist.
- Success audit rows written in the same transaction as the change; failure rows written after rollback.
- The test suite (rules, boundaries, workflow, persistence, revalidation at approval).

## What I changed or asked for
- Asked for a landing page where I first choose who I am acting as, then see that person's workspace.
- Asked about requiring an ID/code to enter manager accounts. Because the problem statement says
  "There is no login", this is implemented as an optional setting that is OFF by default.
- Corrected a number from the AI-written prompt: with the real seed, Tarun has 24 hours in the week
  of 2 March (24 -> 32 after SH004), not 32 -> 40 as the prompt claimed.

## What I rejected
- Nothing in the generated code was discarded outright. 

## How I verified it
- `pytest -q` on my own machine: **61 passed** (the access-code tests add 3 more if that file is included).
- Ran the app locally and checked the dashboard counts (8 staff, 2 managers, 61 shifts) against the JSON.
- Manually walked through the demo scenarios in the browser: posting SH004, Ben's blocked claim
  (Rule 2 and Rule 4 reasons), the SH013 overlap case (Rules 2 and 3), a valid claim and approval.
- Not done: automated browser tests and deployment.