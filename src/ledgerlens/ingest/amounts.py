"""Money parsing and sign-convention inference.

Two separate problems live here, and conflating them is how spending reports
end up inverted.

**Parsing** turns a string like ``"(1,234.56)"``, ``"£1.234,56"`` or
``"1234.56 DR"`` into an unsigned magnitude in integer cents. ``Decimal`` is
used throughout; binary floats never touch a currency value.

**Sign inference** decides what a given bank *meant*. Three conventions show up
in real exports:

1. Separate ``Debit`` / ``Credit`` columns holding positive magnitudes.
2. One signed ``Amount`` column, where negative conventionally means money out.
3. One unsigned ``Amount`` column plus a type column (``DR``/``CR``,
   ``Debit``/``Credit``, ``W``/``D``) that carries the direction.

Getting this wrong silently inverts every number the server reports, so the
inferred plan is recorded in the import log with the evidence behind it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Currency symbols and stray characters that appear glued to amounts.
_STRIP_CHARS = re.compile(r"[^\d,.\-()]")
_DEBIT_WORDS = {"dr", "debit", "d", "w", "withdrawal", "payment", "purchase", "out"}
_CREDIT_WORDS = {"cr", "credit", "c", "deposit", "refund", "in"}


class AmountParseError(ValueError):
    """Raised when a cell cannot be read as a monetary amount."""


def parse_magnitude(raw: str) -> tuple[Decimal, bool]:
    """Parse ``raw`` into ``(magnitude, was_marked_negative)``.

    The magnitude returned is always non-negative. ``was_marked_negative`` is
    True when the string itself carried a negative marker — a leading minus, a
    trailing minus, or accounting-style parentheses.

    Thousands/decimal separators are disambiguated by position rather than by
    locale guessing: whichever of ``.`` or ``,`` appears last is the decimal
    separator, which is correct for both ``1,234.56`` and ``1.234,56``.
    """
    if raw is None:
        raise AmountParseError("empty amount")
    text = str(raw).strip()
    if not text:
        raise AmountParseError("empty amount")

    lowered = text.lower()
    # A trailing DR/CR marker is direction, not magnitude; strip it here and let
    # sign inference deal with it from the type column.
    lowered = re.sub(r"\b(dr|cr)\b\.?$", "", lowered).strip()

    negative = False
    if "(" in lowered and ")" in lowered:
        negative = True
    cleaned = _STRIP_CHARS.sub("", lowered)
    cleaned = cleaned.replace("(", "").replace(")", "")
    if cleaned.startswith("-") or cleaned.endswith("-"):
        negative = True
    cleaned = cleaned.replace("-", "")

    if not cleaned:
        raise AmountParseError(f"no digits in amount {raw!r}")

    last_dot, last_comma = cleaned.rfind("."), cleaned.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Both present: the rightmost one is the decimal separator.
        if last_dot > last_comma:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif last_comma >= 0:
        # Only commas. Two trailing digits reads as a decimal comma
        # ("12,50"); three reads as a thousands separator ("1,234").
        tail = len(cleaned) - last_comma - 1
        if tail == 2 and cleaned.count(",") == 1:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex above
        raise AmountParseError(f"cannot parse amount {raw!r}") from exc

    return value, negative


def to_cents(value: Decimal) -> int:
    """Round a Decimal amount to integer cents, half-up."""
    quantized = value.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    return int(quantized * 100)


def classify_type_word(raw: str | None) -> int | None:
    """Map a transaction-type cell to ``-1`` (outflow), ``+1`` (inflow) or None."""
    if not raw:
        return None
    word = str(raw).strip().lower().rstrip(".")
    if word in _DEBIT_WORDS:
        return -1
    if word in _CREDIT_WORDS:
        return 1
    return None


@dataclass
class SignPlan:
    """How to turn a row's amount cells into signed cents."""

    mode: str  # "debit_credit" | "signed" | "typed" | "assume_outflow"
    negative_is_outflow: bool = True
    confidence: float = 1.0
    reason: str = ""

    def describe(self) -> str:
        return f"{self.mode} (confidence {self.confidence:.2f}): {self.reason}"


def infer_sign_plan(
    *,
    has_debit_credit: bool,
    signed_samples: list[tuple[Decimal, bool]] | None = None,
    type_samples: list[str | None] | None = None,
    override: bool | None = None,
) -> SignPlan:
    """Decide how to interpret amount direction for a file.

    ``signed_samples`` is the output of :func:`parse_magnitude` for the amount
    column. ``type_samples`` are the corresponding transaction-type cells, when
    such a column exists.
    """
    if has_debit_credit:
        return SignPlan(
            mode="debit_credit",
            confidence=1.0,
            reason="separate debit and credit columns carry the direction",
        )

    samples = signed_samples or []
    negatives = sum(1 for _, neg in samples if neg)
    positives = len(samples) - negatives

    if override is not None:
        return SignPlan(
            mode="signed",
            negative_is_outflow=override,
            confidence=1.0,
            reason="direction supplied by user override",
        )

    typed = [classify_type_word(t) for t in (type_samples or [])]
    typed_known = [t for t in typed if t is not None]

    if negatives and positives:
        return SignPlan(
            mode="signed",
            negative_is_outflow=True,
            confidence=0.99,
            reason=(
                f"amount column carries both signs ({negatives} negative, "
                f"{positives} positive); negative reads as money out"
            ),
        )

    if typed_known and len(typed_known) >= max(1, len(samples) // 2):
        return SignPlan(
            mode="typed",
            confidence=0.95,
            reason="amounts are unsigned; a transaction-type column carries the direction",
        )

    if negatives and not positives:
        return SignPlan(
            mode="signed",
            negative_is_outflow=True,
            confidence=0.9,
            reason="every amount is negative, consistent with a spend-only export",
        )

    return SignPlan(
        mode="assume_outflow",
        confidence=0.55,
        reason=(
            "amounts are unsigned with no type column; assuming a card-style "
            "spend-only export. Set negative_is_outflow in ledgerlens.yaml if wrong"
        ),
    )


def signed_cents(
    plan: SignPlan,
    *,
    amount_raw: str | None = None,
    debit_raw: str | None = None,
    credit_raw: str | None = None,
    type_raw: str | None = None,
) -> int:
    """Apply ``plan`` to one row's cells, returning signed integer cents."""
    if plan.mode == "debit_credit":
        out = inn = Decimal(0)
        if debit_raw and str(debit_raw).strip():
            out, _ = parse_magnitude(debit_raw)
        if credit_raw and str(credit_raw).strip():
            inn, _ = parse_magnitude(credit_raw)
        return to_cents(inn - out)

    magnitude, marked_negative = parse_magnitude(amount_raw or "")

    if plan.mode == "typed":
        direction = classify_type_word(type_raw)
        if direction is None:
            direction = -1 if marked_negative else 1
        return to_cents(magnitude) * direction

    if plan.mode == "assume_outflow":
        return -to_cents(magnitude)

    is_outflow = marked_negative if plan.negative_is_outflow else not marked_negative
    return -to_cents(magnitude) if is_outflow else to_cents(magnitude)
