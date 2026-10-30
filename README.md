# flightright-agent

**A severity-aware, context-reading LLM judge that gates every customer-support reply before it is sent.**

The demonstration domain is airline disruption support (delays, cancellations, denied boarding, missed
connections under EU Regulation 261), where a wrong or tone-deaf reply has real consequences: a stranded
parent told the wrong amount, a passenger in distress greeted with cheerful boilerplate, a voucher pushed
instead of the cash refund they are owed.

> Not legal advice. Everything runs at $0 (free model tiers, local tools).

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

## How the judge is evaluated, and what came out

The protocol is pre-registered: [docs/eval_protocol.md](docs/eval_protocol.md). It was frozen, with its
SHA-256 recorded, before the first model call of the final run. The final run used the held-out test
split once: 40 replies drafted by the real pipeline, plus 80 copies of them with one planted error each.

Full report: [eval/report.md](eval/report.md).

**Headline: severity-aware judge vs a fixed-strictness judge** (same model and rubric; the baseline uses
the S2 bar everywhere, has no panel, and sees no triage context):

| | Severity-aware | Fixed baseline |
|---|---|---|
| Bad replies approved in severe (S3) cases | **16 %** (5/32) | 31 % (10/32) |
| Planted errors caught (all tiers) | **91 %** (73/80) | 74 % (59/80) |
| Good simple (S1) replies not approved | 58 % (7/12) | **25 %** (3/12) |
| Model requests per item | 0.9 | 1.0 |

- **Outcome under the pre-registered rule: a trade-off.** Severe leaks fell by 15 points, but
  over-blocking of simple cases rose by more than the 5 points allowed.
- **Significance:** the paired McNemar test on the S3 items is not significant (p = 0.125, n = 32).
- **Cause of the over-blocking (exploratory analysis).** 6 of the 7 blocked S1 replies were not
  rejected by the judge. They had *no trustworthy verdict*:
  - The judge reads the triage context. When urgency is "none", the must-pass urgency check passes
    trivially, but the rubric still demands a verbatim quote, and the draft has nothing to quote.
  - The quote fails verification, so the reply goes to a human.
  - That is a fixable rubric rule, not a judgement error. It is the first change for the next version.

**Other results** (held-out test split):

| Question | Result |
|---|---|
| Does code catch fact errors first? | Wrong amounts and voucher pushes: 6/6 each caught by code checks before any model call. 21 of 80 planted errors stopped there |
| Can the judge catch fact errors alone? | 7/7 when told the code checks passed (small n) |
| Is it stable? | The same reply judged 3 times flips decision 35 % of the time; every flip involves a handoff, mostly from S3 panel disagreement ([docs/judge_consistency.md](docs/judge_consistency.md)) |
| Is it biased? | Politeness padding hid an invented "extraordinary circumstances" or wrong-regime claim in 31 % of cases; verbosity padding shifted mean scores by between -0.3 and +0.4; the two panel judges agree on pass/fail 68 % of the time |
| Is it fair? | Entitlements identical across all 17 counterfactual groups (name, English, tone, loyalty). Reply quality differed by up to 1.2 points in one group |
| Does it agree with people? | HelpSteer3 human preferences: 57 % (both orders); position consistency 77 %. HelpSteer2 ratings: Spearman 0.31-0.62. General-domain sanity checks only |
| Multi-turn simulation (CrewAI passengers, 6 test cases) | All 6 resolved: 2 by the agent, 4 by a human after handoff; 1.8 turns on average |
| Triage that sets the strictness | Recall on severe cases 99 % (104/105), under-triage 2 %, over-triage 12 % |

Limitations:
- Ground truth is planted errors plus presumed-good drafts. There are no large-scale human labels
  for airline replies.
- Sample sizes are set by free-tier quotas.
- Some verdicts were lost to rate limits and provider errors. Those items were judged once more
  under a documented protocol change, and the report shows both versions; the conclusions do not
  change.

The entitlement engine is checked against 37 hand-worked legal cases with 100 % branch coverage.

## Inspecting the judge: the ops console

Every judgement is stored as a structured record: scores, quoted evidence, the code-computed decision
and its reason, panel votes, models, and rubric version. The console (`ui/`, React) is built on those
records and reads only from the API:

- **Chat + live pipeline:** talk to the agent as a passenger from a demo case. Each step lights up as it
  happens (triage, rules engine, draft, code checks, judge, revision loop, reply or handoff), and the
  judge's scorecard appears underneath.
- **Scorecard and diff:** per-dimension scores against the tier's bar, with each evidence quote
  highlighted inside the draft. Then the guidance the agent received, and a word-level diff of the
  failed draft against its revision.
- **Judge Monitor:** a live feed of every verdict across conversations, plus rolling metrics:
  - decisions per tier;
  - pass rate per dimension;
  - S3 panel disagreement and judge latency;
  - free-tier quota per model;
  - quality signals from the latest evaluation, and alert history.
- **Judge Workbench:**
  - one-click corrections ("the judge was right", or override a score, verdict or decision);
  - "for messages like this, a good reply looks like this" examples;
  - the exemplar set the judge actually sees, and agreement per rubric version;
  - proposed bar changes that apply only on approval.

  This is not RLHF: the model is unchanged, and what adapts is versioned context around it. Nothing
  from the held-out test split is accepted.
- **Handoff queue:** SLA countdowns, acknowledge and resolve, and the notifications n8n logged.

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
cd ui && npm ci && npm run dev                              # console on :5173 (enter the ops key in Settings)
```

## License

MIT
