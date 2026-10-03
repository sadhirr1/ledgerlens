"""Foreign currency, countries and travel.

A card used abroad produces statement rows that look superficially ordinary and
mean something different. The amount you are billed is in your home currency,
but it is a *converted* figure, and three separate things are hidden behind it:
the amount actually charged in local currency, the rate it was converted at, and
a fee the issuer added for doing so.

Recovering those matters because the interesting questions about a trip cannot
be answered without them — what did Brazil actually cost, how much went on fees,
and was any of it converted at a worse rate than the rest.

**The ambiguity worth knowing about.** Statements identify location with a
trailing code, and two-letter country codes collide badly with US state codes.
``IN`` is India and Indiana. ``CA`` is California and Canada. ``DE`` is Delaware
and Germany; ``PA``, Pennsylvania and Panama. Reading ``MUMBAI IN`` as Indiana is
not a hypothetical — it is what a naive lookup does.

So the codes are split into two sets. Codes that are *not* US states resolve to a
country outright. Ambiguous ones resolve to a country only when the transaction
carries independent evidence of being foreign — a currency that isn't the
account's, or a conversion rate printed beside it. Absent that evidence, the
domestic reading wins, because for most people most charges are domestic.

Currency is the strongest signal of all: a charge in BRL happened in Brazil, and
no amount of descriptor ambiguity changes that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# ISO 4217 codes seen on consumer card statements, with the country they imply.
# The mapping is one-way on purpose: a currency identifies a country, but a
# country does not always identify a currency (the euro zone, notably).
CURRENCY_COUNTRY: dict[str, str] = {
    "BRL": "BR", "MXN": "MX", "INR": "IN", "ARS": "AR", "CLP": "CL",
    "COP": "CO", "PEN": "PE", "UYU": "UY", "CRC": "CR", "GTQ": "GT",
    "DOP": "DO", "JMD": "JM", "CAD": "CA", "GBP": "GB", "JPY": "JP",
    "CNY": "CN", "HKD": "HK", "SGD": "SG", "THB": "TH", "VND": "VN",
    "IDR": "ID", "MYR": "MY", "PHP": "PH", "KRW": "KR", "TWD": "TW",
    "AUD": "AU", "NZD": "NZ", "ZAR": "ZA", "EGP": "EG", "KES": "KE",
    "NGN": "NG", "MAD": "MA", "AED": "AE", "SAR": "SA", "QAR": "QA",
    "ILS": "IL", "TRY": "TR", "RUB": "RU", "UAH": "UA", "PLN": "PL",
    "CZK": "CZ", "HUF": "HU", "RON": "RO", "SEK": "SE", "NOK": "NO",
    "DKK": "DK", "ISK": "IS", "CHF": "CH", "LKR": "LK", "NPR": "NP",
    "PKR": "PK", "BDT": "BD", "TZS": "TZ", "GHS": "GH",
}

# Currency symbols that are unambiguous enough to be worth recognising.
SYMBOL_CURRENCY: dict[str, str] = {
    "R$": "BRL", "₹": "INR", "£": "GBP", "¥": "JPY", "₩": "KRW",
    "₪": "ILS", "₺": "TRY", "฿": "THB", "₱": "PHP", "₫": "VND",
    "Rp": "IDR", "CHF": "CHF", "kr": "SEK",
}

# Currencies with no minor unit — 1250 JPY is 1250 yen, not 12.50.
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "IDR", "HUF", "COP"}

COUNTRY_NAMES: dict[str, str] = {
    "BR": "Brazil", "MX": "Mexico", "IN": "India", "AR": "Argentina",
    "CL": "Chile", "CO": "Colombia", "PE": "Peru", "UY": "Uruguay",
    "CR": "Costa Rica", "GT": "Guatemala", "DO": "Dominican Republic",
    "JM": "Jamaica", "CA": "Canada", "GB": "United Kingdom", "IE": "Ireland",
    "FR": "France", "DE": "Germany", "ES": "Spain", "PT": "Portugal",
    "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "AT": "Austria",
    "CH": "Switzerland", "SE": "Sweden", "NO": "Norway", "DK": "Denmark",
    "FI": "Finland", "IS": "Iceland", "PL": "Poland", "CZ": "Czechia",
    "HU": "Hungary", "RO": "Romania", "GR": "Greece", "TR": "Turkey",
    "RU": "Russia", "UA": "Ukraine", "IL": "Israel", "AE": "United Arab Emirates",
    "SA": "Saudi Arabia", "QA": "Qatar", "EG": "Egypt", "KE": "Kenya",
    "NG": "Nigeria", "ZA": "South Africa", "MA": "Morocco", "TZ": "Tanzania",
    "GH": "Ghana", "JP": "Japan", "CN": "China", "HK": "Hong Kong",
    "TW": "Taiwan", "KR": "South Korea", "SG": "Singapore", "TH": "Thailand",
    "VN": "Vietnam", "ID": "Indonesia", "MY": "Malaysia", "PH": "Philippines",
    "AU": "Australia", "NZ": "New Zealand", "LK": "Sri Lanka", "NP": "Nepal",
    "PK": "Pakistan", "BD": "Bangladesh", "PA": "Panama",
}

# Three-letter forms issuers use, which do not collide with state codes.
ALPHA3_ALPHA2: dict[str, str] = {
    "BRA": "BR", "MEX": "MX", "IND": "IN", "ARG": "AR", "CHL": "CL",
    "COL": "CO", "PER": "PE", "CAN": "CA", "GBR": "GB", "DEU": "DE",
    "FRA": "FR", "ESP": "ES", "PRT": "PT", "ITA": "IT", "NLD": "NL",
    "CHE": "CH", "SWE": "SE", "NOR": "NO", "DNK": "DK", "POL": "PL",
    "TUR": "TR", "ARE": "AE", "ZAF": "ZA", "EGY": "EG", "KEN": "KE",
    "JPN": "JP", "CHN": "CN", "HKG": "HK", "KOR": "KR", "SGP": "SG",
    "THA": "TH", "VNM": "VN", "IDN": "ID", "MYS": "MY", "PHL": "PH",
    "AUS": "AU", "NZL": "NZ", "LKA": "LK", "NPL": "NP", "PAK": "PK",
    "BGD": "BD", "PAN": "PA", "CRI": "CR",
}

NAME_ALPHA2: dict[str, str] = {v.upper(): k for k, v in COUNTRY_NAMES.items()}
NAME_ALPHA2["UK"] = "GB"
NAME_ALPHA2["UNITED KINGDOM"] = "GB"
NAME_ALPHA2["UAE"] = "AE"
NAME_ALPHA2["HOLLAND"] = "NL"

# Codes that are a country to one reader and a US state to another. These need
# corroborating evidence before they are read as foreign.
AMBIGUOUS_CODES = {c for c in COUNTRY_NAMES if c in US_STATES}

# Issuers word this a dozen ways; all of them mean the same charge.
FOREIGN_FEE = re.compile(
    r"foreign\s+(spend\s+)?transaction\s+fee"
    r"|int(?:e?rnationa?l|l)\.?\s+(transaction|purchase|service)\s+fee"
    r"|cross[-\s]?border\s+(fee|transaction)"
    r"|non[-\s]sterling\s+(transaction\s+)?fee"
    r"|currency\s+conversion\s+(fee|charge)"
    r"|foreign\s+(currency\s+)?(fee|charge)"
    r"|\bfx\s+fee\b"
    r"|overseas\s+(transaction\s+)?(fee|charge)",
    re.IGNORECASE,
)

# "125.00 BRL", "BRL 125.00", "1.250,00 MXN", "125.00 Brazilian Real"
_AMOUNT_THEN_CODE = re.compile(
    r"(?<![\w.])(?P<amt>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s*(?P<code>[A-Z]{3})(?![A-Za-z])"
)
_CODE_THEN_AMOUNT = re.compile(
    r"(?<![\w])(?P<code>[A-Z]{3})\s*"
    r"(?P<amt>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\d])"
)
_SYMBOL_AMOUNT = re.compile(
    r"(?P<sym>R\$|₹|£|¥|₩|₪|₺|฿|₱|₫)\s*"
    r"(?P<amt>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
)

# "Exchange Rate 5.141500", "@ 5.1415", "RATE 5.1415"
_RATE = re.compile(
    r"(?:exchange\s+rate|conversion\s+rate|\brate\b|@)\s*[:=]?\s*"
    r"(?P<rate>\d{1,6}(?:\.\d{1,8})?)",
    re.IGNORECASE,
)

# Words that look like a currency code but are not one.
_NOT_A_CURRENCY = {
    "LLC", "LTD", "INC", "COM", "NET", "ORG", "USA", "THE", "AND", "FEE",
    "ATM", "POS", "WEB", "APP", "TEL", "NEW", "SAN", "LOS", "VAT", "GST",
    "PVT", "CAB", "BAR", "SPA", "CAR", "AIR", "SKY", "SUN", "RED", "ONE",
}


@dataclass
class ForeignDetail:
    """What a statement row reveals about where and in what currency it happened."""

    original_amount_cents: int | None = None
    original_currency: str | None = None
    fx_rate: float | None = None
    country: str | None = None
    is_fee: bool = False

    @property
    def is_foreign(self) -> bool:
        return bool(self.original_currency or self.country)


def _to_cents(raw: str, currency: str | None = None) -> int | None:
    """Parse a foreign-formatted number to minor units."""
    text = raw.strip()
    if not text:
        return None
    last_dot, last_comma = text.rfind("."), text.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        if last_dot > last_comma:
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif last_comma >= 0:
        tail = len(text) - last_comma - 1
        text = text.replace(",", "." if tail == 2 else "")
    try:
        value = float(text)
    except ValueError:
        return None
    if currency and currency.upper() in ZERO_DECIMAL:
        # 1250 JPY is 1250 yen; storing it as 125000 would be a 100x error.
        return int(round(value)) * 100
    return int(round(value * 100))


def parse_foreign_amount(text: str) -> tuple[int, str] | None:
    """Find an amount in a non-home currency, as (minor units, ISO code)."""
    if not text:
        return None

    for match in _AMOUNT_THEN_CODE.finditer(text):
        code = match.group("code").upper()
        if code in CURRENCY_COUNTRY and code not in _NOT_A_CURRENCY:
            cents = _to_cents(match.group("amt"), code)
            if cents:
                return cents, code

    for match in _CODE_THEN_AMOUNT.finditer(text):
        code = match.group("code").upper()
        if code in CURRENCY_COUNTRY and code not in _NOT_A_CURRENCY:
            cents = _to_cents(match.group("amt"), code)
            if cents:
                return cents, code

    match = _SYMBOL_AMOUNT.search(text)
    if match:
        code = SYMBOL_CURRENCY.get(match.group("sym"))
        if code:
            cents = _to_cents(match.group("amt"), code)
            if cents:
                return cents, code

    # Amex spells the currency out: "125.00 Brazilian Real".
    spelled = re.search(
        r"(\d[\d.,]*)\s+(brazilian\s+real|mexican\s+peso|indian\s+rupee|"
        r"pound\s+sterling|japanese\s+yen|euro)",
        text,
        re.IGNORECASE,
    )
    if spelled:
        code = {
            "brazilian real": "BRL", "mexican peso": "MXN",
            "indian rupee": "INR", "pound sterling": "GBP",
            "japanese yen": "JPY",
        }.get(re.sub(r"\s+", " ", spelled.group(2).lower()))
        if code:
            cents = _to_cents(spelled.group(1), code)
            if cents:
                return cents, code
    return None


def parse_fx_rate(text: str) -> float | None:
    """Pull a printed conversion rate out of a statement line."""
    if not text:
        return None
    match = _RATE.search(text)
    if not match:
        return None
    try:
        rate = float(match.group("rate"))
    except ValueError:
        return None
    return rate if 0 < rate < 100_000 else None


def detect_country(
    descriptor: str, *, currency: str | None = None, home_currency: str = "USD"
) -> str | None:
    """Infer the country a transaction happened in.

    Currency decides it outright when present. Otherwise the descriptor's
    trailing location code is used, and a code that is also a US state is only
    accepted as foreign when ``currency`` says the transaction was foreign.
    """
    if currency and currency.upper() != home_currency.upper():
        mapped = CURRENCY_COUNTRY.get(currency.upper())
        if mapped:
            return mapped

    if not descriptor:
        return None

    has_foreign_evidence = bool(currency and currency.upper() != home_currency.upper())
    tokens = [t.strip(",.;:()").upper() for t in descriptor.split() if t.strip(",.;:()")]
    if not tokens:
        return None

    # Full country name, possibly two words ("COSTA RICA", "SOUTH AFRICA").
    for size in (3, 2, 1):
        if len(tokens) >= size:
            tail = " ".join(tokens[-size:])
            if tail in NAME_ALPHA2:
                return NAME_ALPHA2[tail]

    last = tokens[-1]
    if last in ALPHA3_ALPHA2:
        return ALPHA3_ALPHA2[last]

    if last in COUNTRY_NAMES:
        if last not in AMBIGUOUS_CODES:
            return last
        # "MUMBAI IN" is India; "INDIANAPOLIS IN" is Indiana. Without a foreign
        # currency on the row there is nothing to tell them apart, and the
        # domestic reading is the safer default.
        if has_foreign_evidence:
            return last
    return None


def is_foreign_fee(descriptor: str) -> bool:
    return bool(descriptor and FOREIGN_FEE.search(descriptor))


def analyse(
    descriptor: str,
    *,
    extra_text: str = "",
    billed_cents: int | None = None,
    home_currency: str = "USD",
) -> ForeignDetail:
    """Extract everything a row says about currency and location.

    ``extra_text`` carries the continuation lines a PDF statement prints beneath
    a charge, which is usually where the original amount and rate live.
    """
    blob = f"{descriptor} {extra_text}".strip()
    detail = ForeignDetail(is_fee=is_foreign_fee(blob))

    parsed = parse_foreign_amount(blob)
    if parsed:
        amount, code = parsed
        if code.upper() != home_currency.upper():
            detail.original_amount_cents = amount
            detail.original_currency = code

    printed_rate = parse_fx_rate(blob)

    # Prefer the rate implied by the two amounts over the one printed. The
    # implied rate is directly comparable across transactions in the same
    # currency, whichever direction the issuer chose to print theirs in.
    if detail.original_amount_cents and billed_cents:
        billed = abs(billed_cents)
        if billed:
            detail.fx_rate = round(detail.original_amount_cents / billed, 6)
    elif printed_rate:
        detail.fx_rate = printed_rate

    detail.country = detect_country(
        descriptor, currency=detail.original_currency, home_currency=home_currency
    )
    return detail


def country_name(code: str | None) -> str:
    if not code:
        return "Unknown"
    return COUNTRY_NAMES.get(code.upper(), code.upper())


def strip_fx_fragments(text: str) -> str:
    """Remove currency and rate fragments so a merchant name survives them.

    A CSV export folds the original amount into the descriptor
    ("RESTAURANTE SABOR SAO PAULO BR 125.00 BRL"), and leaving it in place puts
    the currency into the merchant name, which then fails to group with the same
    merchant on a PDF statement. Only the name passed to normalization is
    trimmed; the raw descriptor is stored untouched, as always.
    """
    if not text:
        return text
    cleaned = _AMOUNT_THEN_CODE.sub(
        lambda m: "" if m.group("code").upper() in CURRENCY_COUNTRY else m.group(0), text
    )
    cleaned = _CODE_THEN_AMOUNT.sub(
        lambda m: "" if m.group("code").upper() in CURRENCY_COUNTRY else m.group(0), cleaned
    )
    cleaned = _SYMBOL_AMOUNT.sub("", cleaned)
    cleaned = _RATE.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -,;")
