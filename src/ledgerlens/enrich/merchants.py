"""Turning bank gibberish into merchant names.

A card network descriptor looks like this::

    SQ *BLUE BOTTLE #4412 OAKLAND CA 05/14
    TST* SWEETGREEN 0184 NEW YORK NY
    AMZN Mktp US*2H4XY9DK3 AMZN.COM/BILL WA

Underneath each is one merchant. Grouping by the raw string produces a report
with forty rows that are all the same coffee shop, so the raw descriptor is
reduced to a stable name before anything aggregates it.

The raw descriptor is always preserved alongside the normalized name — this
step is lossy by design, and source data is never destroyed.
"""

from __future__ import annotations

import re

from ledgerlens.enrich.foreign import ALPHA3_ALPHA2, COUNTRY_NAMES, US_STATES

# Payment processors and transaction-kind noise that prefixes the real name.
_PREFIXES = [
    # Wells-Fargo-style descriptors put the merchant *after* the date, so this
    # fragment has to go before anything else can find the name.
    r"authorized\s+on\s+\d{1,2}[/-]\d{1,2}\s*",
    r"sq\s*\*", r"tst\s*\*", r"sp\s+", r"pp\s*\*", r"paypal\s*\*",
    # Wallet and tap-to-pay prefixes. These nest: a Google Pay tap at a
    # Toast-using restaurant arrives as "GglPay TST* THE CAFE", so two prefixes
    # have to come off before the merchant is reachable — which is why
    # _strip_prefixes loops instead of passing once.
    r"gglpay\s*\*?\s*", r"g(?:oogle)?\s*pay\s*\*?\s*", r"google\s*\*\s*",
    r"apple\s*pay\s*\*?\s*", r"samsung\s*pay\s*\*?\s*",
    r"venmo\s*\*", r"cash\s*app\s*\*", r"zelle\s*\*",
    r"pos\s+debit\s*-?\s*", r"pos\s+purchase\s*-?\s*", r"pos\s+",
    r"debit\s+card\s+purchase\s*-?\s*", r"checkcard\s*\d*\s*",
    r"recurring\s+payment\s*-?\s*", r"ach\s+(debit|credit)\s*-?\s*",
    r"visa\s+purchase\s*-?\s*", r"card\s+purchase\s*-?\s*",
    r"electronic\s+(payment|withdrawal)\s*-?\s*", r"purchase\s+authorized\s+on\s+",
    r"web\s+id\s*:?\s*\d*", r"gsq\s*\*", r"sumup\s*\*", r"izettle\s*\*",
]

_US_STATES = US_STATES

# A descriptor from abroad ends the same way a domestic one does, with a
# location — "SAO PAULO BR" rather than "OAKLAND CA". Both are noise.
_LOCATION_CODES = _US_STATES | set(COUNTRY_NAMES) | set(ALPHA3_ALPHA2)

# First words of common multi-word city names. Used to remove a second city
# token only when it is safe to — see :func:`_strip_location`.
_CITY_PREFIXES = {
    "SAN", "NEW", "LOS", "LAS", "SANTA", "SANTO", "FORT", "FT", "SAINT", "ST",
    "PORT", "WEST", "EAST", "NORTH", "SOUTH", "LAKE", "MOUNT", "MT", "EL",
    # Non-English city names are at least as common once a card leaves home.
    "SAO", "CIUDAD", "PUERTO", "NUEVA", "NUEVO", "VILLA", "NOVA", "BAHIA",
    "KUALA", "HO", "PHNOM", "ABU", "AL", "NEU", "BAD",
}

# "Rio de Janeiro", "Ciudad de Mexico", "Vina del Mar": a connector means the
# word in front of it belongs to the place name too.
_CITY_CONNECTORS = {"DE", "DA", "DO", "DOS", "DAS", "DEL", "DI", "DU"}

# A descriptor reads left to right: merchant name first, machine noise after.
# Rather than nibbling junk off the end — which fails on multi-word cities like
# "SAN JOSE CA", leaving a stray "SAN" behind — find the first token that is
# unmistakably noise and discard everything from there.
_JUNK_TOKEN = re.compile(
    r"""^(?:
          \#\s*\d+                                # #4412
        | [a-z]{0,2}\d{4,}[a-z0-9]*               # 57442890, F1234, 0184
        | x{2,}[\d-]+                             # card mask
        | [\d-]{7,}                               # phone / reference: 800-1234567
        | \d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?      # embedded date
        | (?:store|str|shop)\#?\d*                # store markers
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

_URL_TAIL = re.compile(r"\b(?:www\.)?[a-z0-9-]+\.(?:com|net|org|co|io|uk)(?:/\S*)?", re.I)
_MULTISPACE = re.compile(r"\s+")
_SYMBOL_RUN = re.compile(r"[*#/\\|]+")

# Well-known descriptors whose normalized form is worth pinning. Extending this
# table is the single easiest contribution to the project.
_ALIASES: list[tuple[re.Pattern[str], str]] = [
    # Order matters: the more specific pattern must be tested first, or
    # "AMAZON WEB SERVICES" resolves to plain "Amazon".
    (re.compile(r"^aws\b|amazon web services", re.I), "Amazon Web Services"),
    (re.compile(r"^amzn|^amazon", re.I), "Amazon"),
    (re.compile(r"^wholefds|whole foods", re.I), "Whole Foods"),
    (re.compile(r"^netflix", re.I), "Netflix"),
    (re.compile(r"^spotify", re.I), "Spotify"),
    (re.compile(r"^uber\s*eats|^ubereats", re.I), "Uber Eats"),
    (re.compile(r"^uber(?!\s*eats)", re.I), "Uber"),
    (re.compile(r"^lyft", re.I), "Lyft"),
    (re.compile(r"^dd\s|^doordash", re.I), "DoorDash"),
    (re.compile(r"^gh\s|^grubhub", re.I), "Grubhub"),
    (re.compile(r"^sbux|^starbucks", re.I), "Starbucks"),
    (re.compile(r"^mcdonald", re.I), "McDonald's"),
    (re.compile(r"^tfl\b|transport for london", re.I), "Transport for London"),
    (re.compile(r"^googl?e?\s*\*?\s*(one|storage)", re.I), "Google One"),
    (re.compile(r"^google\s*\*?\s*youtube|^youtubepremium", re.I), "YouTube Premium"),
    (re.compile(r"^apple\.com/bill|^apple\s+services", re.I), "Apple"),
    (re.compile(r"^github", re.I), "GitHub"),
    (re.compile(r"^openai|^chatgpt", re.I), "OpenAI"),
    (re.compile(r"^anthropic|^claude\.ai", re.I), "Anthropic"),
]

# Casing that title-casing would otherwise mangle.
_CASE_FIXES = {
    "att": "AT&T", "at&t": "AT&T", "hm": "H&M", "h&m": "H&M",
    "ikea": "IKEA", "cvs": "CVS", "kfc": "KFC", "bp": "BP",
    "usps": "USPS", "ups": "UPS", "dhl": "DHL", "nyc": "NYC",
    "tj maxx": "TJ Maxx", "7 eleven": "7-Eleven", "7-eleven": "7-Eleven",
}

_LOWER_WORDS = {"of", "and", "the", "de", "la", "for", "on", "at", "in"}


def _strip_prefixes(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for pattern in _PREFIXES:
            new = re.sub(rf"^\s*{pattern}", "", text, flags=re.I)
            if new != text:
                text, changed = new.strip(), True
    return text


def _cut_at_junk(text: str) -> str:
    """Keep only the tokens before the first unmistakably-noise token."""
    tokens = text.split()
    for i, token in enumerate(tokens):
        if _JUNK_TOKEN.match(token):
            return " ".join(tokens[:i])
    return text


def _strip_location(text: str) -> str:
    """Remove a trailing location fragment: ``CITY ST`` or ``CITY COUNTRY``.

    The location code is the anchor. One word comes off for the city, which
    covers most descriptors. Removing words greedily instead is worse — it eats
    "BOTTLE" from "BLUE BOTTLE OAKLAND CA" — so anything beyond the first word
    has to be justified.

    Two things justify it. A recognisable city-name prefix ("SAN" of San Jose,
    "SAO" of Sao Paulo). And a connector: "Rio de Janeiro" and "Ciudad de
    Mexico" are three-word cities, and the word in front of a "de" is part of
    the place, not the merchant. "CAFE DE PARIS LONDON GB" is unaffected,
    because by the time the city is gone the connector is no longer trailing.
    """
    tokens = text.split()
    if len(tokens) < 2 or tokens[-1].upper() not in _LOCATION_CODES:
        return text

    tokens.pop()  # the state or country code
    if len(tokens) > 1 and tokens[-1].isalpha():
        tokens.pop()  # the city, or its last word

    for _ in range(2):
        if len(tokens) <= 1:
            break
        tail = tokens[-1].upper()
        if tail in _CITY_CONNECTORS:
            tokens.pop()
            if len(tokens) > 1 and tokens[-1].isalpha():
                tokens.pop()  # the word the connector binds to
        elif tail in _CITY_PREFIXES:
            tokens.pop()
        else:
            break

    return " ".join(tokens) if tokens else text


def _titlecase(text: str) -> str:
    words = text.split()
    out: list[str] = []
    for i, word in enumerate(words):
        low = word.lower()
        if low in _CASE_FIXES:
            out.append(_CASE_FIXES[low])
        elif low in _LOWER_WORDS and i > 0:
            out.append(low)
        elif "&" in word and len(word) <= 5:
            out.append(word.upper())  # PG&E, AT&T, H&M
        elif "'" in word:
            head, _, tail = word.partition("'")
            out.append(head.capitalize() + "'" + tail.lower())
        else:
            out.append(word.capitalize())
    return " ".join(out)


def normalize_merchant(raw: str) -> str:
    """Reduce a bank descriptor to a stable, human-readable merchant name.

    Returns ``"Unknown"`` when nothing survives normalization, so that callers
    never have to handle an empty merchant.
    """
    if not raw or not str(raw).strip():
        return "Unknown"

    text = _MULTISPACE.sub(" ", str(raw).strip())

    for pattern, canonical in _ALIASES:
        if pattern.search(text):
            return canonical

    text = _strip_prefixes(text)
    text = _URL_TAIL.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()

    # Cut before cleaning symbols: "#182" identifies itself as a store number
    # only while the hash is still attached.
    text = _cut_at_junk(text)
    text = _strip_location(text)

    text = _SYMBOL_RUN.sub(" ", text)
    text = re.sub(r"[^\w&'\- ]+", " ", text)
    text = _MULTISPACE.sub(" ", text).strip()

    if not text:
        return "Unknown"

    for pattern, canonical in _ALIASES:
        if pattern.search(text):
            return canonical

    return _titlecase(text)
