# Architecture

How the pieces fit together, and which part owns which decision. The full requirements are in
[SPEC.md](SPEC.md). The judge's design is in [judge_design.md](judge_design.md) and the agent's in
[agent.md](agent.md).

## Who decides what

| Decision | Owner | Why |
|---|---|---|
| What the passenger is owed (compensation, care, refund/rerouting, regime) | **Rules engine** (`rules/`, YAML rulesets) | Deterministic, tested against 37 hand-worked cases; an LLM never states a number the engine did not produce |
| How severe the case is (S1–S4) | **Triage**: rules and an LLM, the higher tier wins (`triage/`) | Missing a severe case is worse than over-triaging, so neither source can lower the other |
| What the reply says | **Support agent** LLM (`agent/drafting.py`) | Language, empathy and structure are model work |
| Whether the reply's facts are right | **Code checks** (`agent/checks.py`) | Amounts, rights, voucher consent, care, timelines and promises are compared with the engine's output. A failed check cannot be overridden |
| Whether the reply is good enough to send | **Judge** scores, then **code decides** (`judge/judge.py`, SPEC §6.3) | The model grades eight dimensions with quoted evidence; the tier's bars and the S3 panel rule turn the scores into approve / revise / handoff |
| Side effects (vouchers, rebooking, tickets) | **Graph code** (`agent/graph.py`) | Idempotent per conversation; the LLM only describes what code already did |
| Routing messages, SLA timers, notifications | **n8n** (`n8n/workflows/`) | No reasoning: Code nodes only map, format and verify signatures |
| Corrections to the judge | **A reviewer** in the Workbench | Few-shot exemplars and proposed bar changes; a bar changes only on explicit approval |

## Runtime view

```
              passenger / n8n W1                      ops console (ui/, React)
                     │ POST /conversations/{id}/messages        │ GET /ops/*, SSE streams
                     ▼                                           ▼
┌────────────────────────────── FastAPI (api/) ─────────────────────────────────────┐
│ customer API · ops API · Workbench · metrics · SSE broker · HMAC event publisher  │
│ OpsStore (SQLite): conversations, attempts, judgements, handoffs, events, ...      │
└──────────────┬───────────────────────────────────────────────▲────────────────────┘
               │ ConversationService.handle_message               │ events per node
               ▼                                                  │
┌──────────────────────── LangGraph (agent/graph.py) ──────────────────────────────┐
│ guardrails → context loader → triage → entitlement → care actions → draft        │
│   → code checks → judge → route: approve → send │ revise (≤2) → draft │ handoff   │
│ checkpointer: SQLite (one thread per conversation; replay-safe idempotency keys)  │
└──────┬──────────────┬────────────────┬──────────────────┬────────────────────────┘
       │              │                │                  │
  rules engine   LLM client       mock airline       history store
  (rulesets)     (llm/: free-tier  (services/:        (repeat contacts,
                  chains, ledger,   bookings, vouchers  resolutions)
                  exact cache)      rebooking)
```

- **Events.** Every graph node appends an event (`triage.completed`, `draft.created`,
  `checks.completed`, `judgement.completed`, `handoff.created`, `reply.sent`, ...). The API stores
  them, fans them out over SSE to the console, and publishes `handoff.created`,
  `guardrail.injection_detected` and `metrics.threshold_breached` to n8n, signed with HMAC-SHA256.
- **Judge records.** Each judgement stores everything needed to inspect it later:
  - the inputs it saw;
  - scores with evidence quotes and the code-computed decision with its reason;
  - panel votes and member scores;
  - models used, rubric ID, prompt version, exemplar count and latency.

  The Monitor, Workbench and evaluation all read these records.
- **Free tiers only.**
  - Each role has a fallback chain of free models.
  - A per-model ledger enforces the daily and per-minute limits below each provider's free quota. A
    call books its estimate before it starts, so parallel callers cannot overshoot.
  - When a chain is exhausted, the conversation goes to a human rather than sending an unjudged
    reply.

## Code map

| Package | Contents |
|---|---|
| `rules/` | Entitlement engine, rulesets for the 2004 and amended 2027 regimes, territories, great-circle distances |
| `triage/` | Rule-based and LLM triage and the merge |
| `agent/` | Graph, drafting prompts, code checks, guardrails, handoff summaries, service entry point, live-scenario helpers |
| `judge/` | Severity-aware judge on JudgeKit's scored rubrics; planted-error mutators |
| `llm/` | Provider adapters (Groq, Google AI Studio, Ollama), fallback client, usage ledger, exact-match cache |
| `services/` | Mock airline, passenger history, playbooks |
| `api/` | FastAPI app, runtime wiring, SQLite ops store, uvicorn entry point |
| `simulator/` | CrewAI passenger agents and the handoff oracle |
| `eval/` | Evaluation items, metrics, public human-judgment checks, resumable runner |
| `n8n.py` | Secret scan and lint for exported workflows |
| `ui/` | React console: chat and pipeline, Monitor, explorer, Workbench, handoffs, metrics |

## Data flow for one S3 message (trace)

This trace follows one case: a passenger travelling with an infant, cancelled at Brussels, who has
already contacted support twice.

1. **Guardrails:** no injection or abuse.
2. **Triage:** the rules see an infant and a repeat contact, so the tier is S3, urgency soon.
3. **Entitlement:** regime `eu261_2004`. Brussels → Hamburg is 488 km, so **€250 compensation**, plus
   meals and refreshments while waiting, and a choice of refund or rerouting.
4. **Care actions:** the code issues a €20 meal voucher and a €10 refreshments voucher, idempotently.
5. **Draft:** gpt-oss-120b writes the reply.
6. **Code checks:** every amount and right in the draft is checked against the engine. All pass.
7. **Judge:** S3 uses a panel, Gemma and Qwen (two families, neither the agent's). Both score every
   dimension at or above 4 and pass the three must-pass checks, so the decision is **approve**. The
   scorecard shows the quotes behind each score.
8. **Reply sent.** The console's pipeline view lit up each step as it happened.
