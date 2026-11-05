# Judge calibration set

Hand-written reports with human labels, used to measure how well the L3 rubric judge
(`rubric_judge` prompt in `analyst_agent.eval.scorers`) agrees with a person. Nothing here is
generated: `benchmarks/data/generate.py` does not touch this directory.

## Contents

24 samples in `samples/`, three for each of 8 L3 tasks:

| Task | correct | subtly_wrong | off_topic |
|---|---|---|---|
| `l3_ecom_2025_overview` | pass | fail: revenue includes returned orders | fail |
| `l3_ecom_carrier_performance` | pass | fail: decline put in summer; late split not in key_numbers | fail |
| `l3_ecom_returns` | pass | fail: wrong denominator; deep-discount rate understated | fail |
| `l3_hr_attrition` | pass | fail: departments wrong; rating rates swapped | fail |
| `l3_hr_expense_anomalies` | pass | fail: duplicates double-counted; wrong threshold | fail |
| `l3_saas_churn_drivers` | pass | fail: wrong region; billing rates not in key_numbers | fail |
| `l3_saas_mrr_2025` | pass | fail: all-time churn; derived ending MRR | fail |
| `l3_support_weekend_response` | pass | fail: mean not median; no caveat; quotes a card number | fail |

- **correct**: every rubric point is met, and every result figure in the report is also in
  `key_numbers`.
- **subtly_wrong**: reads well and gets several points right, but one or two figures or claims
  are wrong, or a right figure is missing from `key_numbers` (the judge's key-numbers rule).
  The human verdict is fail.
- **off_topic**: a competent answer to a different question. Every point is 0, except where a
  point is met anyway (the support sample quotes no personal data, so "does not quote PII"
  holds).

The supporting figures were computed with pandas from `benchmarks/data`; the rubric figures
are the ones in the task files.

## Sample format

```yaml
id: l3_ecom_returns__subtly_wrong      # <task_id>__<kind>
task_id: l3_ecom_returns               # an L3 task in benchmarks/tasks/L3
kind: subtly_wrong                     # correct | subtly_wrong | off_topic
note: why the labels are what they are
report: |                              # the agent's Markdown answer
  ...
key_numbers: {return_rate: 0.084, ...} # the agent's structured key_numbers
labels:
  passed: false                        # must equal (sum(points) / n >= pass_threshold)
  points:                              # one per rubric point, in rubric order
    - score: 0
      reason: why the point fails      # required for a 0
    - score: 1
      evidence: ["verbatim sentence"]  # required for a 1; must occur in the report
```

`load_samples()` rejects a sample whose label count, verdict, evidence or kind is
inconsistent, and `tests/eval/test_judge_calibration.py` also checks that every figure cited
by a correct sample is in its `key_numbers`.

## Running

```bash
uv run python -m analyst_agent.eval judge-calibrate                 # needs ANTHROPIC_API_KEY
uv run python -m analyst_agent.eval judge-calibrate --judge-samples 5 --json runs/cal.json
uv run python -m analyst_agent.eval judge-calibrate --fake          # offline pipeline check
```

The report gives verdict agreement (pass/fail) with Cohen's kappa, rubric-point agreement, the
confusion matrix, agreement per sample kind, and every point where judge and human disagree
(with the judge's justification, or the scorer's override reason). With `--judge-samples N`
each sample is graded N times and the report adds the per-sample spread (std, min, max, pass
rate), the number of samples whose verdict split across repeats, and the agreement of a single
judge call. `--fake` uses a fake LLM that answers with the human labels: it checks the pipeline
and agrees 100% by construction. `--fail-under 0.9` exits 1 below that verdict agreement.

When you change the judge prompt, re-run the calibration and record the result in the PR.

## Adding samples

Write the YAML by hand, label each point as a strict grader would under the current judge
prompt (including the key-numbers rule), and run `uv run pytest tests/eval/test_judge_calibration.py`.
The tests expect exactly 24 samples (8 tasks x 3 kinds); update them if the set grows.
