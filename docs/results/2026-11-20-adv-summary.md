# Experiment results

- manifest: `runs/experiments/adv-20261120-cards/manifest.rescored-control.tsv`
- agent: `core`
- git: `fcfc6cb`; judge: `claude-sonnet-5-5` (strict (result figures must be in key_numbers))
- Accuracy is over scored tasks (ungraded and over-budget tasks are excluded). An ADV task the judge refused to grade is ungraded unless its rule checks fail it (*Judge refusals* counts both). One L3 or ADV task is 6.25 pp of that level, one L1/L2 task 4.2 pp. Differences of one or two tasks are within run-to-run noise.
- Cost is estimated from `core/llm.py`'s price table; `Total $` adds the judge.

## Overview

| Experiment | Arm | Accuracy | L1 | L2 | L3 | ADV | Judge refusals | $/task | Total $ (incl. judge) | p50 latency | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| adv | pii-guard-on | 92.9% | – | – | – | 92.9% | 2 | $0.0373 | $0.623 | 13.1s | ADV tasks only |
| adv | sonnet | 91.0% | 100.0% | 95.8% | 75.0% | 85.7% | 2 | $0.0532 | $4.558 | 14.5s |  |

### PII output guard on vs off (Sonnet, the 16 ADV tasks)

| Arm | Config | Accuracy | Δ acc | L1 | L2 | L3 | ADV | $/task | Cost vs control | p50 / p95 latency | Guard fired (tasks) | PII rule failures |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sonnet (control) | Sonnet, all features off (same 16 tasks) | 85.7% | – | – | – | – | 85.7% | $0.0356 | 1.00× | 13.4s / 39.2s | 0 / 16 | 1 |
| pii-guard-on | PII guard on, ADV tasks only | 92.9% | +7.1 pp | – | – | – | 92.9% | $0.0373 | 1.05× | 13.1s / 36.2s | 5 / 16 | 0 |

_Notes: pii-guard-on: ADV tasks only._

_Total spend across all runs: $5.181._
