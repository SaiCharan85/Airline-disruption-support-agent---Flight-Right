# Evaluation protocol (pre-registered)

This document fixes the datasets, metrics, comparisons and decision rules before any final numbers
are computed (SPEC §10). The final run uses the held-out `test` split once. Development and pilots
use `dev`. If the protocol changes after this commit, the change is listed at the bottom with its
reason, and the report says so.

Registered: 2026-10-29.

## 1. Questions

1. **Headline:** does the severity-aware, context-reading judge send fewer bad replies in severe
   cases than a single fixed-strictness judge, without blocking too many good replies in simple
   cases, at an acceptable request cost?
2. Which planted errors does the judge catch, per dimension and per tier?
3. Does the judge prefer the clean reply over its planted twin on the affected dimension?
4. How well does the same judge model agree with human raters on public, general-domain data?
5. Is the judge biased by verbosity, politeness padding or model family?
6. Do counterfactual passengers (same case; different name, English level, tone or loyalty tier)
   get the same entitlements and the same quality of reply?
7. How do the triage, the agent and the simulator perform on the held-out split?

## 2. Systems compared

| System | What it is |
|---|---|
| **Severity-aware (ours)** | Profile from the case's tier (S1–S3; S4 replies are not judged here, they go to a human). It reads the triage context: tier, urgency, frustration, special needs, repeat contacts. S3 uses the two-judge panel (judge_a + judge_b, `combine=min`), and panel disagreement means handoff. |
| **Fixed-strictness baseline** | The same rubric and the same judge_a model. The S2 bar applies to every case, there is no panel, and the judge sees no triage context (severity, urgency, frustration, special needs, repeat-contact lines removed). |

Both systems:
- get the same entitlement block, conversation and code-check results;
- use `airline_v1`, prompt version as recorded, and no reviewer exemplars;
- run with the cache bypassed for the final run.

Code checks run first for both. A failed code check is caught without calling a model, exactly as
in production.

The case's tier for both systems is its **gold tier**, so the judge comparison does not inherit
triage errors. Triage is evaluated separately (section 6).

Models (config at the time of the run; the report records the ones actually used per item):
- judge_a: gemma-4-26b;
- judge_b: qwen3.8-27b;
- agent: gpt-oss-120b.

Judges never share the agent's model family.

## 3. Datasets

**Base replies (test split).**
- Target: 12 S1, 12 S2 and 16 S3 test scenarios, excluding counterfactual variants.
- Each reply is drafted by the real graph: production agent, code checks, rules-only triage,
  placeholder judge.
- Only replies that pass every code check and are not handed off are kept. Some cases are handed
  off by design, because the engine sends cases under unverified rules to review.
- Scenarios are taken per tier in a seeded order (seed 42), drafting until the target number of
  usable replies is reached, or until 3 times the target has been tried.
- The report states how many were drafted and why each dropped one was dropped.
- These are the **presumed-good** replies. They were not human-labelled, so S1 over-blocking is an
  upper bound: some originals may have real flaws a judge is right to flag.

**Planted errors.**
- Each base reply gets 2 of the 13 error types.
- The types are chosen by a seeded draw from those applicable to the reply, balanced so each type
  is used about equally often.
- Each mutated reply carries exactly one known defect and the rubric dimensions that should catch
  it.
- Mutated replies are **bad by construction**.
- After mutation the code checks are re-run with the original check inputs (vouchers issued,
  refund amount, care expected, refund timeline).

**Controlled pairs.**
- Each mutated reply and its original form a pair with a known preferred side (the original).
- Pairs are scored pointwise from the same judgements, so they need no extra calls.

**Public human judgments (general domain, reported separately).** Both are CC-BY-4.0, retrieved
2026-10-29 and recorded in `DATA.md`.
- **HelpSteer3 preference, validation split:**
  - 30 pairs with a non-tied human preference, sampled with seed 42 and restricted to English
    general-domain prompts and responses of up to 2,000 characters each;
  - judged pairwise in both orders.
- **HelpSteer2, validation split:**
  - 30 responses, sampled with seed 42 and stratified by human helpfulness rating;
  - rated by the judge on the same five attributes (0–4).

**Bias probes.** These are built on 16 base replies, 8 S2 and 8 S3.
- *Verbosity padding:* adds neutral filler sentences.
- *Politeness padding with an injected error:* adds warm, polite sentences and also plants one
  error, `wrong_regime` or `fake_extraordinary`.
- *Order swap:* applies to the pairwise public data above.
- *Model-family swap:* judge_a and judge_b scores on the same S3 items, from the panel.

**Counterfactual groups (test).**
- 6 groups, seeded, each with all of its variants.
- Every variant is drafted through the graph and judged by the severity-aware judge.
- Entitlement equality is also checked by the rules engine on all 17 test groups. That check needs
  no model calls.

## 4. Metrics

These are as defined in SPEC §10.2. Each proportion is reported with a Wilson 95 % interval.

| Metric | Computed on |
|---|---|
| Severe leak rate | S3 mutated replies the system **approves** / all S3 mutated replies |
| S1 over-blocking | S1 presumed-good replies the system does **not** approve / all S1 presumed-good replies |
| Catch rate | Mutated replies not approved / mutated replies. Reported per error type, dimension and tier, and split by who caught it (code check, judge). |
| Judge-only catch rate | Same, with code checks bypassed: the judge is told the checks passed. Only for the correctness-type errors that code checks catch, on S2 items. This shows what the judge would catch on its own. |
| Controlled-pair accuracy | Share of pairs where the original scores strictly higher on an affected scale dimension, or passes an affected must-pass dimension the mutated reply fails. Ties count as misses. |
| Public human agreement | HelpSteer3: accuracy against the human preferred side, counting a pair as correct only if both orders agree with the human. Also reported: position-consistency rate. HelpSteer2: Spearman correlation per attribute. |
| Judge bias | Mean score shift (padded minus original) per scale dimension. For politeness padding with an error, also the share still approved. |
| Fairness gap | Within each group: maximum difference in mean scale score and in approval. Entitlement differences, which must be 0. |
| Requests and latency per item | Mean and p95 model requests and judge latency per item, per tier and system. All requests are free-tier; cost $0. |
| Entitlement accuracy | Share of base drafts whose stated amounts and rights pass the code checks against the engine's entitlement. The engine's own legal accuracy is covered by the 37 hand-worked golden cases. |
| Triage recall@S3+ | Hybrid triage on the full test split (section 6) |
| Simulator outcomes | Resolution rate, turns, handoff rate on 6 test scenarios, up to 4 turns each |

## 5. Decision rule (headline)

The severity-aware judge is declared **better** only if all three conditions hold.

1. **Severe leak rate.** Ours is lower than the baseline by at least 5 percentage points, or both
   are at most 1 %.
2. **S1 over-blocking.** Ours is no more than 5 points above the baseline's.
3. **Requests per item.** Ours uses at most 2.0× the baseline's.

Outcomes:
- If condition 1 holds but 2 or 3 fails, the result is reported as a **trade-off**, with the numbers.
- If condition 1 fails, the result is reported as **no improvement**.

A paired exact McNemar test on the S3 mutated items (approved or not, ours vs baseline) is
reported as supporting evidence. With about 32 S3 mutated items the study is small. Only large
effects will be detectable, and the report says so. The sizes are set by the free-tier judge
limit (about 3-4 judge calls a minute), so the final run fits in about 2-3 hours.

Targets from SPEC §10.4 are reported next to each metric. They are not part of the decision rule.

## 6. Other held-out runs

- **Triage:** hybrid triage (rules + LLM, higher tier wins) on all 244 test scenarios. Reports
  recall@S3+, under-triage and over-triage, with the same definitions as `docs/triage_eval.md`.
- **Simulator:** 6 test scenarios, stratified by tier, oracle noise 0.

## 7. Procedure

1. **Pilot on `dev`.** About 10 base replies with all probes, to debug the harness. Pilot numbers
   are not reported as results.
2. Commit this protocol.
3. **Final run on `test`, once.** Every run writes JSONL records under `data/eval/final/`, so an
   interrupted run resumes without re-calling the models.
4. **Fallbacks.** If a judge model hits its free daily limit mid-run, the role falls back down its
   chain as in production. The fallback models are recorded per item, and the report gives the
   share of items judged by fallback models.
5. **Report.** `eval/report.md` is generated from the records. It includes:
   - a run manifest: git commit, config hash, model IDs actually used, ruleset versions, rubric ID,
     prompt version, exemplar-set version (none);
   - all tables and the decision-rule outcome;
   - a limitations section.

## 8. Known limitations (stated in advance)

- There are no large-scale human labels for airline replies. Ground truth comes from planted
  errors (bad by construction) and presumed-good drafts (not human-checked).
- The planted errors are rule-based edits. They are cleaner than real model mistakes, so catch rates
  are likely optimistic.
- The public human sets are general-domain, not airline support. They test the judge model's
  general calibration, not its domain validity.
- Scenarios and passenger messages are synthetic: generated from templates and written by an LLM
  from a different family than the judges.
- Sample sizes are small, because everything runs on free tiers.

## Changes after registration

1. **2026-10-30, before any re-judging: infrastructure failures are judged once more.**
   - *Observed in the final run:*
     - the severity-aware run had no trustworthy verdict on 49 of 120 items, the baseline on 17;
     - 39 of the severity-aware failures and 5 of the baseline's were infrastructure, not the judge:
       Google rate limits (two copies of the run briefly overlapped and shared the quota), provider
       5xx errors, and timeouts while the machine slept.
   - *Change:*
     - those items, and only those, are judged once more for both systems, with
       `scripts/eval_rejudge.py`;
     - content failures (invalid output, evidence quotes not found in the draft) are the judge's own
       behaviour and are kept.
   - *Reporting:*
     - the report gives the as-run numbers, which stay the registered primary result, next to the
       re-judged numbers;
     - it applies the decision rule to both;
     - the original records are not modified.
