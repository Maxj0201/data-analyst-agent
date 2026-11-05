# Semantic layer: retrieval tuning

The semantic layer (`AGENT_SEMANTIC_LAYER_ENABLED`) retrieves governed metric definitions from
`benchmarks/metrics/*.yaml` into the planner prompt. This page covers how retrieval is tuned and
measured. Settings are in [configuration.md](configuration.md); the A/B run is in
[eval-runbook.md](eval-runbook.md).

## How a question is matched

1. **Backend score** (`AGENT_SEMANTIC_BACKEND`):
   - `bm25`: Okapi BM25 over each metric's name, label and synonyms (each counted twice) and its
     definition.
   - `embedding`: cosine similarity with `all-MiniLM-L6-v2`.
   - `hybrid`: both, max-normalised per question and mixed as
     `(1 - w) * bm25 + w * embedding`, where `w` is `AGENT_SEMANTIC_HYBRID_WEIGHT`.
2. **Analyst vocabulary** (BM25 only, including BM25's share of `hybrid`): see below.
3. **Table filter**: a metric is kept only if every table it reads is loaded. This keeps SaaS
   churn out of HR questions.
4. **Mention rerank**: when the question names a metric verbatim (its name, a label part, or a
   distinctive synonym of two or more words), the metric's score is multiplied by 1.5. When a
   question names both a composite metric and one of its components, the composite ranks first
   (AOV before net revenue).

## Analyst vocabulary (`benchmarks/metrics/vocabulary.yaml`)

Analysts often word things differently from the metric definitions: "current employees" for
headcount, "still employed" for attrition, "GMV" for net revenue. BM25 only matches shared
tokens, so without help it can pick a metric that merely shares other words with the question.
That happened with "current employees" and *average tenure of current employees*.

The vocabulary is a short curated phrase table:

```yaml
kind: vocabulary
version: 1
concepts:
  - name: headcount
    expands_to: [headcount, workforce]
    phrases: [current employees, active employees, how many employees, staff, personnel, fte]
```

When a question contains one of a concept's `phrases`, or one of its `expands_to` terms, BM25
also scores the `expands_to` tokens the question lacks. Those extra terms count at **half
weight** (`BM25Backend(expansion_weight=0.5)`). Matching uses the retriever's tokenizer
(lower-case, stop words dropped, light plural strip), so a phrase matches only as a run of
consecutive tokens. The shipped concepts are:

| Concept | Adds | Triggered by (examples) |
|---|---|---|
| `headcount` | headcount, workforce | current/active employees, how many employees, staff, personnel, FTE |
| `churn_attrition` | attrition, churn | retention, retained, still employed, employee/staff turnover, leavers, left the company, cancelled subscriptions |
| `revenue` | net revenue, sales, GMV | revenue, GMV, gross merchandise value, top line, turnover, net/total sales |
| `compensation` | salary, pay | compensation, earnings, earn, wage(s), paid, payroll cost |

The domain is not part of the table, because tables already pick it. `churn_attrition`
points at `churn_rate` when `subscriptions` is loaded and at `attrition_rate` when `employees`
is loaded.

Rules for editing it (`tests/semantic/test_vocabulary.py` enforces the first two):

- Phrases are at most three tokens long, and none is a whole benchmark question. Do not copy
  benchmark or paraphrase questions in here. The paraphrase cases in `retrieval_eval.yaml` are
  a held-out check, and the table was not tuned on them.
- Concept names are unique across all vocabulary files.
- Use multi-word phrases for words that mean different things in different domains. For
  example, "turnover" is revenue in retail and attrition in HR, so only "employee turnover" and
  "staff turnover" trigger attrition.
- Within one domain, an expansion should point at a single family of metrics. For example,
  `churn_attrition` does not add "retention", because that would pull net revenue retention into
  every churn question.

The loader finds vocabulary files next to the metric definitions: the same file and directory
rules apply, and `kind: vocabulary` makes the metrics loader skip the file. The wheel bundles the
file with the metrics. A custom `AGENT_SEMANTIC_METRICS_DIR` with no vocabulary file gets no
expansion. `semantic eval --no-vocabulary` shows the ranking without the table.

## Synonym and label changes (metric YAMLs)

| Metric | Change | Why |
|---|---|---|
| `headcount` | dropped `employees` and `number of employees`; added staff, workforce, employee count, people employed, number of staff | "the number of employees terminated" is not a headcount question, and the multi-word synonym made it a verbatim mention |
| `average_tenure` | label "Average tenure of current employees" → "Average tenure" | the label made every question about current employees look like a tenure question, and it blocked "current employees" from being a distinctive phrase |
| `attrition_rate` | added attrition, employee attrition, staff churn, employee retention, retention rate, left the company, people who left, quit rate | retention and leaver phrasings ranked tenure first |
| `termination_rate_by_group` | added attrition by group, cohort attrition, retention by cohort | same |
| `revenue_per_customer` | dropped "customer value" and "customer lifetime revenue" (the caveat itself says it is not lifetime value); added revenue per buyer, average customer spend | the repeated "customer" tokens pulled every question that mentions customers and revenue |

## Results

`uv run python -m analyst_agent.semantic eval -v` on 80 benchmark tasks (33 of them have an
intended metric; the other 47 are listed but not scored) and 10 paraphrases. Measured
2026-11-04.

### BM25

| Configuration | Benchmark top-1 | top-3 | MRR | Paraphrase top-1 | top-3 | MRR |
|---|---|---|---|---|---|---|
| Before (`main` at `8686150`) | 0.848 | 0.909 | 0.896 | 0.500 | 0.900 | 0.708 |
| Tuned YAMLs, no vocabulary, no mention rerank | 0.848 | 0.939 | 0.904 | 0.600 | 0.900 | 0.758 |
| Tuned YAMLs, no vocabulary (`--no-vocabulary`) | 0.909 | 0.939 | 0.934 | 0.600 | 0.900 | 0.758 |
| **Tuned YAMLs + vocabulary (default)** | **1.000** | **1.000** | **1.000** | 0.600 | 0.900 | 0.758 |

Benchmark cases that were wrong before, with the rank of the right metric before → after:
`l1_hr_engineering_headcount` 4 → 1, `l1_hr_remote_current` 4 → 1, `l3_ecom_2025_overview`
4 → 1, `l2_hr_first_year_retention` 2 → 1 and `l3_hr_engagement_attrition` 3 → 1.

Expansion weight. Only 0.5 is shipped; the other values are shown to check that the result does
not hang on the exact number:

| `expansion_weight` | 0 | 0.25 | **0.5** | 0.75 | 1.0 |
|---|---|---|---|---|---|
| Benchmark top-1 | 0.909 | 0.970 | **1.000** | 1.000 | 0.970 |
| Benchmark MRR | 0.934 | 0.985 | **1.000** | 1.000 | 0.985 |

Paraphrases are still the weak spot: 4 of 10 are not ranked first. `para_nrr` ("grow their
spend … net of churn") ranks NRR 4th, and `para_repeat_buyers`, `para_profitability` and
`para_lost_mrr` rank the right metric 2nd or 3rd. These paraphrases deliberately share few
words with the definitions, which is what the embedding backend is for. The table was not
tuned on them.

The regression guard (`tests/semantic/test_retrieval_eval.py`) requires benchmark top-1 ≥ 0.9
and top-3 and MRR ≥ 0.95. It also checks that the tuned YAMLs clear 0.9 without the vocabulary,
and that the vocabulary never lowers MRR.

### Embedding and hybrid: not measured yet

These have not been measured in this environment. The `embeddings` extra needs torch:

- `uv pip install --torch-backend cpu torch` and the PyTorch CPU index
  (`download.pytorch.org/whl/cpu`) both failed: the session proxy refused the CONNECT tunnel.
- `huggingface.co` was refused too (403), so `all-MiniLM-L6-v2` could not be downloaded even
  with torch installed.

PyPI itself is reachable, but its Linux torch wheel is the CUDA build, and it is no use without
the model. `semantic eval` therefore reports `embedding` and `hybrid` as *skipped*.
`AGENT_SEMANTIC_HYBRID_WEIGHT` stays at its default, 0.5, until the sweep below has run.

Run this on a machine that can reach the PyTorch index and Hugging Face:

```bash
uv sync --all-extras --no-extra embeddings
```

```bash
uv pip install --torch-backend cpu torch "sentence-transformers>=3.0"
```

```bash
uv run --no-sync python -m analyst_agent.semantic eval -v --backend bm25 --backend embedding --backend hybrid --cache-dir data/embeddings
```

```bash
for w in 0.2 0.3 0.4 0.5 0.6 0.7 0.8; do echo "== hybrid weight $w"; uv run --no-sync python -m analyst_agent.semantic eval --backend hybrid --hybrid-weight "$w" --cache-dir data/embeddings; done
```

`--no-sync` matters: a plain `uv run` re-syncs the environment to the lock file, which would
replace the CPU torch with the CUDA build or remove the extra.

How to choose the weight: highest benchmark top-1, then benchmark MRR, then paraphrase MRR. If
the winner is not 0.5, change the default of `semantic_hybrid_weight` in
`src/analyst_agent/core/config.py`, its row in `docs/configuration.md` and its line in
`.env.example` (`tests/test_config.py` checks that the three agree). Then fill in this table:

| Backend | Benchmark top-1 | top-3 | MRR | Paraphrase top-1 | top-3 | MRR |
|---|---|---|---|---|---|---|
| bm25 | 1.000 | 1.000 | 1.000 | 0.600 | 0.900 | 0.758 |
| embedding | – | – | – | – | – | – |
| hybrid w=0.2 | – | – | – | – | – | – |
| hybrid w=0.3 | – | – | – | – | – | – |
| hybrid w=0.4 | – | – | – | – | – | – |
| hybrid w=0.5 | – | – | – | – | – | – |
| hybrid w=0.6 | – | – | – | – | – | – |
| hybrid w=0.7 | – | – | – | – | – | – |
| hybrid w=0.8 | – | – | – | – | – | – |
