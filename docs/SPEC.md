# flightright-agent — Technical Specification

**Author:** Sri Sai Charan
**Status:** v1.4 (source of truth for what the system does)
**Location in repo:** `docs/SPEC.md`. Requirement changes are recorded in the changelog at the bottom.

---

## 1. Overview

### 1.1 Problem
Flight disruptions create surges of stressed passengers contacting support. Automated replies often:
- stall instead of acting,
- misstate legal entitlements,
- misuse "extraordinary circumstances,"
- push vouchers without disclosing cash rights,
- strike the wrong tone with people in genuine trouble.

### 1.2 Solution
A support system in which:
1. a deterministic **entitlement engine** computes what the passenger is legally owed;
2. a **support agent** drafts replies using that entitlement and operational tools;
3. a **severity-aware, context-reading LLM judge** gates every reply. It scores relevance, context awareness, urgency, tone, grievance handling, correctness, actionability, and fairness, with strictness scaled to the passenger's situation;
4. **n8n** handles the operational plumbing around the agent: inbound channels, handoff tickets, SLA escalation, alerts, and daily digests;
5. an **ops API and console** make every judge decision inspectable;
6. a **Judge Workbench** shows what the judge is doing, accepts verdict corrections, and uses those corrections to improve the judge without fine-tuning (FR-16).

### 1.3 Goals
- **G1:** Zero contradictions between replies and the entitlement engine.
- **G2:** Near-zero bad replies sent on high-severity (S3/S4) cases.
- **G3:** A judge validated against planted errors and public human-judgment data, with known biases measured.
- **G4:** Evidence that severity-aware judging beats fixed-strictness judging on safety *and* cost.
- **G5:** Every judge decision is inspectable via API and UI.
- **G6:** A realistic operational loop (channels, handoff, SLA, alerting) via n8n.
- **G7:** Everything runs at $0: free model tiers, local models, free/self-hosted services, free public data.

### 1.4 Non-goals
- Baggage claims, US DOT rules, UK261 divergence (stretch only).
- Real airline system integrations (all services are mocks behind interfaces).
- Legal advice. The system states published rules; the README says it is not legal advice.
- Model fine-tuning or RLHF. (The Judge Workbench adapts prompts, exemplars, rubric anchors, and thresholds only; model weights never change.)
- Paid services of any kind.

### 1.5 Users
| User | Needs |
|---|---|
| **Passenger** | Fast, correct, empathetic answers and concrete next steps |
| **Human support agent** | Clear handoff summaries; a queue; the ability to resolve cases |
| **QA / ops analyst** | Inspect judge verdicts, filter failures, see metrics and fairness gaps, correct the judge |
| **Engineer** | Reproducible evals, tests, traces, config-driven behavior |

---

## 2. System architecture

### 2.1 Components

```
               ┌──────────── n8n (self-hosted, Docker) ─────────────┐
 Channels ───► │ W1 inbound → API → reply out                        │
 (webhook/     │ W2 handoff ticket + notify + SLA escalation         │
  Telegram/    │ W3 daily ops digest   W4 threshold alerts           │
  email)       │ W5 human resolution callback                        │
               └───────────────┬─────────────────────▲───────────────┘
                               │ REST                │ signed event webhooks
                               ▼                     │
        ┌──────────────── FastAPI service ───────────┴──────────────┐
        │ customer API · ops API · SSE stream · persistence (SQLModel)│
        └───────────────┬───────────────────────────────────────────┘
                        ▼
        ┌──────────── LangGraph conversation graph ─────────────────┐
        │ guardrails → context_loader → triage → entitlement_engine  │
        │ → support_agent → code_checks → judge → router             │
        │            ↑ revise (≤2)           ├─ send                 │
        │            └───────────────────────├─ handoff → summary    │
        └───────┬──────────────┬─────────────┴──────────┬────────────┘
                ▼              ▼                        ▼
       rules_engine      mock services            judgekit (separate repo)
       (versioned YAML)  (flights, rebooking,     generic judge engine
                          vouchers, refunds,      + judge feedback store
                          tickets, history)         (FR-16)
```

### 2.2 Separation of responsibilities (hard rules)
| Concern | Owner | Never owned by |
|---|---|---|
| Legal entitlements, amounts, regime selection | `rules_engine` (code) | any LLM |
| Control flow, retries, routing | LangGraph graph + `router` (code) | any LLM, n8n |
| Reply drafting | `support_agent` (LLM) | — |
| Severity classification | `triage` (separate from the agent) | `support_agent` |
| Communication-quality judgment | `judge` via judgekit (LLM) | code (except must-pass inputs) |
| Channels, tickets, notifications, SLAs, schedules | **n8n** | the Python core |
| Business and judging logic | Python core | **n8n** (n8n holds no reasoning or judging logic) |

---

## 3. Functional requirements

IDs are referenced in tests (`test_FR_x_y_*`) and in the step plan.

### FR-1 Conversation handling
- **FR-1.1:** Create a conversation; accept passenger messages; return the final approved reply only.
- **FR-1.2:** Multi-turn. Each conversation is a LangGraph thread with checkpointed state.
- **FR-1.3:** A passenger can ask for a human at any time; this forces handoff.
- **FR-1.4:** Each message carries an idempotency key; duplicate deliveries (e.g. n8n retries) never produce duplicate replies or actions.

### FR-2 Guardrails
- **FR-2.1:** Detect prompt injection. Respond safely and log the attempt; never change entitlements.
- **FR-2.2:** Detect abuse or harassment. De-escalate; after a configured threshold, offer handoff.
- **FR-2.3:** PII minimization:
  - never persist passport or ID numbers,
  - mask booking references and names in logs and in ops responses by default.
- **FR-2.4:** Off-topic requests get a polite redirect.

### FR-3 Triage
- **FR-3.1:** Output a `TriageResult` (§5) per message: tier S1–S4, urgency (with deadline if any), frustration 1–5, vulnerability flags, repeat-contact count, reasons.
- **FR-3.2:** Rules + LLM. When the two disagree or confidence is low, take the higher tier.
- **FR-3.3:** Vulnerability flags include the specific-needs categories (reduced mobility, infants, unaccompanied minors, pregnant passengers, medical assistance).
- **FR-3.4:** The judge may bump the tier (never lower it), with a stated reason; the bump is logged.

### FR-4 Entitlement engine
- **FR-4.1:** A pure function: `compute_entitlement(case: DisruptionCase) -> Entitlement`. No I/O and no LLM calls.
- **FR-4.2:** **Regime selection by scheduled departure date:**
  - `eu261_2004` for flights before 2027-10-23,
  - `eu261_amended_2027` from that date.

  The boundary date lives in config.
- **FR-4.3:** All amounts, thresholds, distance bands, and time windows come from versioned YAML rulesets.
- **FR-4.4:** Distance is great-circle distance from OurAirports coordinates. Per regime, it's measured by flight sector or by overall journey to the final destination.
- **FR-4.5:** **Covers:**
  - applicability (scope),
  - delay compensation,
  - cancellation (with notice-period rules),
  - denied boarding,
  - missed connections,
  - the reduction-on-rerouting rule,
  - extraordinary-circumstances exemption,
  - right to care (meals, refreshments, communication, hotel, with the amended regime's caps),
  - refund/rerouting rights,
  - voucher conditions,
  - claim deadlines where defined.
- **FR-4.6:** Output includes the regime, every right with amount or obligation, the applicable article references, and a human-readable reason trace (each rule evaluated → result).
- **FR-4.7:** Any rule marked `unverified: true` in YAML makes the engine set `entitlement.confidence = "unverified"`. The agent must then hand off rather than state an amount.

### FR-5 Context and tools
- **FR-5.1:** `context_loader` gathers flight status, passenger history (prior cases, contacts), and preferences.
- **FR-5.2:** Agent tools:
  - `get_rebooking_options`,
  - `issue_care_voucher(type, amount)`,
  - `initiate_refund`,
  - `create_ticket`,
  - `get_playbook(disruption_type)` (keyed lookup),
  - `get_passenger_history(passenger_ref)` (prior cases, contacts, resolutions).
- **FR-5.3:** Tools enforce authority limits from config (e.g. maximum voucher values without a human).

### FR-6 Support agent
- **FR-6.1:** Drafts replies grounded in the `Entitlement` and tool outputs.
- **FR-6.2:** **Offer ladder:** urgent care first → rebooking/rerouting → refund → compensation owed.
- **FR-6.3:** May offer vouchers only alongside an explicit statement of the cash/refund right.
- **FR-6.4:** On revision, receives the judge's `revision_guidance` and the failed checks, and must address each one.
- **FR-6.5:** Never states an amount, deadline, or timeline not present in the engine or tool outputs.

### FR-7 Code checks (deterministic, run before the judge)
- **FR-7.1:** Amounts and rights mentioned match the `Entitlement` (via structured claim extraction from the draft, then comparison).
- **FR-7.2:** The regime referenced (if any) matches the flight date.
- **FR-7.3:** A voucher mention without a cash-right disclosure fails.
- **FR-7.4:** Required care obligations (per the engine) are mentioned when applicable.
- **FR-7.5:** No promises beyond authority (refund timelines, compensation not in the entitlement).
- **FR-7.6:** Results are passed to the judge as must-pass inputs. **The LLM judge can never override a code-check failure.**

### FR-8 Judge
- **FR-8.1:** Implemented via judgekit; this repo provides airline rubrics, anchors, planted errors, and an adapter.
- **FR-8.2:** Inputs: full conversation, triage, tool outputs, entitlement, code-check results, case history, draft, and (when enabled) reviewer-correction exemplars (FR-16.3).
- **FR-8.3:** **Eight dimensions:** relevance, context awareness, urgency recognition, tone/emotional attunement, grievance handling, correctness/grounding, actionability/clarity, fairness/safety. Each is scored 1–5 with written anchors, or must-pass (§6).
- **FR-8.4:** Every score cites an evidence quote from the draft. A score without evidence is invalid and triggers a judge retry.
- **FR-8.5:** Severity-scaled pass bars from config (§6.3).
- **FR-8.6:** S3 uses a 2-judge panel; disagreement routes to handoff.
- **FR-8.7:** Output is a `Judgement` (§5), including analysis and revision guidance.
- **FR-8.8:** Judge models differ from the support-agent model; at least one panel judge comes from a different model family where available.

### FR-9 Routing and handoff
- **FR-9.1:** Approve → send. Fail with retries remaining (default 2) → revise. Otherwise → handoff.
- **FR-9.2:** S4 always routes to handoff; the agent may send a short holding message that is itself judged.
- **FR-9.3:** `handoff_summary` produces a `HandoffTicket` (issue, entitlement, actions tried, passenger state, urgency, recommended next step), judged for completeness and accuracy.
- **FR-9.4:** Handoff emits a `handoff.created` event to n8n (§8).
- **FR-9.5:** A human resolution comes back via `POST /ops/handoffs/{id}/resolve` and is stored as episodic memory.

### FR-10 Memory
- **FR-10.1:** **Working memory:** LangGraph checkpointer (SQLite).
- **FR-10.2:** **Episodic:** passenger cases, contacts, human resolutions, looked up by passenger and booking reference.
- **FR-10.3:** **Semantic:** passenger preferences, airport data.
- **FR-10.4:** **Procedural:** playbooks per disruption type; rubrics per tier; judge-correction exemplars (FR-16).
- **FR-10.5:** **No vector retrieval (RAG).** Legal entitlements come from the engine, playbooks are keyed by disruption type, history is keyed by passenger, and judge exemplars are selected by dimension and recency (FR-16.3). There is no embedding model or vector index.

### FR-11 Ops API
See §7. Every draft attempt, check result, and judgement is persisted and queryable.

### FR-12 n8n workflows
See §8.

### FR-13 Simulator
- **FR-13.1:** Multi-turn LLM personas (temperament, circumstances, English proficiency, honesty, injection attempts) that accept or reject offers, follow up, and escalate.
- **FR-13.2:** A handoff oracle resolves tickets from scenario ground truth, with configurable noise.
- **FR-13.3:** Seeded and cached, so reruns are reproducible.

### FR-14 Evaluation
See §10.

### FR-15 UI
See §11.

### FR-16 Judge Workbench (human-in-the-loop judge calibration)
Not RLHF: no reward model, no weight updates. The judge model is fixed; what adapts is versioned context around it.
- **FR-16.1 Inspect:** for any judgement, show the inputs the judge saw, per-dimension scores with evidence highlighted in the draft, the code-computed decision and why (which bar failed), panel votes, and which exemplar set and rubric version were used.
- **FR-16.2 Correct:** a reviewer can override any dimension score or must-pass result and the overall verdict, with a short reason, and can submit "for prompts like this, the expected reply looks like this" examples (prompt + expected reply + optional notes).
- **FR-16.3 Learn (few-shot from corrections):** corrections become exemplars in a judge feedback store; at judge time up to `k` exemplars are added to the judge prompt, chosen by matching dimension, tier, and disruption type, most recent first (no embeddings). Toggle and `k` in config.
- **FR-16.4 Learn (rubric and thresholds):** when corrections on a dimension show a consistent pattern, the workbench proposes an anchor-text edit or a tier-bar change as a diff; it is applied only on explicit approval in the workbench, creating a new rubric version.
- **FR-16.5 Guard against leakage:** corrections and exemplars are accepted only for `dev`-split items and live/demo conversations, never `test`. Every judgement records `exemplar_set_version` and `rubric_id`/version; exemplar sets are immutable once used in an eval run.
- **FR-16.6 Measure:** show judge–reviewer agreement over time and before/after each exemplar-set or rubric version, on a fixed `dev` slice (no extra labeling required).
- **FR-16.7 Effort:** the workbench never requires bulk labeling; every correction is optional and one click plus an optional reason.

### FR-17 Judge Monitor (real-time judge performance)
A live, system-wide view of what the judge is deciding on the support agent's replies, and how well.
- **FR-17.1 Live feed:** every judgement across all conversations streams in as it happens (tier, decision, verdict, failed dimensions, evidence snippet, latency, model, attempt number), with filters by tier, decision, and dimension; each row opens the scorecard (screen 3) and diff (screen 4).
- **FR-17.2 Rolling metrics:** over selectable windows (last 15 min / 1 h / 24 h / 7 d): approve / revise / handoff rates per tier, pass rate per dimension, revisions per reply, S3 panel disagreement rate, judge latency p50/p95, requests and free-tier quota used per model, fallback rate, evidence-retry rate.
- **FR-17.3 Quality signals:** latest planted-error catch rate and controlled-pair accuracy (from the most recent eval or canary run), judge–reviewer agreement from workbench corrections (FR-16.6), and drift versus the previous window.
- **FR-17.4 Canary checks:** an optional scheduled job replays a small fixed set of planted-error drafts through the live judge (default: daily, ≤ 20 drafts, within free-tier budget) so catch rate is tracked continuously, not only at eval time.
- **FR-17.5 Alerts:** threshold breaches (e.g. S3 leak signal, disagreement spike, catch-rate drop, quota near limit) emit `metrics.threshold_breached` to n8n (W4).
- **FR-17.6 Data source:** reads only from the API (`/ops/judgements/stream`, `/ops/metrics/judge?window=`); no judging logic in the UI.

---

## 4. Non-functional requirements

| ID | Requirement | Target (initial; config-driven) |
|---|---|---|
| NFR-1 | Reply latency (end-to-end, excluding channel) | p95 ≤ 8 s S1–S2, ≤ 15 s S3 (panel); report actuals (free-tier rate limits may dominate) |
| NFR-2 | Cost per conversation turn | $0 (free tiers); per-tier budgets of tokens/requests/steps in config; enforced; exceeding → handoff; per-model daily usage ledger stops below free-tier limits |
| NFR-3 | Reliability | Fallback chain primary → backup model(s) → handoff; no unjudged reply is ever sent |
| NFR-4 | Idempotency | Duplicate inbound deliveries produce no duplicate replies, vouchers, refunds, or tickets |
| NFR-5 | Security | Ops endpoints need an API key; n8n ↔ API webhooks signed with HMAC-SHA256 + timestamp (replay window 5 min); secrets only in `.env` / n8n credentials store |
| NFR-6 | Privacy | PII masked in logs and ops responses by default; no ID numbers persisted |
| NFR-7 | Observability | Structured JSON logs with `conversation_id`, `attempt_id`, `judgement_id`; tracing via LangSmith (free tier); per-node token/request/latency |
| NFR-8 | Reproducibility | Seeds everywhere; LLM call cache; eval runs record config hash, model IDs, ruleset versions, rubric and exemplar-set versions, git commit |
| NFR-9 | Testability | CI runs with no API keys (all LLMs mocked); engine coverage ≥ 95% branches |
| NFR-10 | Portability | Runs on Windows (PowerShell) and Linux; `docker compose up` brings up API + n8n |
| NFR-11 | Accessibility (UI) | Keyboard navigable; color not the only signal; respects `prefers-reduced-motion` |
| NFR-12 | Zero cost | Only free model tiers (Groq, Google AI Studio), local models (Ollama, fastembed), free/self-hosted services (n8n community edition, LangSmith free tier), and free public data |

---

## 5. Data models (Pydantic v2; names are normative, fields may grow)

```python
class DisruptionCase(BaseModel):
    flight_no: str; carrier_iata: str; carrier_is_eu: bool
    origin_iata: str; destination_iata: str; final_destination_iata: str
    connection_iatas: list[str] = []
    scheduled_departure: datetime; scheduled_arrival: datetime
    actual_arrival: datetime | None
    disruption_type: Literal["delay","cancellation","denied_boarding","missed_connection"]
    cause_code: str                      # maps to ordinary/extraordinary per ruleset
    notice_given_days: int | None        # for cancellations
    rerouting_offered: ReroutingOffer | None
    passenger_flags: list[VulnerabilityFlag] = []

class Entitlement(BaseModel):
    regime: Literal["eu261_2004","eu261_amended_2027"]
    ruleset_version: str
    in_scope: bool
    compensation_eur: int | None; compensation_reason: str
    reduction_applied: bool
    care: list[CareObligation]           # meals, refreshments, hotel(nights cap), comms
    refund_or_rerouting: RerouteRefundRights | None
    voucher_allowed_only_with_consent: bool
    claim_deadline: str | None
    articles: list[str]
    reason_trace: list[RuleStep]
    confidence: Literal["verified","unverified"]

class TriageResult(BaseModel):
    tier: Literal["S1","S2","S3","S4"]; urgency: Literal["none","soon","immediate"]
    urgency_deadline: datetime | None; frustration: conint(ge=1, le=5)
    vulnerability_flags: list[VulnerabilityFlag]; repeat_contact_count: int
    reasons: list[str]; source: Literal["rules","llm","merged"]

class DraftAttempt(BaseModel):
    attempt_id: str; conversation_id: str; attempt_no: int
    draft_text: str; tool_calls: list[ToolCallRecord]
    code_checks: list[CheckResult]; created_at: datetime

class DimensionScore(BaseModel):
    dimension: Dimension; score: conint(ge=1, le=5) | None; passed: bool | None
    evidence: str; rationale: str

class Judgement(BaseModel):
    judgement_id: str; attempt_id: str; conversation_id: str
    severity_tier: str; tier_bumped_from: str | None; tier_bump_reason: str | None
    verdict: Literal["good","acceptable","bad"]
    decision: Literal["approve","revise","handoff"]
    scores: list[DimensionScore]
    analysis: str; revision_guidance: str | None
    panel: PanelInfo | None; models: dict[str, str]
    rubric_id: str; prompt_version: str; exemplar_set_version: str | None
    cost_usd: float; latency_ms: int; created_at: datetime

class JudgeCorrection(BaseModel):     # FR-16
    correction_id: str; judgement_id: str | None; split: Literal["dev","live"]
    dimension_overrides: dict[Dimension, int | bool]; verdict_override: str | None
    expected_reply: str | None; prompt_text: str | None
    reason: str | None; created_at: datetime

class HandoffTicket(BaseModel):
    ticket_id: str; conversation_id: str; tier: str; urgency: str
    summary: str; entitlement: Entitlement; actions_tried: list[str]
    passenger_state: str; recommended_next_step: str
    status: Literal["open","acknowledged","resolved","escalated"]
    sla_due_at: datetime; resolution: Resolution | None

class Scenario(BaseModel):          # synthetic eval case with ground truth
    scenario_id: str; case: DisruptionCase; persona: Persona
    opening_message: str; gold_entitlement: Entitlement; gold_tier: str
    gold_key_actions: list[str]; counterfactual_group: str | None
    planted_error: PlantedError | None
```

---

## 6. Judge specification

### 6.1 Dimensions
| # | Dimension | Type | Anchor sketch (full anchors in rubric YAML) |
|---|---|---|---|
| 1 | relevance | 1–5 | 1 = ignores the question · 3 = answers the main ask, misses a part · 5 = answers every part directly |
| 2 | context_awareness | 1–5 | 1 = re-asks for given info, ignores history · 3 = uses some context · 5 = uses all relevant context, acknowledges repeat contact |
| 3 | urgency | must-pass | Time-critical action appears first when urgency ≠ none |
| 4 | tone | 1–5 | 1 = cheerful or robotic in distress · 3 = polite but generic · 5 = calm, specific empathy proportional to the situation |
| 5 | grievance_handling | 1–5 | 1 = deflects or blames · 3 = apologizes, partial resolution · 5 = hears, empathizes, apologizes, resolves, sets follow-up |
| 6 | correctness | must-pass | All code checks pass, and no unsupported factual claims |
| 7 | actionability | 1–5 | 1 = no next step · 3 = next step without timeline · 5 = concrete steps, owner, timeline, right length |
| 8 | fairness_safety | must-pass | No discriminatory or unsafe content; nothing that would differ for a different passenger with the same case |

### 6.2 Rubric YAML schema (in `config/rubrics/airline_v1.yaml`)

```yaml
rubric_id: airline_v1
dimensions:
  - id: tone
    type: scale_1_5
    question: "Does the tone fit the passenger's emotional state and situation?"
    anchors:
      1: "Cheerful, flippant, or robotic in the face of distress..."
      3: "Polite but generic; empathy not tied to specifics..."
      5: "Calm, specific acknowledgment proportional to severity..."
    evidence_required: true
  - id: urgency
    type: must_pass
    question: "If urgency is not 'none', is the time-critical action stated first?"
    evidence_required: true
tiers:
  S1: {min_scores: {relevance: 3, tone: 3}, must_pass: [correctness], panel: 1}
  S2: {min_scores: {relevance: 3, tone: 3, actionability: 3, context_awareness: 3}, must_pass: [correctness, fairness_safety], panel: 1}
  S3: {min_scores: {relevance: 4, tone: 4, grievance_handling: 4, actionability: 4, context_awareness: 4}, must_pass: [urgency, correctness, fairness_safety], panel: 2}
  S4: {judge_target: handoff_summary, must_pass: [correctness, fairness_safety], panel: 1}
```

### 6.3 Decision logic (code, not LLM)
1. Any code-check failure → `revise` (or `handoff` if retries are exhausted).
2. Any must-pass failure → `revise` / `handoff`.
3. Any score below the tier minimum → `revise` / `handoff`.
4. S3 panel disagreement (one passes, one fails) → `handoff`.
5. Otherwise → `approve`.

The verdict label (good/acceptable/bad) comes from the judge, but the **decision is computed by code** from scores and config.

### 6.4 Judge robustness
- Evidence quotes are verified to be **actual substrings** of the draft (code check). If not, the judge output is rejected and retried once, then the case goes to handoff.
- Structured-output validation failure → one retry with the error, then handoff.
- Judge prompts, rubrics, and exemplar sets are versioned; every judgement stores the `rubric_id`, prompt version, and exemplar-set version.

---

## 7. API specification (FastAPI)

### 7.1 Customer endpoints
| Method | Path | Request | Response | Notes |
|---|---|---|---|---|
| POST | `/conversations` | `{channel, passenger_ref?, booking_ref?}` | `{conversation_id}` | |
| POST | `/conversations/{id}/messages` | `{text, idempotency_key}` | `{reply_text, status: "answered"\|"handed_off", ticket_id?}` | Never returns drafts, scores, or analysis |

### 7.2 Ops endpoints (header `X-API-Key`)
| Method | Path | Purpose |
|---|---|---|
| GET | `/ops/conversations/{id}` | Conversation with messages, triage per turn, final replies |
| GET | `/ops/conversations/{id}/judgements` | All judgements (all attempts) in order |
| GET | `/ops/judgements/{judgement_id}` | Full `Judgement` + linked `DraftAttempt` + code checks |
| GET | `/ops/judgements/{judgement_id}/diff` | Failed draft vs. next revision + guidance that caused it |
| GET | `/ops/judgements` | Filters: `verdict`, `decision`, `tier`, `dimension_failed`, `since`, `until`; cursor pagination |
| GET | `/ops/conversations/{id}/stream` | SSE events (§7.3) |
| GET | `/ops/judgements/stream` | SSE feed of every `judgement.completed` event across conversations (FR-17.1) |
| GET | `/ops/handoffs` | Filter by `status`, `tier`, `sla_breached` |
| POST | `/ops/handoffs/{id}/acknowledge` | Human acknowledges (stops the SLA escalation timer) |
| POST | `/ops/handoffs/{id}/resolve` | `{decision, notes, compensation_paid?}` → stored as episodic memory |
| POST | `/ops/judgements/{judgement_id}/corrections` | FR-16.2 correction (rejected for `test`-split items) |
| POST | `/ops/judge/examples` | FR-16.2 "prompt → expected reply" example |
| GET | `/ops/judge/corrections` | List corrections; filter by dimension, split, date |
| GET | `/ops/judge/proposals` | FR-16.4 proposed rubric/threshold diffs |
| POST | `/ops/judge/proposals/{id}/apply` | Apply an approved proposal → new rubric version |
| GET | `/ops/metrics/judge` | `?window=15m\|1h\|24h\|7d`. Pass rates by tier and dimension, retry/handoff rates, panel disagreement, requests/latency per tier, judge–reviewer agreement by version |
| GET | `/ops/metrics/fairness` | Counterfactual-pair gaps |
| GET | `/healthz` | Liveness, plus version info (git commit, ruleset versions, rubric ID, exemplar-set version) |

- **PII:** masked by default; `?unmask=true` requires a second key (`X-Admin-Key`).
- **Errors:** RFC 7807 problem+json.
- **Docs:** all responses use Pydantic models, so `/docs` is fully typed.

### 7.3 SSE event types (`/ops/conversations/{id}/stream`)
- `triage.completed`
- `entitlement.computed`
- `draft.created`
- `checks.completed`
- `judgement.completed`
- `revision.started`
- `reply.sent`
- `handoff.created`

Each event carries `{event, conversation_id, attempt_no, payload, ts}`.

### 7.4 Outbound events to n8n
The API POSTs signed events to `N8N_EVENTS_WEBHOOK_URL`:
- `handoff.created`
- `handoff.sla_breached` (computed by n8n; included for symmetry)
- `metrics.threshold_breached`
- `guardrail.injection_detected`

**Signing:** header `X-Signature: sha256=<hmac>` over `timestamp + body`, with `X-Timestamp`. Retries use exponential backoff, and each event has an `event_id` for idempotency.

---

## 8. n8n specification

### 8.1 Deployment
- Self-hosted **n8n community edition** (free) via `docker compose` alongside the API (service names `api`, `n8n`). Data persisted in a Docker volume. No paid n8n features (cloud, enterprise SSO, etc.).
- Credentials (Telegram bot token, Slack webhook, SMTP, API keys, HMAC secret) live **only** in n8n's credentials store and `.env`, never in exported workflow JSON.
- Workflows are exported to `n8n/workflows/*.json` and committed. Add `scripts/n8n_export` and `scripts/n8n_import` helpers (via the n8n CLI inside the container).
- **No reasoning or judging logic in n8n.** Function/Code nodes are allowed only for formatting, mapping, and signature verification.

### 8.2 Workflows

| ID | Name | Trigger | Steps | Failure handling |
|---|---|---|---|---|
| **W1** | Inbound channel → agent → reply | Webhook (generic JSON) **and** one real channel (Telegram bot or email/IMAP, §8.4) | Normalize message → find or create the conversation (map channel user → `conversation_id`) → `POST /conversations/{id}/messages` with `idempotency_key` = channel message ID → send `reply_text` back on the same channel | Retry ×3 with backoff; on persistent failure send a safe holding message and alert (W4) |
| **W2** | Handoff ticketing + SLA | API event `handoff.created` (signed) | Verify HMAC + timestamp → post the ticket summary to Slack/email (tier, urgency, link to the ops console) → **Wait** until `sla_due_at` (from config per tier: e.g. S4 10 min, S3 15 min, S2 60 min) → check `GET /ops/handoffs/{id}`; if not acknowledged → escalate to the supervisor channel and mark `escalated` | Signature failure → drop + log; API unreachable → retry, then alert |
| **W3** | Daily ops digest | Cron (08:00 local, configurable) | `GET /ops/metrics/judge` + `/ops/metrics/fairness` + `/ops/handoffs?status=open` → format a digest (pass rates by tier, severe-case leaks, handoffs, SLA breaches, free-tier usage) → send to Slack/email | Partial data → send with a warning section |
| **W4** | Threshold alerts | API event `metrics.threshold_breached` or `guardrail.injection_detected`, plus a cron poll every 15 min | Verify HMAC → route by type → alert channel with context and links | Deduplicate repeated alerts within 1 h |
| **W5** | Human resolution callback | Slack interactive action or a simple n8n form (§8.4) | Human picks acknowledge/resolve + notes → `POST /ops/handoffs/{id}/acknowledge` or `/resolve` → confirm back to the human | Validation errors shown back on the form |

### 8.3 n8n acceptance tests
- A scripted test (`scripts/test_n8n_flows.py`) runs against the docker-compose stack:
  - sends webhook messages,
  - asserts replies come back once (idempotency),
  - triggers a handoff and asserts the ticket notification,
  - fast-forwards the SLA in test config (e.g. 10 seconds) and asserts escalation,
  - posts a resolution and asserts the API state change.
- Exported workflow JSON passes a check that **no credentials or secrets are embedded**.

### 8.4 Free-tier options (one of each is wired in Step 10)
- **Real channel:** Telegram bot (free) or an email inbox via IMAP/SMTP.
- **Notifications:** Slack free workspace with an incoming webhook, or email.
- **Human actions:** Slack interactive buttons, or an n8n Form trigger (simplest, no Slack app needed).

---

## 9. Rule engine specification

### 9.1 Ruleset YAML (illustrative structure; values must be verified in Step 1)

```yaml
ruleset: eu261_2004
version: "2004.1"
applies_to: {departure_before: "2027-10-23"}
sources: [{doc: "Regulation (EC) No 261/2004", url: "<EUR-Lex link>"}]
distance_basis: flight_sector          # amended: overall_journey
compensation:
  bands:
    - {max_km: 1500, eur: 250}
    - {max_km: 3500, eur: 400, also_intra_eu_over: 1500}
    - {max_km: null, eur: 600}
  delay_trigger_hours: 3
  reduction_on_rerouting: {...}
extraordinary_circumstances: {cause_codes: [...]}
cancellation_notice_rules: [...]
care: {...}
vouchers: {requires_consent: true}
unverified: []                         # list rule keys pending confirmation
```

### 9.2 Testing requirements
- **Table-driven tests:** at least one test per rule branch per regime, named after the article.
- **Property-based tests (hypothesis):**
  - compensation is monotonic in distance band,
  - never negative,
  - extraordinary circumstances never remove care obligations,
  - longer notice never increases compensation,
  - regime selection flips exactly at the boundary date.
- **Golden tests:** a set of hand-checked cases with expected outputs in `tests/golden/`, each checked by hand against the regulation text.

---

## 10. Evaluation specification

### 10.1 Datasets
- **Scenarios (~500):** every engine branch, both regimes, all tiers, vulnerability flags, multi-turn follow-ups.
- **Counterfactual groups:** the same case with varied name, English proficiency, tone, and loyalty tier.
- **Planted-error set (primary judge ground truth):** good replies mutated with one known error each, across all dimensions and tiers:
  - wrong amount, wrong regime, fake extraordinary circumstances,
  - voucher push without disclosure, missing care, buried urgency,
  - cheerful in crisis, generic apology, blame-shifting,
  - re-asking for given info, ignoring repeat contact, partial answer.

  Each planted error also yields a **controlled pair** (original vs. mutated) with a known preferred side.
- **Public human-judgment data (free, license-checked, recorded in `DATA.md`):** e.g. MT-Bench human pairwise judgments and HelpSteer2 attribute ratings, used to measure the judge's general agreement with human raters and its biases. Reported separately as general judge sanity checks, not as airline-domain validation.
- **Reviewer input (optional, low effort):** Judge Workbench corrections on `dev` (FR-16) and an optional set of ~30 airline A/B picks. No bulk labeling.
- **Splits:** `dev` for tuning judge prompts, exemplars, and thresholds; `test` held out until the final run, touched once.

### 10.2 Metrics (definitions)
| Metric | Definition |
|---|---|
| Entitlement accuracy | % of turns where every stated right/amount matches the gold entitlement |
| Regime accuracy | % of cases where the correct regime is used |
| Triage recall@S3+ | TP(S3/S4) / actual S3/S4 |
| Severe leak rate | Bad drafts (per gold or planted) **sent** on S3/S4 / all S3/S4 bad drafts |
| S1 over-blocking | Good drafts rejected at S1 / good S1 drafts |
| Catch rate | Planted errors flagged / planted errors, per dimension and tier |
| Controlled-pair accuracy | % of planted pairs where the judge scores the original above the mutated reply on the affected dimension, order-swapped |
| Public human agreement | Agreement with human pairwise preferences (order-swapped) and correlation with human attribute ratings on the public sets |
| Reviewer agreement (if available) | Agreement with the optional A/B picks and workbench corrections, with the small-n caveat stated |
| Judge bias | Score shift under verbosity padding, politeness padding with an injected error, order swap, model-family swap |
| Fairness gap | Max difference in mean quality score and pass rate across counterfactual variants; entitlement differences (must be 0) |
| Requests and latency per tier | Mean and p95 requests/tokens and ms per turn (all $0) |
| Simulator outcomes | Resolution rate, offer acceptance, turns to resolution, handoff rate |

### 10.3 Headline experiment
**Severity-aware context-reading judge vs. a fixed-strictness single judge** (S2-level bar everywhere, no panel, no triage context), compared on severe leak rate, S1 over-blocking, and requests per turn. The protocol and decision rule are pre-registered in `docs/eval_protocol.md` and committed before running.

### 10.4 Targets (initial, revisable before pre-registration)
| Metric | Target |
|---|---|
| Entitlement accuracy | 100% (any miss is a bug) |
| Triage recall@S3+ | ≥ 98% |
| Severe leak rate | ≤ 1% |
| S1 over-blocking | ≤ 10% |
| Catch rate (must-pass dimensions) | ≥ 95% |
| Controlled-pair accuracy (subjective dimensions) | ≥ 85% |
| Public human pairwise agreement | report (no fixed target; compared with published judge baselines where available) |
| Fairness entitlement differences | 0 |

### 10.5 Outputs
`eval/report.md` with tables, charts, the run manifest (git commit, config hash, model IDs, ruleset versions, rubric ID, exemplar-set version), and a limitations section that states the absence of large-scale domain-specific human labels.

---

## 11. UI specification
- **Framework:** Streamlit or FastAPI + React, chosen at Step 12.
- **Screens:**
  1. **Passenger chat:** channel-like view.
  2. **Live pipeline:** stepper driven by SSE (nodes light up; parallel steps visible; retry loops back with the guidance text).
  3. **Judge scorecard:** dimension scores with evidence highlighted inside the draft, analysis, decision, panel view.
  4. **Diff view:** failed draft vs. revision with the guidance.
  5. **Judgement explorer:** filters from `/ops/judgements`.
  6. **Handoff queue:** SLA countdowns; acknowledge/resolve (mirrors W5).
  7. **Metrics and fairness dashboards.**
  8. **Judge Workbench (FR-16):** "why this decision" trace, one-click score/verdict overrides with optional reason, "prompt → expected reply" examples, exemplar set browser, proposed rubric/threshold diffs to approve, agreement-over-versions chart.
  9. **Judge Monitor (FR-17):** live judgement feed, rolling-window metric tiles and trend charts, quality signals (catch rate, agreement, drift), alert history.
- **Motion:** subtle, purposeful, `prefers-reduced-motion` respected.
- **Assets:** Lucide icons (package), self-made SVG logo, illustrations, and animations; `ASSETS.md`; no scraping.
- **Data access:** the UI reads only from the API, with no business logic in the UI.

---

## 12. Configuration (`config/app_config.yaml` keys)
- **`models`:** providers, free-tier catalog (family, limits), per-role fallback chains (triage, agent, judge_a, judge_b, summary, simulator), usage ledger, cache.
- **`budgets`:** tokens, requests, and steps per turn, per tier.
- **`retries`:** max revisions (2), judge output retries (1).
- **`triage`:** rule weights, round-up policy.
- **`judge`:** rubric ID, tier bars (or rubric reference), panel sizes, correction exemplars (enabled, k).
- **`authority`:** voucher caps, refund initiation limits.
- **`regime`:** boundary date, ruleset paths.
- **`sla`:** per-tier minutes (test overrides).
- **`alerts`:** thresholds for leak rate, disagreement rate, free-tier usage.
- **`pii`:** masking rules.

---

## 13. Repository layout

```
├── config/                      # app_config.yaml, rulesets/, rubrics/
├── src/flightright/             # Python package
├── tests/                       # mirrors src/; FR-referenced test names
├── scripts/                     # CLI tools (commit-message lint, n8n helpers, eval runners)
├── eval/                        # eval protocol outputs, report.md
├── docker-compose.yml           # api + n8n
├── n8n/
│   ├── workflows/               # exported JSON (no secrets)
│   └── README.md                # setup, credentials to create, import/export
├── docs/SPEC.md                 # this file
├── DATA.md · ASSETS.md · README.md
```

---

## 14. Milestones and Definition of Done
| Step | Deliverable | Definition of Done |
|---|---|---|
| 0 | Foundations | uv project, CI green on a smoke test, pre-commit hooks (incl. commit-message lint), `docs/SPEC.md`, `.env.example`, `DATA.md`, `ASSETS.md`, README stub with diagram; provider choices made |
| 1 | Regulation research | `docs/regulation_notes.md` (plain English, article refs, both regimes); both ruleset YAMLs drafted; `unverified` list explicit; golden cases drafted and hand-checked before any engine code |
| 2 | Rule engine | FR-4 complete; table-driven + property + golden tests pass; ≥ 95% branch coverage |
| 3 | Mock services + memory | FR-5 tools with authority limits and idempotency; history store; playbooks; LLM client with fallback chains, usage ledger and cache (all mocked in tests) |
| 4 | Scenarios + personas | Generator per §10.1; pilot of 50 inspected before the full run; request/token estimate shown first |
| 5 | Triage | FR-3; triage eval shows recall@S3+ and a confusion matrix on `dev` |
| 6 | Agent graph | FR-1, 2, 6, 7, 9 (minus the judge); every routing path covered by mocked tests; idempotency tests |
| 7a | judgekit core (`judgekit` repo) | Generic engine, rubric loader (incl. 1–5 anchored scales), evidence enforcement, panels, planted-error framework, controlled pairs, calibration, pairwise, bias tests, exemplar-store hook; own CI |
| 7b | Judge integration | FR-8; airline rubric with anchors; decision logic per §6.3; evidence-substring check; planted-error unit tests pass |
| 8 | Simulator + handoff | FR-13; multi-turn runs end to end with the oracle |
| 9 | API | §7 fully implemented (incl. FR-16 endpoints); SSE; PII masking; problem+json; endpoint tests; `langgraph.json` for Studio |
| 10 | n8n | §8: docker compose, W1–W5, exported JSON with no secrets, `scripts/test_n8n_flows.py` passes, `n8n/README.md` |
| 11 | Evaluation | `docs/eval_protocol.md` written and **committed before results**; public human-judgment sets downloaded and license-checked; all §10 metrics; `eval/report.md` |
| 12 | UI | §11 incl. Judge Workbench and Judge Monitor; demo script; screenshots/GIF for the README |
| 13 | Polish | Final README (problem, architecture, judge design, results, limitations, how to run and demo), `docs/architecture.md`, `docs/judge_design.md`, CI badge |

---

## 15. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Misreading the regulation | Regulation notes and golden cases hand-checked before engine code; `unverified` flag forces handoff |
| Judge unreliability on subjective dimensions | Anchors, evidence enforcement, panels at S3, controlled planted pairs, public human-judgment checks, workbench corrections |
| Little domain-specific human validation | Stated as a limitation; optional ~30 A/B picks; workbench agreement tracked over versions |
| Judge biases | Explicit bias tests in judgekit; report the results |
| Workbench overfitting / eval leakage | Corrections only on `dev`/live; versioned, immutable exemplar sets; `test` touched once |
| Synthetic data too clean | Personas with messiness, non-native English, contradictions; multi-turn follow-ups |
| Free-tier quota exhaustion (quotas shared with other projects) | Cache, usage ledger with hard stops, fallback chains incl. local Ollama, pilot runs, dry-run estimates |
| n8n becoming a second brain | Hard rule: no reasoning in n8n; Code nodes for formatting only |
| Secrets leaking via workflow exports | Credentials store only + export secret-scan check |
| Duplicate actions from retries | Idempotency keys end to end (n8n → API → tools) |

---

## 16. Open questions (resolve at the noted step)
1. Delay thresholds and other details under the amended regime (Step 1).
2. ~~LLM, embedding, and tracing providers (Step 0).~~ Resolved: free Groq + Google AI Studio tiers with local Ollama fallback; judge family ≠ agent family; no embeddings (FR-10.5); LangSmith free tier.
3. ~~Real channel for W1~~ Resolved: generic webhook first; Telegram or email can be added later.
4. ~~Human-action surface for W5~~ Resolved: n8n Form; notifications logged and shown in the UI.
5. ~~UI framework~~ Resolved: FastAPI + React.
6. ~~CrewAI for the simulator~~ Resolved: CrewAI persona agents on the project's LLM client (cache, ledger, fallback); not used in the core graph.

---

## Changelog
- v1.0: initial specification.
- v1.1 (2026-10-19): zero-cost requirement (G7, NFR-12) and free provider choices (§16 Q2); FR-16 Judge Workbench (human-in-the-loop judge calibration, not RLHF) with API endpoints, `JudgeCorrection` model, and UI screen 8; judge validation based on planted-error controlled pairs + free public human-judgment sets + optional low-effort reviewer input instead of large-scale manual labeling (§10.1, §10.2, §10.4); repo layout (§13).
- v1.4 (2026-10-29): open questions 3-6 resolved (webhook channel, n8n Form for handoff actions, FastAPI + React UI, CrewAI simulator); conversation graph loads context before triage (triage uses history and booking assistance flags).
- v1.3 (2026-10-25): no vector retrieval (RAG): FR-10.5 replaced, FR-5.2 tools are keyed lookups (`get_playbook`, `get_passenger_history`), FR-16.3 exemplars chosen by dimension/tier/recency, retrieval and embedding config removed, Step 3 rescoped.
- v1.2 (2026-10-25): FR-17 Judge Monitor (system-wide live judgement feed, rolling metrics, canary checks, alerts), `/ops/judgements/stream`, windowed `/ops/metrics/judge`, UI screen 9.
