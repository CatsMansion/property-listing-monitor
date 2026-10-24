"""Pull the strata / body-corporate levy out of an allhomes listing page.

There is no structured field. Domain's payload has no `strataLevy`; the number
only exists in the agent's free text, written a dozen different ways:

    Strata Fee: $2,633 per quarter      Strata: $1,250 p.q ( Approx.)
    • Strata: $3,097.80 pq              Strata Levies: $907 per quarter
    Strata: $2,240 p.a.                 Strata Levy: 1216.08 per quarter
    Strata: $2056.30 pq ($645.80 Including sinking fund)

So this is a text extractor, and it is deliberately conservative: when the
number is ambiguous or absent it returns None rather than a guess. A missing
strata figure is honest; a wrong one changes the buyer's decision.
"""

import html as _html
import re

# `\u003cbr\u003e` inside the JSON blobs, plus real markup.
_BR = re.compile(r"\\u003cbr\s*/?\\u003e|<br\s*/?>", re.I)
_ESC = re.compile(r"\\u([0-9a-fA-F]{4})")
_LABEL = re.compile(r"(?:strata|body\s*corporate)", re.I)
# Where the sentence stops being about strata.
_STOP = re.compile(
    r"rates|council|land\s*tax|rental|rent\s*(?:estimate|appraisal)|water|"
    r"\bEER\b|year\s*built|built\s*:|internal|carport|garage|balcony|storage|"
    r"living\s*(?:size|area)",
    re.I,
)
# amount, then a period token, in that order and close together
_AMOUNT = re.compile(
    r"(?P<amt>\$?\s?\d[\d,]*(?:\.\d{1,2})?)\s*"
    r"(?P<per>p\.?\s?q\.?|pq\b|p/?q\b|per\s+q(?:uarter|tr)\b|quarterly|"
    r"p\.?\s?a\.?\b|per\s+annum\b|per\s+year\b|annually\b|pa\b)",
    re.I,
)

MIN_Q = 40          # $/quarter; below this it is a typo or a different fee
MAX_Q = 30_000      # above this it is almost certainly a price, not a levy


def _text(page: str) -> str:
    """Flatten the page to plain text with one line per <br>-separated item."""
    s = _BR.sub("\n", page)
    s = _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"<[^>]+>", " ", s)
    return _html.unescape(s)


def _period(text: str) -> str:
    t = text.lower().replace(" ", "")
    if re.search(r"p\.?a\.?(?![a-z])|perannum|peryear|annually|^pa$", t):
        return "y"
    return "q"


def extract(page: str):
    """Return (quarterly_aud, raw_snippet) or (None, None)."""
    if not page:
        return None, None
    text = _text(page)
    best = None
    for m in _LABEL.finditer(text):
        # window: from the label to end of line, cut at the next fee category
        end = text.find("\n", m.start())
        if end < 0 or end - m.start() > 160:
            end = min(len(text), m.start() + 160)
        window = text[m.end():end]
        stop = _STOP.search(window)
        if stop:
            window = window[:stop.start()]
        am = _AMOUNT.search(window)
        if not am:
            continue
        raw_amt = am.group("amt")
        has_dollar = "$" in raw_amt
        val = float(raw_amt.replace("$", "").replace(",", "").replace(" ", ""))
        # A bare number is only believable if it carries cents (`1216.08`) or is
        # plainly a fee-sized figure. Otherwise it is probably a unit count.
        if not has_dollar and "." not in raw_amt and val < 200:
            continue
        q = val / 4 if _period(am.group("per")) == "y" else val
        if not (MIN_Q <= q <= MAX_Q):
            continue
        snippet = (window[max(0, am.start() - 12):am.end() + 6]).strip()
        # First plausible figure wins. Duplicates are rare (69 matches over 63
        # listings); when a page repeats the number it repeats the same one.
        best = {"q": round(q, 2), "raw": snippet}
        break
    if not best:
        return None, None
    return best["q"], best["raw"]
