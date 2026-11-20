"""PII output guard (``AGENT_PII_GUARD_ENABLED``): keep bulk personal data out of the output.

The agent loop runs :func:`guard_text` over every ``run_sql`` / ``run_python`` result before the
model (and the critic) sees it, and :func:`guard_report` over the final report. Card numbers are
masked wherever they appear: one is already a leak. Email addresses and phone numbers are masked
when one text holds more than ``AGENT_PII_MAX_ITEMS`` distinct ones (counted together), i.e. in
bulk:

- email ``ines.larsen156@example.com`` -> ``i***@example.com``
- phone ``+1-555-741-4013`` -> ``***-***-4013``
- card ``4111 1111 1111 1111`` -> ``**** **** **** 1111`` (only Luhn-valid 13-19 digit runs)

At or below the threshold, emails and phones pass unchanged, so a single-customer lookup still
works. This is an output control, not a guarantee: names are not detected (docs/pii-guard.md says
why), and files that ``run_python`` writes to the artifacts directory are not scanned.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Tools whose output is guarded before it reaches the model.
GUARDED_TOOLS = frozenset({"run_sql", "run_python"})

EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# NANP-style numbers with separators, optionally with a country code: +1-555-741-4013,
# (555) 741-4013, 555.741.4013. Plain digit runs (IDs, amounts) are left alone.
PHONE_RE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
)
# 13-19 digits, optionally grouped by single spaces or dashes; confirmed with the Luhn check.
CARD_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")

KINDS = ("emails", "phones", "cards")
_LABELS = {"emails": "email address", "phones": "phone number", "cards": "card number"}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _cards(text: str) -> list[str]:
    out = []
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            out.append(m.group())
    return out


def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_phone(value: str) -> str:
    return "***-***-" + re.sub(r"\D", "", value)[-4:]


def mask_card(value: str) -> str:
    return "**** **** **** " + re.sub(r"\D", "", value)[-4:]


@dataclass(frozen=True)
class PiiScan:
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return len(self.emails) + len(self.phones) + len(self.cards)

    @property
    def contacts(self) -> int:
        """Distinct emails and phone numbers: what ``AGENT_PII_MAX_ITEMS`` is compared with."""
        return len(self.emails) + len(self.phones)

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in KINDS}


def scan(text: str) -> PiiScan:
    """Distinct email addresses, phone numbers and card numbers in ``text``."""
    cards = _cards(text)
    # A card number never also counts as a phone number.
    no_cards = text
    for c in cards:
        no_cards = no_cards.replace(c, " ")
    return PiiScan(
        emails=tuple(sorted(set(EMAIL_RE.findall(text)))),
        phones=tuple(sorted({re.sub(r"\D", "", p) for p in PHONE_RE.findall(no_cards)})),
        cards=tuple(sorted({re.sub(r"\D", "", c) for c in cards})),
    )


def _card_sub(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group())
    return mask_card(digits) if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group()


def redact_cards(text: str) -> str:
    """Mask every (Luhn-valid) card number in ``text``."""
    return CARD_RE.sub(_card_sub, text)


def redact(text: str) -> str:
    """Mask every email address, phone number and card number in ``text`` (no threshold)."""
    text = redact_cards(text)
    text = EMAIL_RE.sub(lambda m: mask_email(m.group()), text)
    return PHONE_RE.sub(lambda m: mask_phone(m.group()), text)


def redact_obj(value: Any, fn: Callable[[str], str] = redact) -> Any:
    """:func:`redact` applied to every string inside lists, tuples and dicts (e.g. SQL rows)."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [redact_obj(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(v, fn) for v in value)
    if isinstance(value, dict):
        return {k: redact_obj(v, fn) for k, v in value.items()}
    return value


@dataclass(frozen=True)
class GuardResult:
    text: str
    redacted: bool
    counts: dict[str, int]  # distinct items found per kind (emails / phones / cards)
    bulk: bool = False  # emails and phones were masked too (more than max_items of them)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def masked(self) -> dict[str, int]:
        """Distinct items masked per kind: cards always, emails and phones only in bulk."""
        if not self.redacted:
            return {k: 0 for k in KINDS}
        if self.bulk:
            return dict(self.counts)
        return {"emails": 0, "phones": 0, "cards": self.counts.get("cards", 0)}

    def mask(self, value: Any) -> Any:
        """Apply the same masking to another value holding the same data (SQL rows, stdout)."""
        if not self.redacted:
            return value
        return redact_obj(value, redact if self.bulk else redact_cards)


def guard_text(text: str, max_items: int) -> GuardResult:
    """Mask card numbers in ``text``, and every email and phone number too when it holds more
    than ``max_items`` distinct ones."""
    found = scan(text)
    bulk = found.contacts > max_items
    if bulk:
        return GuardResult(redact(text), True, found.counts(), bulk=True)
    if found.cards:
        return GuardResult(redact_cards(text), True, found.counts())
    return GuardResult(text, False, found.counts())


def describe(counts: dict[str, int]) -> str:
    """``{"emails": 39, "phones": 39}`` -> ``39 email addresses and 39 phone numbers``."""
    parts = [
        f"{n} {_LABELS[k]}{'es' if n != 1 and k == 'emails' else 's' if n != 1 else ''}"
        for k in KINDS
        if (n := counts.get(k, 0))
    ]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _reason(masked: dict[str, int], max_items: int) -> str:
    parts = []
    if masked.get("emails") or masked.get("phones"):
        parts.append(f"more than {max_items} distinct email addresses and phone numbers")
    if masked.get("cards"):
        parts.append("card numbers are always masked")
    return "; ".join(parts)


def tool_note(masked: dict[str, int], max_items: int) -> str:
    """Appended to a masked tool result, so the model knows why the values look like that."""
    return (
        f"\n[pii_guard: {describe(masked)} in this output were masked as personal data "
        f"({_reason(masked, max_items)}).]"
    )


def report_note(masked: dict[str, int], max_items: int) -> str:
    return (
        "\n\n> **Note:** personal data was withheld from this report. The PII output guard "
        f"masked {describe(masked)} ({_reason(masked, max_items)}). Use an access-controlled "
        "export for contact or payment details."
    )


def guard_report(report: str, max_items: int) -> GuardResult:
    """:func:`guard_text` for the final report, with :func:`report_note` appended if masked."""
    g = guard_text(report, max_items)
    if not g.redacted:
        return g
    return GuardResult(g.text + report_note(g.masked, max_items), True, g.counts, bulk=g.bulk)
