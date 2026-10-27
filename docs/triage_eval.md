# Triage evaluation

Split: `dev` (256 synthetic scenarios). Run 2026-10-26. Models: {'groq:openai/gpt-oss-20b': 174, 'google:gemma-4-26b-a4b-it': 82}.

Gold tiers come from the scenario design (`config/triage.yaml`); messages were written by gemini-3.5-flash-lite, triage runs gpt-oss-20b first to avoid self-preference.

| method | n | accuracy | recall@S3+ | under-triage | over-triage |
|---|---|---|---|---|---|
| rules | 256 | 84% | 97% | 11% | 5% |
| llm | 256 | 78% | 95% | 3% | 19% |
| merged | 256 | 77% | 99% | 1% | 22% |

Target: recall@S3+ >= 98 %. Under-triage = predicted tier below gold.

## Merged (production) confusion matrix

| gold \ predicted | S1 | S2 | S3 | S4 | recall |
|---|---|---|---|---|---|
| **S1** | 14 | 26 | 5 | 0 | 31% |
| **S2** | 1 | 96 | 22 | 0 | 81% |
| **S3** | 0 | 1 | 77 | 3 | 95% |
| **S4** | 0 | 0 | 0 | 11 | 100% |
| precision | 93% | 78% | 74% | 79% | |

## Rules only

| gold \ predicted | S1 | S2 | S3 | S4 | recall |
|---|---|---|---|---|---|
| **S1** | 44 | 0 | 1 | 0 | 98% |
| **S2** | 24 | 86 | 9 | 0 | 72% |
| **S3** | 0 | 3 | 75 | 3 | 93% |
| **S4** | 0 | 0 | 0 | 11 | 100% |
| precision | 65% | 97% | 88% | 79% | |

## LLM only

| gold \ predicted | S1 | S2 | S3 | S4 | recall |
|---|---|---|---|---|---|
| **S1** | 14 | 27 | 4 | 0 | 31% |
| **S2** | 1 | 100 | 18 | 0 | 84% |
| **S3** | 0 | 4 | 77 | 0 | 95% |
| **S4** | 0 | 1 | 2 | 8 | 73% |
| precision | 93% | 76% | 76% | 100% | |

Latency per message (incl. cache hits): p50 0.01 s, p95 0.02 s.
