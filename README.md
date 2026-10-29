# flightright-agent

**A severity-aware, context-reading LLM judge that gates every customer-support reply before it is sent.**

The demonstration domain is airline disruption support (delays, cancellations, denied boarding, missed
connections under EU Regulation 261), where a wrong or tone-deaf reply has real consequences: a stranded
parent told the wrong amount, a passenger in distress greeted with cheerful boilerplate, a voucher pushed
instead of the cash refund they are owed.

> Status: in active development. Not legal advice. Everything runs at $0 (free model tiers, local tools).

## Why this judge is different

Most LLM-as-a-judge setups apply one fixed rubric with one fixed bar and take the model's word for it.
This one:

| | Typical LLM judge | This judge |
|---|---|---|
| Strictness | One bar for everything | **Scaled to the case**: lenient for a simple question, a two-judge panel with high bars when a passenger is stranded, vulnerable or in distress |
| Context | Reads the reply alone | **Reads the whole case**: the conversation, history and earlier contacts, special needs, and what the passenger is legally owed |
| Grounding | Free-text opinions | **Every score quotes the reply**, and code verifies the quote exists; invented evidence is retried once, then the case goes to a human |
| Decision | The model says pass/fail | **Code decides** from the scores and the tier's pass bar; the model cannot be talked into a pass |
| Facts | Judged by the model | **Checked deterministically first**: amounts, rights and promises are compared with a rules engine before the judge runs, and a failed check can never be overridden |
| Self-grading | Often the same model family | **Never the drafting model's family**; at high severity, two judges from different families must agree |
| Trust | Assumed | **Measured**: planted-error catch rates, controlled pairs, consistency, bias and fairness probes, agreement with human preferences |

Eight dimensions are graded: relevance, context awareness, urgency, tone, grievance handling, correctness,
actionability and fairness/safety. Details: [docs/judge_design.md](docs/judge_design.md).

## How a reply gets through

```
passenger message
  → guardrails (injection, abuse, PII)            → triage: severity S1–S4 (rules + LLM, higher tier wins)
  → entitlement engine (what EU261 says is owed)  → support agent drafts a reply
  → code checks (amounts, rights, vouchers, care, timelines)
  → JUDGE (profile chosen by severity; panel at S3)
        approve → send     revise (≤ 2, with the judge's guidance) → redraft     else → human handoff
```

Severe cases (S4: health, safety, a child alone) always go to a human; the short holding message and the
handoff summary are judged too.

## How the judge is evaluated

Methods are in place; the full results are produced by the evaluation step and reported here when final.

| Question | Method |
|---|---|
| Does it catch known errors? | 13 planted-error types (wrong amount, invented "extraordinary circumstances", voucher push, buried urgency, cheerful tone in distress, blame-shifting, ignoring a repeat contact, …), catch rate per type |
| Is it sensitive to single defects? | Controlled pairs: the same reply with and without one planted error |
| Is it stable? | The same reply judged repeatedly with caching off; flip rate per dimension and per decision ([docs/judge_consistency.md](docs/judge_consistency.md)) |
| Is it biased? | Position, verbosity and politeness probes; counterfactual passengers who differ only in name, English level, tone or loyalty |
| Does it agree with people? | Public human-preference datasets, order-swapped |
| Is severity-awareness worth it? | Headline experiment: severity-aware judge vs. a single fixed-strictness judge, on bad replies sent in severe cases, good replies blocked in simple ones, and cost |

Supporting components already measured: the triage that sets the judge's strictness catches 99 % of
severe (S3/S4) cases on the development split ([docs/triage_eval.md](docs/triage_eval.md)); the entitlement
engine is checked against 37 hand-worked legal cases with 100 % branch coverage.

## Inspecting the judge (in progress)

Every judgement is already stored as a structured record (scores, quoted evidence, decision and reason,
panel votes, models, rubric version). Two views on top of it are being built:

- **Judge Monitor**: a live feed of every verdict across conversations, rolling pass rates per tier and
  dimension, consistency and catch-rate signals, alerts.
- **Judge Workbench**: why a decision was made (scores with the quoted evidence highlighted), one-click
  corrections, and corrections fed back to the judge as calibration examples. Not RLHF: the model is
  unchanged; what adapts is versioned context around it.

## Architecture

```
               ┌──────────── n8n (self-hosted) ─────────────────────┐
 Channels ───► │ inbound → API → reply · handoff + SLA · digest      │
               └───────────────┬─────────────────────▲───────────────┘
                               │ REST                │ signed events
                               ▼                     │
        ┌──────────────── FastAPI service ───────────┴──────────────┐
        │ customer API · ops API · SSE stream · persistence           │
        └───────────────┬───────────────────────────────────────────┘
                        ▼
        ┌──────────── LangGraph conversation graph ─────────────────┐
        │ guardrails → context → triage → entitlement engine         │
        │ → support agent → code checks → judge → router             │
        │            ↑ revise (≤2)           ├─ send                 │
        │            └───────────────────────├─ handoff → summary    │
        └────────────────────────────────────────────────────────────┘
```

n8n never decides anything. It moves messages, verifies the API's HMAC-signed events, runs SLA
timers (an unacknowledged S3 ticket is escalated after 15 minutes), and collects the human
acknowledge/resolve step through a form. Setup, the five workflows and their acceptance test:
[n8n/README.md](n8n/README.md).

The generic judging machinery (scored rubrics, evidence enforcement, panels, planted errors, controlled
pairs, calibration, bias probes) is a separate, domain-agnostic library:
[JudgeKit](https://github.com/SaiCharan85/JudgeKit). Full specification: [docs/SPEC.md](docs/SPEC.md).

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest                                              # no API keys needed (all LLMs mocked)
uv run python scripts/demo_conversation.py SC-0020         # one live scenario through the full pipeline
uv run python -m flightright.api.server                    # API on :8000 (docs at /docs)
docker compose up -d n8n && uv run python scripts/n8n_import.py   # n8n on :5678
```

## License

MIT
