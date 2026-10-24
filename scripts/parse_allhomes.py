"""Parse allhomes search result pages into structured listing dicts."""

import html
import re

BASE = "https://www.allhomes.com.au"
LISTING_HREF = re.compile(r'href="(https://www\.allhomes\.com\.au/[^"]+?)"')
# strict thousands grouping: $698,000 / $1,550,000  (rejects allhomes' own "$1,485,00" typo)
PRICE_STRICT = re.compile(r"\$(\d{1,3}(?:,\d{3})+)")
# shorthand: $698k / $1.2m
PRICE_SHORT = re.compile(r"\$(\d+(?:\.\d+)?)\s*([kKmM])")
AUCTION_RE = re.compile(r"Auction\s+(\d{2}/\d{2}/\d{2})")
MIN_PLAUSIBLE = 100_000
NUM_AFTER_TITLE = r"<title>%s</title>.*?<span[^>]*>(\d+)</span>"

PROP_TYPES = (
    "House", "Townhouse", "Unit", "Apartment", "Villa", "Duplex",
    "Terrace", "Semi-detached", "Land", "Acreage", "Studio", "Retire",
)


def _num(article: str, title: str):
    m = re.search(NUM_AFTER_TITLE % title, article, re.S)
    return int(m.group(1)) if m else None


def _price(article: str, anchor: int = -1) -> tuple[int | None, str, int | None]:
    """Return (low, raw display text, high).

    Only the price block that sits immediately above the address link is
    considered. Scanning the whole card is unsafe: project/estate cards list
    several package prices ("land from $110,000", "house & land $550,000") and a
    card-wide minimum picks the wrong one.

    `high` matters: a listing advertised at "$550,000-$2,000,000" is not a
    sub-$700k property just because its floor is.
    """
    region = article[max(0, anchor - 700):anchor] if anchor > 0 else article

    for src in (region, article):
        strict = [x for x in PRICE_STRICT.findall(src)
                  if int(x.replace(",", "")) >= MIN_PLAUSIBLE]
        if strict:
            nums = [int(x.replace(",", "")) for x in strict]
            lo, hi = min(nums), max(nums)
            raw = f"${lo:,}" if lo == hi else f"${lo:,} - ${hi:,}"
            return lo, raw, hi
        short = PRICE_SHORT.search(src)
        if short:
            unit = 1_000 if short.group(2).lower() == "k" else 1_000_000
            v = int(float(short.group(1)) * unit)
            return v, short.group(0), v

    a = AUCTION_RE.search(region) or AUCTION_RE.search(article)
    if a:
        return None, "Auction " + a.group(1), None
    for kw in ("Contact Agent", "Expressions of Interest", "Offers Over", "Price guide not available", "EOI"):
        if kw in region or kw in article:
            return None, kw, None
    return None, "", None


def _prop_type(article: str, url: str = "") -> str:
    m = re.search(r">(" + "|".join(PROP_TYPES) + r")<", article)
    if m:
        return m.group(1)
    # cards for units sometimes omit the label entirely; the slug is unambiguous
    slug = url.rsplit("/", 1)[-1]
    if slug.startswith("unit-"):
        return "Unit"
    return ""


def _address(article: str) -> tuple[str, str, str, str]:
    name = re.search(r'itemProp="name"\s+content="([^"]+)"', article)
    if name:
        full = html.unescape(name.group(1)).strip()
    else:
        d = re.findall(r'class="css-1t18mjv">([^<]+)', article)
        full = html.unescape(d[0] + ", " + re.sub(r"<!-- -->", "", d[1])) if len(d) >= 2 else ""
    street = re.search(r'itemProp="streetAddress">([^<]+)<', article)
    loc = re.search(r'itemProp="addressLocality">([^<]+)<', article)
    pc = re.search(r'itemProp="postalCode">([^<]+)<', article)
    if not (street and loc and pc):
        parts = [p.strip() for p in full.split(",")]
        return full, parts[0] if parts else "", "", ""
    return (full, html.unescape(street.group(1)).strip(),
            html.unescape(loc.group(1)).strip(), html.unescape(pc.group(1)).strip())


# Australian postcode -> state. Ranges are checked in order, first hit wins,
# which is why NSW's 3644-3999 block is listed separately from VIC's 3000-3643.
_POSTCODE_STATE = [
    ("ACT", [(200, 299), (2600, 2618), (2900, 2920)]),
    ("NSW", [(1000, 2599), (2619, 2899), (2921, 2999), (3644, 3999)]),
    ("VIC", [(3000, 3643), (8000, 8999)]),
    ("QLD", [(4000, 4999), (9000, 9999)]),
    ("SA", [(5000, 5999)]),
    ("WA", [(6000, 6999)]),
    ("TAS", [(7000, 7999)]),
    ("NT", [(800, 999)]),
]


def postcode_state(pc: str) -> str:
    """Postcode -> state code, or "" when it is not a real AU postcode.

    Deliberately handles the traps that trip up a naive ACT/NSW split:
    ACT is 2600-2618 and 2900-2920, while 2913 is Franklin ACT but 2620 is
    Queanbeyan NSW, and 3644-3999 belongs to NSW even though it sits inside
    VIC's 3xxx block.
    """
    if not str(pc).strip().isdigit():
        return ""
    n = int(pc)
    for state, ranges in _POSTCODE_STATE:
        for lo, hi in ranges:
            if lo <= n <= hi:
                return state
    return ""


# kept so older call sites keep working
_state_from_postcode = postcode_state


def parse(html: str) -> list[dict]:
    out, seen = [], set()
    for art in re.findall(r"<article.*?</article>", html, re.S):
        href = None
        for h in LISTING_HREF.findall(art):
            clean = h.split("?")[0].split("#")[0]
            # promoted/project cards append ?tid=…, so match the path, not the full URL
            if re.search(r"-(?:act|nsw)-\d{4}$", clean):
                href = clean
                break
        if not href:
            continue

        # A slug like "macnamara-act-2615" carries no street number: it is an
        # estate/project landing page listing land packages, not one dwelling.
        stem = re.sub(r"-(?:act|nsw)-\d{4}$", "", href.rsplit("/", 1)[-1])
        if not re.search(r"\d", stem):
            continue

        lid = re.search(r"listing-details-(\d+)", art)
        key = lid.group(1) if lid else href
        if key in seen:
            continue
        seen.add(key)

        price_num, price_raw, price_high = _price(art, art.find(href))
        full, street, suburb, postcode = _address(art)
        if not full or "no street name" in (street or "").lower():
            continue
        out.append({
            "id": key,
            "url": href,
            "address": full or (street + " " + suburb),
            "street": street,
            "suburb": suburb,
            "postcode": postcode,
            "state": _state_from_postcode(postcode),
            "beds": _num(art, "Bedrooms"),
            "baths": _num(art, "Bathrooms"),
            "parking": _num(art, "Parking"),
            "price_num": price_num,
            "price_high": price_high,
            "price_raw": price_raw,
            "prop_type": _prop_type(art, href),
            "source": "allhomes",
        })
    return out


def matches(lst: dict, max_price: int, min_beds: int, min_baths: int) -> bool:
    """Shape + budget *floor* check: is this worth showing at all?"""
    beds, baths = lst.get("beds"), lst.get("baths")
    if beds is None or baths is None:
        return False
    if beds < min_beds or baths < min_baths:
        return False
    low = lst.get("price_num")
    if low is not None and low > max_price:
        return False
    return True


def within_budget(lst: dict, max_price: int) -> bool:
    """True only when the whole advertised range sits under the cap.

    "$550,000" -> yes. "$550,000 - $620,000" -> yes.
    "$550,000 - $2,000,000" -> no. "$689,000+" -> no: a floor is not a ceiling,
    the property may well sell above budget.
    """
    hi = lst.get("price_high")
    return hi is not None and hi <= max_price
