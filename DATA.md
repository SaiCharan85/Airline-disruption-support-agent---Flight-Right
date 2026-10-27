# Data sources

No data files are committed to this repository. Everything below is downloaded or generated locally into
the git-ignored `data/` folder. Every source must be free to use; licenses are checked before adoption.
The latest available version of each source is used; the "Checked" column records when it was last verified.

| # | Source | Real? | Used for | License / terms | Status | Checked |
|---|---|---|---|---|---|---|
| 1 | Regulation (EC) No 261/2004; Regulation (EU) 2026/2202 (amendment, OJ L 2.10.2026); Commission Interpretative Guidelines 2016; CJEU judgments | real (legal text) | Rulesets and `docs/regulation_notes.md`, written in our own words with article references | EU legal acts and CJEU case law: reuse permitted with source acknowledgement (EUR-Lex / Curia legal notices) | In use (sources listed in `docs/regulation_notes.md` §12) | 2026-10-25 |
| 2 | OurAirports `airports.csv` (coordinates, country) | real | Great-circle distances and EU territory (FR-4.4) | Public domain (OurAirports) | In use: latest release via `scripts/download_airports.py` (sha256 + date in `data/ourairports/manifest.json`); a 26-airport subset is committed as `tests/fixtures/airports.csv` for CI | 2026-10-25 |
| 3 | Synthetic scenarios and personas (500, incl. 35 counterfactual groups) | simulated | Evaluation and simulation (§10.1); design in `docs/scenarios.md` | Ours (MIT). Facts and gold labels from code; opening messages written by `gemini-3.5-flash-lite` (Google AI Studio free tier). Not committed (`data/scenarios/`) | Generated: 500 scenarios (dev 256 / test 244), via `scripts/generate_scenarios.py`; messages regenerated once with a stricter "no invented stakes" rule | 2026-10-25 |
| 4 | Public human-judgment sets (candidates: MT-Bench human judgments, HelpSteer2) | real human ratings | Judge agreement and bias sanity checks (§10.1) | To be verified per dataset before download | Candidate (Step 11) | — |

## Notes
- Simulated passengers, bookings and conversations are fictional and must never be presented as real.
- Nothing here is legal advice.
