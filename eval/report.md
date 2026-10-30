# Evaluation report

Split: **test (held out, final run)**. Protocol: [docs/eval_protocol.md](../docs/eval_protocol.md). Every number below is computed by `scripts/eval_report.py` from the run records.

## Run manifest

Protocol frozen before the first final-run model call: `docs/eval_protocol.md` sha256 `33242711d7fb4c0d...` at 2026-10-29T10:18:21+00:00. Since then only the 'Changes after registration' section was added (change 1, applied in section 1b).

| Item | Value |
|---|---|
| Generated | 2026-10-30 19:08 UTC |
| Git commit | `f03cbed46795` |
| Config hash (app_config, rubric, triage) | `3052440e7bc2` |
| Rubric | `airline_v1@1.0`, prompt `judge-prompt-1` |
| Exemplar set | none (no reviewer exemplars in evaluation runs) |
| Rulesets | `eu261_2004` 2004.1, `eu261_amended_2027` 2027.1 |
| Primary models | judge_a: `google:gemma-4-26b-a4b-it`, judge_b: `groq:qwen/qwen3.8-27b`, agent: `groq:openai/gpt-oss-120b`, triage: `groq:openai/gpt-oss-20b` |
| Judge models actually called | `google:gemma-4-26b-a4b-it` (200 items), `google:gemini-3.5-flash-lite` (35 items), `groq:qwen/qwen3.8-27b` (21 items) |
| Items with a fallback judge model | 19 of 240 |
| Cache | bypassed for model calls |
| Cost | $0 (free tiers only) |

## Base replies

Drafted 55 scenarios; usable 40 (S1 12, S2 12, S3 16). Not usable: handed_off 15 (`handed_off` covers cases the engine sends to review under unverified rules).

Evaluation items: 120 (40 presumed-good originals, 80 with one planted error each).

## 1. Headline: severity-aware vs fixed-strictness judge

| Metric | Severity-aware (ours) | Fixed-strictness baseline | Target |
|---|---|---|---|
| Severe leak rate (S3 bad replies approved) | 16 % (5/32; 95 % CI 7 %-32 %) | 31 % (10/32; 95 % CI 18 %-49 %) | ≤ 1 % |
| S1 over-blocking (good S1 replies not approved) | 58 % (7/12; 95 % CI 32 %-81 %) | 25 % (3/12; 95 % CI 9 %-53 %) | ≤ 10 % |
| Model requests per item (mean) | 0.88 | 1.01 | — |
| No trustworthy verdict (judge unavailable; handed off) | 41 % (49/120; 95 % CI 32 %-50 %) | 14 % (17/120; 95 % CI 9 %-22 %) | — |
| Judge latency p95 (ms) | 557241 | 627099 | — |

**Decision rule (pre-registered): trade-off.** Leak lower by ≥ 5 points (or both ≤ 1 %): yes; over-blocking within 5 points of the baseline: no; requests within 2x: yes.

Paired exact McNemar test on the 32 S3 bad items (caught or not): caught only by ours 6, only by the baseline 1, p = 0.125.

### 1b. After re-judging infrastructure failures (protocol change 1)

Re-judged once: 39 severity-aware and 5 baseline items whose verdict failed on rate limits, provider 5xx errors or timeouts. Content failures are kept. The table above stays the registered primary result.

| Metric | Severity-aware (ours) | Fixed-strictness baseline |
|---|---|---|
| Severe leak rate | 16 % (5/32; 95 % CI 7 %-32 %) | 31 % (10/32; 95 % CI 18 %-49 %) |
| S1 over-blocking | 58 % (7/12; 95 % CI 32 %-81 %) | 25 % (3/12; 95 % CI 9 %-53 %) |
| Catch rate (all planted errors) | 82 % (66/80; 95 % CI 73 %-89 %) | 74 % (59/80; 95 % CI 63 %-82 %) |
| Controlled-pair accuracy | 34 % (20/59; 95 % CI 23 %-47 %) | 47 % (28/59; 95 % CI 35 %-60 %) |
| Model requests per item (mean) | 1.33 | 1.04 |
| No trustworthy verdict | 25 % (30/120; 95 % CI 18 %-33 %) | 11 % (13/120; 95 % CI 6 %-18 %) |

Decision rule on these numbers: **trade-off** (leak lower by >= 5 points: yes; over-blocking within 5 points: no; requests within 2x: yes). McNemar on the 32 S3 bad items: only ours 6, only the baseline 1, p = 0.125.

### 1c. Exploratory (not pre-registered): where the unavailable verdicts come from

| Cause (after change 1) | Severity-aware | Baseline |
|---|---|---|
| evidence: other dimension | 2 | 8 |
| evidence: urgency or fairness check | 22 | 4 |
| infrastructure (rate limit, 5xx, timeout) | 6 | 1 |

Of the 7 good S1 replies the severity-aware judge did not approve, 1 was judged below the bar and 6 had no trustworthy verdict.

Most evidence failures are on the must-pass **urgency** and **fairness/safety** checks, and they hit the severity-aware judge far more often. It reads the triage context. When that context says urgency is 'none', the check passes trivially, but the rubric still demands a verbatim quote from the draft, and there is nothing in the draft to quote. The baseline does not see the triage context and rarely hits this. Letting a must-pass check that passes trivially skip the quote is the clearest next change. It would need a new evaluation run.

On the 86 items where both systems produced a verdict: severe leak rate ours 17 % (5/30; 95 % CI 7 %-34 %), baseline 33 % (10/30; 95 % CI 19 %-51 %); S1 over-blocking ours 17 % (1/6; 95 % CI 3 %-56 %), baseline 0 % (0/6; 95 % CI 0 %-39 %).

## 2. Catch rate (planted errors not approved)

Overall: ours 91 % (73/80; 95 % CI 83 %-96 %), baseline 74 % (59/80; 95 % CI 63 %-82 %). Caught by code checks before any model call: 21.

| Error type | Ours | Baseline | Caught by code |
|---|---|---|---|
| blame_shifting | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 0 |
| buried_urgency | 83 % (5/6; 95 % CI 44 %-97 %) | 50 % (3/6; 95 % CI 19 %-81 %) | 0 |
| cheerful_in_crisis | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 0 |
| fake_extraordinary | 83 % (5/6; 95 % CI 44 %-97 %) | 83 % (5/6; 95 % CI 44 %-97 %) | 0 |
| generic_apology | 71 % (5/7; 95 % CI 36 %-92 %) | 43 % (3/7; 95 % CI 16 %-75 %) | 0 |
| ignore_repeat_contact | 100 % (6/6; 95 % CI 61 %-100 %) | 67 % (4/6; 95 % CI 30 %-90 %) | 1 |
| missing_care | 86 % (6/7; 95 % CI 49 %-97 %) | 43 % (3/7; 95 % CI 16 %-75 %) | 1 |
| no_next_step | 83 % (5/6; 95 % CI 44 %-97 %) | 50 % (3/6; 95 % CI 19 %-81 %) | 0 |
| partial_answer | 83 % (5/6; 95 % CI 44 %-97 %) | 33 % (2/6; 95 % CI 10 %-70 %) | 2 |
| reask_info | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 0 |
| voucher_push | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 6 |
| wrong_amount | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 6 |
| wrong_regime | 100 % (6/6; 95 % CI 61 %-100 %) | 100 % (6/6; 95 % CI 61 %-100 %) | 5 |

| Dimension | Ours | Baseline |
|---|---|---|
| actionability | 83 % (10/12; 95 % CI 55 %-95 %) | 42 % (5/12; 95 % CI 19 %-68 %) |
| context_awareness | 100 % (12/12; 95 % CI 76 %-100 %) | 83 % (10/12; 95 % CI 55 %-95 %) |
| correctness | 94 % (29/31; 95 % CI 79 %-98 %) | 84 % (26/31; 95 % CI 67 %-93 %) |
| grievance_handling | 85 % (11/13; 95 % CI 58 %-96 %) | 69 % (9/13; 95 % CI 42 %-87 %) |
| relevance | 85 % (11/13; 95 % CI 58 %-96 %) | 38 % (5/13; 95 % CI 18 %-64 %) |
| tone | 89 % (17/19; 95 % CI 69 %-97 %) | 79 % (15/19; 95 % CI 57 %-91 %) |
| urgency | 83 % (5/6; 95 % CI 44 %-97 %) | 50 % (3/6; 95 % CI 19 %-81 %) |

| Tier | Ours | Baseline |
|---|---|---|
| S1 | 96 % (23/24; 95 % CI 80 %-99 %) | 71 % (17/24; 95 % CI 51 %-85 %) |
| S2 | 96 % (23/24; 95 % CI 80 %-99 %) | 83 % (20/24; 95 % CI 64 %-93 %) |
| S3 | 84 % (27/32; 95 % CI 68 %-93 %) | 69 % (22/32; 95 % CI 51 %-82 %) |

**Judge on its own** (correctness errors on S2 items, judge told the code checks passed): 100 % (7/7; 95 % CI 65 %-100 %).

| Error type | Judge-only catch |
|---|---|
| fake_extraordinary | 100 % (2/2; 95 % CI 34 %-100 %) |
| voucher_push | 100 % (2/2; 95 % CI 34 %-100 %) |
| wrong_amount | 100 % (1/1; 95 % CI 21 %-100 %) |
| wrong_regime | 100 % (2/2; 95 % CI 34 %-100 %) |

## 3. Controlled pairs (original beats its planted twin on an affected dimension)

Ours 19 % (11/59; 95 % CI 11 %-30 %); baseline 44 % (26/59; 95 % CI 32 %-57 %). Pairs skipped because a code check caught the twin before any model call: 21. Target for subjective dimensions: ≥ 85 %.

| Error type | Ours | Baseline |
|---|---|---|
| blame_shifting | 50 % (3/6; 95 % CI 19 %-81 %) | 67 % (4/6; 95 % CI 30 %-90 %) |
| buried_urgency | 0 % (0/6; 95 % CI 0 %-39 %) | 17 % (1/6; 95 % CI 3 %-56 %) |
| cheerful_in_crisis | 17 % (1/6; 95 % CI 3 %-56 %) | 50 % (3/6; 95 % CI 19 %-81 %) |
| fake_extraordinary | 33 % (2/6; 95 % CI 10 %-70 %) | 67 % (4/6; 95 % CI 30 %-90 %) |
| generic_apology | 29 % (2/7; 95 % CI 8 %-64 %) | 29 % (2/7; 95 % CI 8 %-64 %) |
| ignore_repeat_contact | 20 % (1/5; 95 % CI 4 %-62 %) | 40 % (2/5; 95 % CI 12 %-77 %) |
| missing_care | 0 % (0/6; 95 % CI 0 %-39 %) | 17 % (1/6; 95 % CI 3 %-56 %) |
| no_next_step | 0 % (0/6; 95 % CI 0 %-39 %) | 17 % (1/6; 95 % CI 3 %-56 %) |
| partial_answer | 25 % (1/4; 95 % CI 5 %-70 %) | 100 % (4/4; 95 % CI 51 %-100 %) |
| reask_info | 17 % (1/6; 95 % CI 3 %-56 %) | 67 % (4/6; 95 % CI 30 %-90 %) |
| wrong_regime | 0 % (0/1; 95 % CI 0 %-79 %) | 0 % (0/1; 95 % CI 0 %-79 %) |

## 4. Requests and latency per tier

| Tier | Ours: requests mean / p95 | Ours: latency p95 ms | Baseline: requests mean / p95 |
|---|---|---|---|
| S1 | 0.44 / 2 | 629735 | 1.06 / 2 |
| S2 | 0.50 / 2 | 451050 | 1.19 / 2 |
| S3 | 1.50 / 3 | 557241 | 0.83 / 2 |
| all | 0.88 / 2 | 557241 | 1.01 / 2 |

## 5. Judge bias

Verbosity padding (two neutral filler sentences, no defect): mean score shift.

| Dimension | Mean shift (padded - original) | n |
|---|---|---|
| actionability | 0.40 | 10 |
| context_awareness | -0.30 | 10 |
| grievance_handling | 0.30 | 10 |
| relevance | 0.10 | 10 |
| tone | 0.40 | 10 |

Verbosity-padded replies still approved: 62 % (10/16; 95 % CI 39 %-82 %).

Politeness padding hiding one planted error (wrong regime or invented extraordinary circumstances): still approved 31 % (5/16; 95 % CI 14 %-56 %).

Model-family swap (judge_a vs judge_b on the same S3 items):

S3 items judged by both panel members: 38; same pass/fail: 68 % (26/38; 95 % CI 53 %-81 %).

| Dimension | Mean judge_a - judge_b | n |
|---|---|---|
| actionability | 0.85 | 20 |
| context_awareness | 0.55 | 20 |
| grievance_handling | 0.50 | 20 |
| relevance | 0.80 | 20 |
| tone | 0.25 | 20 |

## 6. Public human judgments (general domain, not airline validation)

HelpSteer3 preference (validation), judge_a, both orders: accuracy 57 % (17/30; 95 % CI 39 %-73 %); position consistency 77 % (23/30; 95 % CI 59 %-88 %); accuracy on strong preferences (|strength| ≥ 2) 60 % (12/20; 95 % CI 39 %-78 %).

| HelpSteer2 attribute | Spearman (judge vs human) | n |
|---|---|---|
| helpfulness | 0.31 | 29 |
| correctness | 0.40 | 29 |
| coherence | 0.40 | 29 |
| complexity | 0.38 | 29 |
| verbosity | 0.62 | 29 |

## 7. Fairness (counterfactual passengers)

Entitlements across all 17 counterfactual groups: groups with any difference **0** (target 0).

Judged variants: 20 in 4 groups (10 variants not usable as replies). Largest within-group gap in mean scale score: 1.20; groups where one variant was approved and another not: 2.

| Group | Variants | Score gap | Approval differs |
|---|---|---|---|
| SC-0141 | 5 | 1.20 | no |
| SC-0168 | 5 | 0.00 | no |
| SC-0179 | 5 | 0.00 | yes |
| SC-0314 | 5 | 0.20 | yes |

## 8. Triage (held out)

| Scenarios | Exact tier | Recall@S3+ | Under-triage | Over-triage |
|---|---|---|---|---|
| 244 | 86 % (209/244; 95 % CI 81 %-90 %) | 99 % (104/105; 95 % CI 95 %-100 %) | 2 % | 12 % |

## 9. Simulator

| Conversations | Resolved (by the agent, or by a human after handoff) | Mean turns | Handoff rate | Ended by |
|---|---|---|---|---|
| 6 | 100 % (6/6; 95 % CI 61 %-100 %) | 1.8 | 67 % | handoff 4, passenger_done 2 |

## Limitations

- No large-scale human labels for airline replies: ground truth is planted errors (bad by construction) and presumed-good drafts (not human-checked), so S1 over-blocking is an upper bound.
- Planted errors are rule-based edits, cleaner than real model mistakes; catch rates are likely optimistic.
- The public human sets are general-domain; they test the judge model's general calibration, not its airline-domain validity.
- Scenarios and passenger messages are synthetic.
- Free-tier sample sizes are small; the confidence intervals above are wide, and only large differences are detectable.
