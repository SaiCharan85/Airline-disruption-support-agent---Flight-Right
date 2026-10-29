# Judge design

Every reply the support agent drafts is graded before it can be sent. The judge is built on
[JudgeKit](https://github.com/SaiCharan85/JudgeKit) scored rubrics; the airline-specific parts live here:
the rubric (`config/rubrics/airline_v1.yaml`), the adapter to the project's LLM client, the decision
logic and the planted errors (`src/flightright/judge/`).

## What the judge reads (FR-8.2)

The draft plus: the conversation so far, the triage result (tier, urgency, frustration, special needs,
earlier contacts), what the passenger is owed according to the rules engine, and the results of the
deterministic code checks. The rules-engine facts are given as correct by definition, so the judge never
relies on its own knowledge of passenger rights.

## Dimensions

| Dimension | Type | What a 5 / pass looks like |
|---|---|---|
| relevance | 1–5 | Answers every part of the passenger's message |
| context_awareness | 1–5 | Uses everything known; acknowledges repeat contact and special needs |
| urgency | check | Time-critical help comes first (passes automatically when urgency is none) |
| tone | 1–5 | Calm, specific acknowledgement in proportion to the situation |
| grievance_handling | 1–5 | Hears, empathises, apologises, resolves, sets a follow-up |
| correctness | check | Every claim supported by the context; fails if any code check failed |
| actionability | 1–5 | Concrete next steps with owner and timing |
| fairness_safety | check | Nothing discriminatory or unsafe; same reply for any passenger with this case |

Each answer must quote the reply. JudgeKit checks the quote is really there; an invented quote triggers
one retry, then the verdict is unusable and the conversation goes to a human.

## Pass bars per severity (SPEC §6.2)

| Tier | Minimum scores | Must pass | Judges |
|---|---|---|---|
| S1 | relevance 3, tone 3 | correctness | 1 |
| S2 | relevance, tone, actionability, context_awareness ≥ 3 | correctness, fairness_safety | 1 |
| S3 | relevance, tone, grievance_handling, actionability, context_awareness ≥ 4 | urgency, correctness, fairness_safety | 2 (different model families) |
| S4 / holding messages / handoff summaries | — | correctness, fairness_safety | 1 |

## Decision (code, not the model; SPEC §6.3)

1. A failed code check → revise. The LLM judge is not even called (its answer could not change it).
2. No trustworthy verdict (judge unavailable, or invented evidence twice) → hand off.
3. S3 panel members disagree on pass/fail → hand off.
4. A must-pass check fails or a score is below the tier's bar → revise, with the failures and the judge's
   revision guidance fed back to the agent (at most two revisions, then hand off).
5. Otherwise → approve.

The judge's overall label (good / acceptable / bad) is recorded but never decides anything.

## Keeping the judge honest

- **Model families:** the judges (Gemma 4, Gemini; Qwen for the second S3 judge) never share a family with
  the drafting model (gpt-oss), so no model grades its own writing.
- **Non-determinism:** free-tier models are not bit-exact even at temperature 0. Safety-critical facts are
  code; reruns of evaluations replay the exact-match cache; the S3 panel turns disagreement into a handoff;
  and `scripts/judge_consistency.py` measures how often scores and decisions flip when the same reply is
  judged again without the cache (`docs/judge_consistency.md`).
- **Planted errors (SPEC §10.1):** 13 error types (`src/flightright/judge/planted.py`) plant exactly one
  known defect in a good reply: wrong amount, wrong regime, invented extraordinary circumstances, voucher
  push, missing care, buried urgency, cheerful tone in distress, generic apology, blame-shifting, re-asking
  for given details, ignoring a repeat contact, partial answer, no next step. Each names the dimensions
  that should catch it; JudgeKit's controlled pairs measure whether the judge ranks the clean reply above
  the flawed one.
- **Reviewer corrections (FR-16.3):** corrections from the Judge Workbench can be shown to the judge as
  calibration examples (exemplars), from the dev split only.

## Every decision is recorded

Each judgement is stored with the conversation as a `Judgement` record (SPEC §5): tier, per-dimension
scores with evidence and rationale, the code-computed decision and its reason, panel members and
disagreement, models used, rubric and prompt versions, exemplar count and latency. These records feed the
ops API, the Judge Monitor and the Workbench.
