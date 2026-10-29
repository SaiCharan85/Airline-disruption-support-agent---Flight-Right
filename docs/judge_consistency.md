# Judge consistency

Run 2026-10-29: 23 dev replies, each judged 3 times with the cache bypassed (temperature 0). A flip means the repeats did not all agree.

**Final decision flips:** 8/23 replies (35%); unavailable verdicts: 2.

| tier | replies | decision flips |
|---|---|---|
| S1 | 1 | 0 (0%) |
| S2 | 9 | 2 (22%) |
| S3 | 13 | 6 (46%) |

| dimension | flip rate | mean score spread (max - min) |
|---|---|---|
| actionability | 4/23 (17%) | 0.26 |
| context_awareness | 3/23 (13%) | 0.22 |
| correctness | 2/23 (9%) | n/a (check) |
| fairness_safety | 0/23 (0%) | n/a (check) |
| grievance_handling | 5/23 (22%) | 0.39 |
| relevance | 4/23 (17%) | 0.22 |
| tone | 6/23 (26%) | 0.35 |
| urgency | 1/23 (4%) | n/a (check) |

Replies were drafted through the real graph (rules-only triage, placeholder judge); the judge under test is the production `SeverityAwareJudge` (S3 = two-judge panel).
