# flightright-agent

Support for passengers hit by flight delays, cancellations, denied boarding and missed connections, where
**every reply is checked before it is sent**:

1. a deterministic **entitlement engine** works out what EU Regulation 261 says the passenger is owed
   (both the 2004 regime and the amended regime from 2027-10-23);
2. a **support agent** drafts the reply with that entitlement and operational tools;
3. **code checks** catch wrong amounts, missing care obligations and voucher pushes;
4. a **severity-aware LLM judge** scores relevance, context, urgency, tone, grievance handling,
   correctness, actionability and fairness, stricter when the passenger is in more trouble;
5. **n8n** runs the operations loop (channels, handoff tickets, SLA escalation, digests, alerts);
6. a **Judge Workbench** shows why the judge decided what it did and lets a human correct it.

Everything runs at $0: free model tiers, local models, self-hosted n8n.

> Status: early development. Not legal advice.

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
        │ guardrails → triage → context → entitlement engine         │
        │ → support agent → code checks → judge → router             │
        │            ↑ revise (≤2)           ├─ send                 │
        │            └───────────────────────├─ handoff → summary    │
        └────────────────────────────────────────────────────────────┘
```

Full specification: [docs/SPEC.md](docs/SPEC.md).

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
```

## License

MIT
