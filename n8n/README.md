# n8n workflows

n8n (self-hosted community edition, free) handles the operational loop around the agent: channel
messages in and out, handoff tickets with SLA timers, the daily digest, alerts, and the human
acknowledge/resolve step. **It makes no decisions.** Triage, drafting, judging and entitlements all
happen in the API; Code nodes only map fields, format text and verify signatures.

```
passenger ──► W1 /webhook/inbound ──► API /conversations/{id}/messages ──► reply
                                         │ signed events (HMAC-SHA256)
                                         ▼
              W2 /webhook/flightright-events
                ├─ handoff.created ─► ticket notice ─► wait SLA ─► still open? ─► escalate + notify
                └─ threshold / injection alerts ─► de-duplicate (1 h) ─► alert notice
              W3 daily 08:00 (or /webhook/digest-now) ─► metrics + handoffs ─► digest notice
              W4 every 15 min ─► API /ops/alerts/check (breaches come back through W2)
              W5 /form/handoff-action (people) or /webhook/handoff-action-api (scripts)
                 ─► acknowledge / resolve ─► confirmation
```

Notices (ticket, escalation, alert, digest) are written to the API's notification log
(`GET /ops/notifications`), which the ops console shows. Pointing them at Slack or email instead
means swapping that one HTTP node per workflow for n8n's Slack or Send Email node.

| File | Workflow | Trigger |
|---|---|---|
| `workflows/w1_inbound.json` | W1 Inbound message | `POST /webhook/inbound` `{user_id, text, message_id, booking_ref?}` |
| `workflows/w2_events_handoff_sla.json` | W2 Signed events: handoffs, SLA and alerts | `POST /webhook/flightright-events` (from the API) |
| `workflows/w3_daily_digest.json` | W3 Daily ops digest | cron `0 8 * * *`, or `POST /webhook/digest-now` |
| `workflows/w4_alert_poll.json` | W4 Threshold alert poll | every 15 minutes |
| `workflows/w5_handoff_actions.json` | W5 Handoff acknowledge and resolve | form `/form/handoff-action`, or `POST /webhook/handoff-action-api` |

The API publishes all events to one URL, so the W4 alert events arrive through the W2 webhook,
which verifies them once and routes them by type.

## Run it

```bash
# .env needs OPS_API_KEY, OPS_ADMIN_KEY, N8N_HMAC_SECRET and
# N8N_EVENTS_WEBHOOK_URL=http://localhost:5678/webhook/flightright-events
uv run python -m flightright.api.server        # API on the host, port 8000
docker compose up -d n8n                       # n8n on http://localhost:5678
uv run python scripts/n8n_import.py            # import, publish, restart
```

The first visit to http://localhost:5678 asks you to create the local owner account. The
workflows are already imported and running by then.

To run the API in a container too, use `docker compose --profile api up -d`. This needs the
`judgekit` dependency to come from its git tag rather than a local path.

## Settings (environment, from `.env` via docker compose)

| Variable | Used by | Default |
|---|---|---|
| `FLIGHTRIGHT_API_URL` | all HTTP nodes | `http://host.docker.internal:8000` |
| `OPS_API_KEY` | `X-API-Key` on `/ops/*` calls | required |
| `N8N_HMAC_SECRET` | W2 signature check (same value as the API's) | required |
| `FLIGHTRIGHT_CONSOLE_URL` | links in ticket notices | `http://localhost:5173` |
| `SLA_SECONDS_OVERRIDE` | W2 wait (test stack only) | unset: the tier's SLA from `config/app_config.yaml` |
| `GENERIC_TIMEZONE` | W3 schedule | `Europe/Berlin` |

Nothing secret is stored in the workflow JSON. Nodes read keys through `$env`, which needs
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. `NODE_FUNCTION_ALLOW_BUILTIN=crypto` allows the HMAC check.
Both are set in `docker-compose.yml`.

## Signed events

The API signs `timestamp + raw body` with HMAC-SHA256 and sends the result as
`X-Signature: sha256=<hex>`, together with `X-Timestamp` and `X-Event-Id`. W2 receives the raw
body and recomputes the signature. It rejects:
- timestamps more than 5 minutes off;
- bad signatures;
- repeated `event_id`s.

Each rejected event is dropped and logged as a `signature.rejected` notice.

## Editing workflows

You can edit in the n8n editor, then export:

```bash
uv run python scripts/n8n_export.py
```

The export:
- keeps the file name for each workflow ID;
- strips runtime-only fields (`staticData` holds W1's user-to-conversation map and the seen event
  IDs, so it must never be committed);
- refuses to write a workflow that has attached credentials, literal keys, or any secret value
  from `.env`.

`tests/test_n8n.py` runs the same checks in CI, along with structure checks: connections, unique
webhook paths, and every API call reading its URL and key from `$env`.

## Acceptance test

```bash
SLA_SECONDS_OVERRIDE=10 docker compose up -d n8n
uv run python scripts/n8n_import.py
uv run python scripts/test_n8n_flows.py
```

The test checks that:
1. W1 replies exactly once per `message_id`;
2. a handoff produces a verified event and a ticket notice;
3. an unacknowledged ticket is escalated after the (shortened) SLA, while an acknowledged one is
   not;
4. W5 validation errors come back, and a W5 resolve stores the outcome in the API;
5. forged events are dropped;
6. W3 writes a digest;
7. the exported JSON holds no secrets.

It makes a few free-tier LLM calls (triage and handoff summaries).

Version: n8n 2.46.1, pinned in `docker-compose.yml` (checked 2026-10-29).
