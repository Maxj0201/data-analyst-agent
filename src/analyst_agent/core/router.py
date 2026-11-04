"""Model routing: pick a working model per question, and escalate when a step keeps failing.

Before the agent loop starts, one cheap classifier call (``AGENT_ROUTER_MODEL``, a Haiku-class
model at low effort) labels the question ``L1`` / ``L2`` / ``L3`` from the question text and a
compact schema summary. Each tier maps to a working model (``AGENT_MODEL_L1`` / ``_L2`` / ``_L3``,
each defaulting to ``AGENT_MODEL``).

During the run the loop escalates to the next tier (and retries the failing step once) when:

* the critic asks for a revision for the second time (``critic_revise``), or
* the same tool errors on two consecutive calls (``tool_error``).

Every route and escalation is recorded on ``AgentState`` and emitted as an event; tokens and
cost are tracked per model by :class:`~analyst_agent.core.llm.CostTracker`.

Routing is off unless ``AGENT_ROUTING_ENABLED=true``, so an eval run can A/B it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from analyst_agent.core.config import Settings, get_settings
from analyst_agent.core.llm import LLM, CostTracker
from analyst_agent.core.prompts import ROUTER_SYSTEM
from analyst_agent.tools.base import ToolContext
from analyst_agent.tools.registry import wrap_untrusted

log = logging.getLogger(__name__)


class Tier(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


TIERS: tuple[Tier, ...] = (Tier.L1, Tier.L2, Tier.L3)

EscalationTrigger = Literal["critic_revise", "tool_error"]

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": [t.value for t in TIERS]},
        "reason": {"type": "string"},
    },
    "required": ["tier", "reason"],
    "additionalProperties": False,
}

MAX_SCHEMA_SUMMARY_CHARS = 4000


def next_tier(tier: Tier | str) -> Tier | None:
    """The tier above ``tier``, or ``None`` at the top."""
    i = TIERS.index(Tier(tier))
    return TIERS[i + 1] if i + 1 < len(TIERS) else None


@dataclass
class RouteDecision:
    """The router's choice for one run."""

    tier: Tier
    model: str
    reason: str = ""
    router_model: str | None = None
    # router: the classifier answered; fallback: it failed and router_fallback_tier was used
    source: Literal["router", "fallback"] = "router"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        if d["error"] is None:
            del d["error"]
        return d


@dataclass
class Escalation:
    """One move up a tier during a run."""

    from_tier: Tier
    to_tier: Tier
    from_model: str
    to_model: str
    trigger: EscalationTrigger
    turn: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["from_tier"] = self.from_tier.value
        d["to_tier"] = self.to_tier.value
        return d


def parse_route(text: str) -> tuple[Tier, str]:
    """Parse the router's JSON. Raises ``ValueError`` on anything unusable."""
    candidate = text.strip()
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if m:
        candidate = m.group(0)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"router output is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("router output is not a JSON object")
    tier = str(obj.get("tier", "")).strip().upper()
    if tier not in Tier.__members__:
        raise ValueError(f"invalid tier {obj.get('tier')!r}")
    return Tier(tier), str(obj.get("reason") or "")


def schema_summary(ctx: ToolContext, max_chars: int = MAX_SCHEMA_SUMMARY_CHARS) -> str:
    """Compact ``table(rows): col TYPE, ...`` lines for the loaded tables. Never raises."""
    lines: list[str] = []
    for table in ctx.tables:
        lit = table.replace("'", "''")
        ident = '"' + table.replace('"', '""') + '"'
        try:
            cols = ctx.run_sql(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name = '{lit}' ORDER BY ordinal_position"
            )
            count = ctx.run_sql(f"SELECT COUNT(*) AS n FROM {ident}")
        except Exception as exc:  # noqa: BLE001 - a summary gap must not stop the run
            lines.append(f"{table}: (schema unavailable: {type(exc).__name__})")
            continue
        n = count.rows[0]["n"] if count.ok and count.rows else "?"
        col_list = (
            ", ".join(f"{r['column_name']} {r['data_type']}" for r in cols.rows)
            if cols.ok
            else "(columns unavailable)"
        )
        lines.append(f"{table} ({n} rows): {col_list}")
    out = "\n".join(lines) or "(no tables loaded)"
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [truncated]"
    return out


def build_router_input(question: str, summary: str) -> str:
    return (
        f"## Question\n{question}\n\n"
        "## Loaded tables\n"
        + wrap_untrusted("schema_summary", summary, max_chars=MAX_SCHEMA_SUMMARY_CHARS + 200)
        + '\n\nRespond with JSON: {"tier": "L1"|"L2"|"L3", "reason": "..."}'
    )


class Router:
    """Classifies a question into a tier and maps tiers to working models."""

    def __init__(self, llm: LLM, settings: Settings | None = None) -> None:
        self.llm = llm
        self.settings = settings or get_settings()

    def model_for(self, tier: Tier | str) -> str:
        return self.settings.model_for_tier(Tier(tier).value)

    def classify(
        self, question: str, summary: str, cost: CostTracker | None = None
    ) -> RouteDecision:
        """One router call. Failures fall back to ``router_fallback_tier``; never raises."""
        s = self.settings
        try:
            resp = self.llm.create(
                system=ROUTER_SYSTEM,
                messages=[{"role": "user", "content": build_router_input(question, summary)}],
                output_format={"type": "json_schema", "schema": ROUTER_SCHEMA},
                model=s.router_model,
                role="router",
                cost=cost,
                effort=s.router_effort,
                max_tokens=s.router_max_tokens,
            )
            if resp.stop_reason == "refusal":
                raise ValueError("router declined")
            tier, reason = parse_route(resp.text)
        except Exception as exc:  # noqa: BLE001 - routing must never stop the run
            log.warning("router failed (%s); using %s", exc, s.router_fallback_tier)
            tier = Tier(s.router_fallback_tier)
            return RouteDecision(
                tier=tier,
                model=self.model_for(tier),
                router_model=s.router_model,
                source="fallback",
                error=f"{type(exc).__name__}: {exc}",
            )
        return RouteDecision(
            tier=tier, model=self.model_for(tier), reason=reason, router_model=s.router_model
        )
