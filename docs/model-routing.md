# Model routing

Off by default. Turn it on with `AGENT_ROUTING_ENABLED=true`, or per eval run with
`eval run --routing` / `--no-routing`, so you can A/B it.

## Routing

Before the first agent turn, `core/router.py` makes one classifier call to `AGENT_ROUTER_MODEL`
(default `claude-haiku-5-5`, at `AGENT_ROUTER_EFFORT=low` with `AGENT_ROUTER_MAX_TOKENS=1024`).
It sends the question plus a compact schema summary (`table (N rows): col TYPE, ...`). The
summary comes from the dataset, so it is wrapped as untrusted data. The router returns
`{"tier": "L1"|"L2"|"L3", "reason": ...}` through structured outputs.

| Tier | Meaning | Working model |
|---|---|---|
| L1 | single lookup / aggregate | `AGENT_MODEL_L1` (default `AGENT_MODEL`) |
| L2 | multi-step: group/rank, joins, date buckets, careful filters | `AGENT_MODEL_L2` (default `AGENT_MODEL`) |
| L3 | open-ended, multi-part, statistics, Python | `AGENT_MODEL_L3` (default `AGENT_MODEL`) |

If the router call fails, refuses or returns something unparseable, the run uses
`AGENT_ROUTER_FALLBACK_TIER` (default `L3`) and records `source: "fallback"`. The critic is not
routed; it keeps `AGENT_CRITIC_MODEL`.

## Escalation

The loop moves to the next tier's model (L1 → L2 → L3) when either of these happens:

- **`critic_revise`**: the critic says "revise" for the second time in the run. The loop grants
  one extra critic round beyond `AGENT_MAX_CRITIC_ROUNDS`, so the stronger model's revision is
  reviewed once.
- **`tool_error`**: the same tool errors on two consecutive calls of that tool. The stronger
  model takes the next turn, with both errors in its history.

Each escalation also grants one extra turn beyond `AGENT_MAX_TURNS`. That is the "retry the
step once": `max_turns` cannot block the retry. The token budget still applies. At L3, and with
routing off, there is no escalation.

## What gets recorded

- `AgentState.route`, `.escalations`, `.turn_models`, and `ToolCallRecord.model`.
  `AgentResult.to_dict()["routing"]` holds a summary.
- Loop events `route` and `escalation`, which show up in the API's SSE stream and job events.
- `CostTracker.by_model`: calls, tokens and USD per model, plus `usd_by_role["router"]`.
- In eval traces: a `route` event, `escalation` events, and a `model` on each tool call. Each
  result row gets `routing` and `cost_by_model`.
- `eval report` adds a **Routing** section: accuracy and cost by routed tier, by escalation
  (and by trigger), routed tier vs labelled difficulty, and tokens/cost by model.
  `eval compare` adds the escalation rate and accuracy per routed tier.

## LLM wrapper changes

`LLM.create` accepts per-call `effort` and `max_tokens`. The server-side refusal-fallback beta
is now sent only to models that support it (`llm.FALLBACK_MODELS`). Haiku has no server-side
fallback, so the router and any Haiku tier get a plain request.
