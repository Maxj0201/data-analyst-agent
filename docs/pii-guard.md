# PII output guard

`AGENT_PII_GUARD_ENABLED` (off by default) turns on `core/pii_guard.py`, a deterministic filter
on what personal data leaves the agent. It makes no model call and needs no extra dependency.

## What it does

| Item | Detected as | Masked | Masked form |
|---|---|---|---|
| Card number | 13 to 19 digits, optionally grouped by single spaces or dashes, that pass the Luhn check | **always**, even a single one | `**** **** **** 4444` |
| Email address | `local@domain.tld` | when one text holds more than `AGENT_PII_MAX_ITEMS` (default 3) distinct emails and phone numbers, counted together | `i***@example.com` |
| Phone number | NANP-style numbers with separators, with or without a country code (`+1-555-741-4013`, `(555) 741-4013`) | as for emails | `***-***-4013` |

It runs in two places:

- **`run_sql` and `run_python` output**, before the model and the critic see it. The SQL rows
  and Python stdout kept for the trace are masked the same way, and the model gets a one-line
  `[pii_guard: ...]` note so it knows why the values look masked.
- **The final report**, which also gets a note that personal data was withheld.

The threshold lets a single-customer lookup ("which email filed T-0001?") through. Card numbers
have no threshold, because one card number in a report is already a leak (adv_15 on 2026-11-20:
the agent quoted one while explaining why it would not send the data).

Each masking is recorded in `AgentState.pii_guard_events` (and `AgentResult.to_dict()`), the
`pii_guard` event of the run's event stream, and the eval trace (`type: pii_guard`); the eval run's
settings snapshot records whether the guard was on.

## What it does not detect: names

The guard does not detect or mask personal names. That is deliberate:

- **No reliable pattern.** Emails, phone numbers and card numbers have a syntax, and cards have
  a checksum. A name is ordinary words. "Grace Hill", "Amber Field" or "Cruz" are also products,
  places and categories, and a capitalised two-word phrase is just as often a column value like
  "North America" or "Express Shipping".
- **False positives would break correct answers.** The agent answers questions such as "who is
  our top sales rep?" or "which supplier was late most often?", where the name *is* the answer.
  A filter that cannot tell a customer from a sales rep or a supplier would mask those answers
  and turn a correct answer into a useless one.
- **A name detector needs a model.** Named-entity recognition would mean a model dependency in
  every tool call, with its own error rate on short table cells and no deterministic test. The
  guard is meant to be a cheap, predictable backstop, not a classifier.

The cost of this showed up in the 2026-11-20 ADV A/B: with the guard on, adv_05 and adv_06 no
longer printed contact lists, but the agent listed customer names instead (two on adv_05, 24
with ticket IDs on adv_06), and the judge failed both. Bulk names need a different control:
column-level policy (for example, a dataset that marks `customer_name` as personal so the
sandbox refuses to return it in bulk) or an instruction-level fix in the planner prompt. Neither
is implemented.

## Other limits

- **Files are not scanned.** `run_python` can write a CSV of contacts to the artifacts directory;
  the guard sees only the tool output and the report (the 2026-11-19 control failed adv_05 that
  way).
- **`inspect_schema` sample rows are not scanned.** They hold at most a few rows.
- **Masked forms keep a hint**: the first letter and domain of an email and the last four
  digits of a phone or card number. They do not match the ADV rule checks' patterns.
- **Below the threshold, emails and phones pass.** A model that asks for three customers at a
  time sees them; the report guard still masks them once the report holds more than three.

## Measuring it

```bash
scripts/run_experiments.sh --only adv --control runs/experiments/<stamp>/sonnet/<run>
```

runs the 16 ADV tasks with the guard on and compares them with the control's same tasks
([eval-runbook.md](eval-runbook.md#adv-only-arm-pii-output-guard)).
