# Synthetic scenarios

Evaluation needs passengers whose correct answer is known. Each scenario is **designed in code**
(`src/flightright/scenarios/design.py`) and only its wording comes from an LLM
(`src/flightright/scenarios/writer.py`), so ground truth never depends on a model.

## What a scenario contains

| Part | Source |
|---|---|
| Disruption case (route, airline, delays, notice, cause, passenger needs) | 21 case templates, one per engine branch, sampled with a fixed seed |
| Regime | Both regimes for every template (departures before / after 23 Oct 2027) |
| Severity tier S1–S4 and situation (timing, urgency, deadline, overnight, repeat contact, S4 trigger) | Tier sampled first (20 / 40 / 30 / 10 %), situation built to match `config/triage.yaml` |
| Persona (name, temperament, English level, honesty, loyalty, injection attempt) | Sampled per tier; ~5 % try prompt injection |
| Gold entitlement | The entitlement engine (itself checked against 37 hand-worked golden cases) |
| Gold key actions (e.g. `offer_care_now`, `state_compensation`, `handoff`, `resist_injection`) | Derived from the entitlement and situation |
| Follow-up intents for multi-turn simulation | Sampled per tier (used by the simulator) |
| Opening message | Free-tier LLM, five scenarios per request, from plain-language facts |

## Coverage of the full set (seed 42)

500 scenarios: every template × regime cell; tiers S1 69 · S2 234 · S3 174 · S4 23; dev 256 / test 244.
35 **counterfactual groups**: a base scenario plus four variants that change exactly one persona
attribute (name, English level, tone, loyalty). The case and gold labels are identical inside a group,
so any difference in the reply or the judge's score is a fairness signal. Groups stay inside one split.

## Checks on generated messages

Length 15–220 words, no personal data (ids, e-mail, phone), injection attempts visible when the persona
has one, honest personas don't quote amounts. Failed messages are not stored; the run is resumable and
re-requests only what is missing. Prompts and answers are cached, so re-runs cost nothing.

## Known limits

- Names and loyalty tier often don't appear in the message itself; the agent sees them through the
  booking record, which is where the fairness comparison applies.
- Messages are single first contacts; follow-up turns are produced by the simulator.
- Planted-error replies (judge evaluation) are built in the judge step, not here.

## Commands

```
uv run python scripts/generate_scenarios.py --dry-run    # plan, request/token estimate, example facts
uv run python scripts/generate_scenarios.py --pilot 50   # first 50 + data/scenarios/pilot_review.md
uv run python scripts/generate_scenarios.py              # full set (resumes)
```
