"""Cross-session insight store (``AGENT_MEMORY_ENABLED``).

After a run whose report passed the critic, the loop saves ``(dataset fingerprint, question,
key findings)`` to a SQLite file (``AGENT_MEMORY_DB_PATH``). On later runs over the *same*
dataset (same fingerprint), the most relevant prior insights are retrieved and put into the
first user message as clearly labelled prior context: hints about what was found before, never
evidence. The planner prompt tells the agent to recompute any number it reports.

The fingerprint is derived from the loaded tables through ``ToolContext.run_sql``: table names,
column names and types, row counts, and an order-independent content checksum per table. It
works with both the module-level sandbox and a per-job sandbox, and changes whenever the data
does, so stale insights from an older version of a file are never retrieved.

Thread-safe: every operation opens its own short-lived connection, so the API job pool and the
eval runner can share one database file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from analyst_agent.tools.base import ToolContext

log = logging.getLogger(__name__)

MAX_FINDINGS_CHARS = 600

# --------------------------------------------------------------------------- fingerprint


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def dataset_fingerprint(ctx: ToolContext) -> str:
    """Stable id for the loaded dataset: schema, row counts and a content checksum per table.

    Table order does not matter. Raises ``RuntimeError`` if the schema cannot be read; the
    content checksum is best-effort (omitted if the sandbox rejects it).
    """
    parts: list[str] = []
    for table in sorted(ctx.tables):
        lit = table.replace("'", "''")
        cols = ctx.run_sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{lit}' ORDER BY ordinal_position"
        )
        if not cols.ok:
            raise RuntimeError(cols.error or f"cannot read schema of {table!r}")
        schema = ",".join(f"{r['column_name']}:{r['data_type']}" for r in cols.rows)
        stats = ctx.run_sql(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(hash(t)), 0)::VARCHAR AS h FROM {_q(table)} t"
        )
        if stats.ok and stats.rows:
            n, checksum = stats.rows[0]["n"], stats.rows[0]["h"]
        else:
            count = ctx.run_sql(f"SELECT COUNT(*) AS n FROM {_q(table)}")
            n, checksum = (count.rows[0]["n"] if count.ok and count.rows else "?"), ""
        parts.append(f"{table}|{schema}|{n}|{checksum}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- records


@dataclass
class Insight:
    fingerprint: str
    question: str
    headline: str
    key_numbers: dict[str, Any] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    id: int | None = None
    score: float = 0.0  # retrieval score; 0 when not retrieved

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def headline_from_markdown(markdown: str) -> str:
    """First substantive line of a free-form report (headings, banners and blanks skipped)."""
    for line in markdown.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ">", "|", "```", "![")):
            continue
        return s[:MAX_FINDINGS_CHARS]
    return ""


# --------------------------------------------------------------------------- retrieval

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or per the to was were "
    "what which who why with each our we you me my this that these those than all any show "
    "give tell list".split()
)


def _tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN_RE.findall(text.lower()):
        if t in _STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def _doc_text(i: Insight) -> str:
    # The question carries most of the signal; metric names help ("churn_rate").
    names = " ".join(k.replace("_", " ") for k in i.key_numbers)
    return f"{i.question} {i.question} {i.headline} {names}"


def rank(question: str, insights: list[Insight]) -> list[Insight]:
    """Score insights against ``question`` (TF-IDF overlap); newest first on ties.

    Only insights sharing at least one non-stop-word term are returned.
    """
    q = set(_tokens(question))
    if not q or not insights:
        return []
    docs = [Counter(_tokens(_doc_text(i))) for i in insights]
    n = len(docs)
    df = Counter(t for d in docs for t in set(d))
    scored: list[Insight] = []
    for ins, doc in zip(insights, docs, strict=True):
        length = sum(doc.values()) or 1
        s = sum(
            (doc[t] / length) ** 0.5 * math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            for t in q
            if t in doc
        )
        if s > 0:
            ins.score = round(s, 4)
            scored.append(ins)
    scored.sort(key=lambda i: (i.score, i.created_at), reverse=True)
    return scored


# --------------------------------------------------------------------------- store


class InsightStore:
    """SQLite-backed store of past findings, keyed by dataset fingerprint."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint TEXT NOT NULL,
        question TEXT NOT NULL,
        headline TEXT NOT NULL,
        key_numbers TEXT NOT NULL,
        caveats TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS insights_fingerprint ON insights (fingerprint, created_at);
    """

    def __init__(self, path: Path | str, *, max_per_fingerprint: int = 200) -> None:
        self.path = Path(path)
        self.max_per_fingerprint = max_per_fingerprint
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, insight: Insight) -> int:
        """Save an insight. Re-asking the same question on the same dataset replaces the old one."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM insights WHERE fingerprint = ? AND lower(trim(question)) = ?",
                (insight.fingerprint, insight.question.strip().lower()),
            )
            cur = conn.execute(
                "INSERT INTO insights (fingerprint, question, headline, key_numbers, caveats, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    insight.fingerprint,
                    insight.question.strip(),
                    insight.headline[:MAX_FINDINGS_CHARS],
                    json.dumps(insight.key_numbers, default=str),
                    json.dumps(insight.caveats[:5]),
                    insight.created_at,
                ),
            )
            # Bound growth per dataset: keep the newest N.
            conn.execute(
                "DELETE FROM insights WHERE fingerprint = ? AND id NOT IN (SELECT id FROM "
                "insights WHERE fingerprint = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
                (insight.fingerprint, insight.fingerprint, self.max_per_fingerprint),
            )
            insight.id = int(cur.lastrowid or 0)
            return insight.id

    def all_for(self, fingerprint: str) -> list[Insight]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, fingerprint, question, headline, key_numbers, caveats, created_at "
                "FROM insights WHERE fingerprint = ? ORDER BY created_at DESC, id DESC",
                (fingerprint,),
            ).fetchall()
        return [
            Insight(
                id=r[0],
                fingerprint=r[1],
                question=r[2],
                headline=r[3],
                key_numbers=json.loads(r[4]),
                caveats=json.loads(r[5]),
                created_at=r[6],
            )
            for r in rows
        ]

    def search(self, fingerprint: str, question: str, k: int = 3) -> list[Insight]:
        """Most relevant prior insights for the same dataset; never crosses fingerprints."""
        if k <= 0:
            return []
        return rank(question, self.all_for(fingerprint))[:k]


def insight_from_result(
    fingerprint: str,
    question: str,
    report_markdown: str,
    key_numbers: Mapping[str, Any],
    structured: Mapping[str, Any] | None = None,
) -> Insight | None:
    """Build the insight to save from a finished run; None if there is nothing worth keeping."""
    if structured:
        headline = str(structured.get("headline") or "")
        caveats = [str(c) for c in structured.get("caveats") or []]
    else:
        headline, caveats = headline_from_markdown(report_markdown), []
    if not headline and not key_numbers:
        return None
    return Insight(
        fingerprint=fingerprint,
        question=question,
        headline=headline,
        key_numbers=dict(key_numbers),
        caveats=caveats,
    )
