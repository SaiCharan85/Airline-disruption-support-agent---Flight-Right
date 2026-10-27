# Triage

Every passenger message gets a severity tier (S1–S4, defined in `config/triage.yaml`). The tier sets
how strict the judge is and how fast a human must pick up a handoff, so **missing a severe case is far
worse than over-escalating** one.

## How it works

1. **Rules** (`src/flightright/triage/rules.py`): text cues (urgent medical need, a child alone,
   personal safety, acute crisis, overnight stays, stated deadlines, vulnerability, strong distress)
   plus facts the system already knows: assistance on the booking, earlier contacts from the history
   store, how far away the flight is.
2. **LLM** (`gpt-oss-20b`, fallback Gemma 4; free tiers): a structured tier, urgency, frustration,
   vulnerability flags, short evidence phrases and a confidence.
3. **Merge**: the higher tier wins, flags are combined, the strongest urgency is kept. If the LLM is
   unavailable, the rule result stands.

The scenario messages were written by `gemini-3.5-flash-lite`, so that model is kept out of triage to
avoid a model grading its own writing.

## Results (dev split)

See `docs/triage_eval.md` for the confusion matrices. Merged: recall@S3+ 99 % (target ≥ 98 %),
under-triage 1 %, over-triage 22 %. S4 cases are all caught.

What each half contributes: the rules are the safety net for S4 and rarely over-escalate (5 %);
the LLM reads tone and context the rules cannot; together they miss almost nothing.

## Known limits

- The rules were tuned on the `dev` split; the `test` split is untouched until the final evaluation,
  where these numbers are re-measured.
- S1 vs S2 is the weakest boundary (an advance-notice cancellation can be either). The cost is low:
  both are lenient tiers.
- Over-triage (mostly S2 read as S3) means some replies get the stricter S3 judge and a two-judge
  panel; its cost is measured in the headline experiment.
- The p95 latency in the report reflects free-tier rate-limit waits during a batch run, not
  single-message latency.

## Commands

```
uv run python scripts/eval_triage.py --rules-only      # free
uv run python scripts/eval_triage.py --n 50 --dry-run  # estimate
uv run python scripts/eval_triage.py --report          # dev split -> docs/triage_eval.md
```
