# Conversation graph

`src/flightright/agent/` runs one LangGraph thread per conversation, checkpointed in SQLite
(`thread_id = conversation_id`). Code decides every route; the LLM only writes text.

```
intake ─► guardrails ─┬─ human asked / repeated abuse / already handed off ─► handoff
                      ├─ off-topic ─► polite redirect
                      └─► context ─┬─ no booking yet ─► ask for the booking reference
                                   └─► triage ─► entitlement ─┬─ S4 / unverified rule ─► handoff
                                                               └─► care actions ─► draft ─► code checks
                                                                   ─► judge ─┬─ approve ─► finish
                                                                             ├─ revise (≤ 2) ─► draft
                                                                             └─ exhausted ─► handoff
```

| Node | What it does |
|---|---|
| intake | Duplicate delivery (same idempotency key) returns the stored outcome; nothing re-runs (FR-1.4) |
| guardrails | Injection (logged, facts unchanged), abuse (de-escalate, then offer a human), human request, off-topic, PII masking before anything is stored (FR-2) |
| context | Booking, flight facts, passenger history (earlier contacts), playbook |
| triage | Rules + LLM, higher tier wins (`docs/triage.md`) |
| entitlement | The rules engine; rebooking options when rerouting applies |
| care actions | Code issues care vouchers while the passenger waits, and a refund when they ask for one; idempotent per conversation; above authority limits → handoff (FR-5.3) |
| draft | The support agent writes from the facts block only (FR-6) |
| code checks | Amounts, compensation promises, reductions, regime, vouchers without the cash right, missing care, unbacked timelines (FR-7) |
| judge | Pluggable; the severity-aware judge plugs in here (FR-8). A code-check failure always means revise or handoff, whatever the judge says |
| handoff | One ticket per conversation, summary (LLM, with a code fallback), holding message; holding message and summary are judged too (FR-9) |
| finish | Stores the reply, the outcome per idempotency key and the contact in the history store |

Context is loaded before triage because triage uses it (earlier contacts, assistance on the booking,
how far away the flight is).

## Live check

`scripts/demo_conversation.py <dev scenario id>` plays a scenario through the whole graph with free
models. The live runs during development caught three issues the mocked tests could not: replies with no
acknowledgement of the passenger's situation, unbacked response-time promises ("within the next hour"),
and an invented reduction caveat that a playbook line had encouraged. All three now have prompt fixes
and, where deterministic, code checks.
