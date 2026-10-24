"""Parse a realestate.com.au search-results DOM (dumped via a real browser).

REA blocks bots at the HTTP layer, so the HTML comes from tools/cdp_dump.js
running against a headed Edge instance where the Kasada challenge resolves
normally. The markup is then fully server/JS rendered and safe to regex.

Card shape (verified on live DOM):
    <article class="... residential-card" aria-label="ADDRESS">
      <a href="/property-house-act-suburb-123" class="details-link">
      <h2 class="residential-card__address-heading">ADDRESS</h2>
      <span>$490,000</span>  /  "Offers over $490,000"
      <ul aria-label="House  with 3 bedrooms  2 bathrooms 2 car spaces">
"""

import html as htmllib
import json
import re

from parse_allhomes import postcode_state

PREFIX = r"(?=<article[^>]*residential-card)"
BLOCK = re.compile(PREFIX)
ARIA = re.compile(r'aria-label="([^"]*)"')
LINK = re.compile(r'href="(/property-[^"?#]+-\d+)"')
ADDR = re.compile(r'residential-card__address-heading"><a[^>]*><span[^>]*>([^<]+)</span>')
PRIMARY = re.compile(r'residential-card__primary"\s+aria-label="([^"]*)"')
PRICE = re.compile(r"\$([\d,]{6,9})")
AUCTION = re.compile(r"(Auction|EOI|Expressions of Interest|Contact agent|Price guide)", re.I)

ROMAN_BEDS = re.compile(r"(\d+)\s*bedroom", re.I)
BATHS = re.compile(r"(\d+)\s*bathroom", re.I)
CARS = re.compile(r"(\d+)\s*car\s*space", re.I)
TYPE = re.compile(r"^\s*(House|Townhouse|Apartment|Unit|Villa|Duplex|Terrace|Land|Studio|Retire|BlockOfUnits|SemiDetached|Alpine)", re.I)


def _residence_index(dom: str) -> tuple[dict, dict]:
    """Harvest postcodes from the schema.org Residence graph.

    REA emits one flat object per listing:
        {"@context":"https://schema.org","@type":"Residence",
         "address":{"@type":"PostalAddress","addressLocality":"Ngunnawal",
                    "addressRegion":"ACT","postalCode":"2913",
                    "streetAddress":"3/3 Manity Court","addressCountry":"AU"},
         "name":"3/3 Manity Court"}

    The older `\\{...\\}\\}` regex could never match this (the nested address
    object means there is no `}}` before the object ends), which is why every
    postcode came back empty. Brace-matched scan instead.

    Returns (street -> postcode, suburb -> postcode) where the suburb map keeps
    the first postcode seen — REA only ever lists one postcode per locality.
    """
    by_street, by_suburb = {}, {}
    needle = '{"@context":"https://schema.org","@type":"Residence"'
    pos = dom.find(needle)
    while pos >= 0:
        depth, i, in_str, esc = 0, pos, False, False
        while i < len(dom):
            c = dom[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        try:
            d = json.loads(dom[pos:i + 1])
        except Exception:
            pos = dom.find(needle, pos + 1)
            continue
        a = d.get("address") or {}
        street = (a.get("streetAddress") or "").strip()
        pc = (a.get("postalCode") or "").strip()
        loc = (a.get("addressLocality") or "").strip()
        if street and pc:
            by_street[street] = pc
        if loc and pc:
            by_suburb.setdefault(loc, pc)
        pos = dom.find(needle, pos + 1)
    return by_street, by_suburb


def parse(dom: str) -> list[dict]:
    postcodes, suburb_pc = _residence_index(dom)
    blocks = [b.split("</article>")[0] for b in re.split(PREFIX, dom)[1:]]
    out, seen = [], set()

    for b in blocks:
        link = LINK.search(b)
        addr = ADDR.search(b)
        prim = PRIMARY.search(b)
        if not (link and addr):
            continue
        url = "https://www.realestate.com.au" + link.group(1)
        if url in seen:
            continue
        seen.add(url)

        address = htmllib.unescape(addr.group(1)).strip()
        street = address.split(",")[0].strip()
        suburb = address.split(",")[1].strip() if "," in address else ""

        beds = baths = cars = None
        ptype = ""
        if prim:
            prim_label = htmllib.unescape(prim.group(1))
            m = ROMAN_BEDS.search(prim_label)
            beds = int(m.group(1)) if m else None
            m = BATHS.search(prim_label)
            baths = int(m.group(1)) if m else None
            m = CARS.search(prim_label)
            cars = int(m.group(1)) if m else None
            m = TYPE.match(prim_label)
            ptype = m.group(1) if m else ""

        raw = ""
        price = None
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b))
        m = re.search(
            r"((?:Offers?\s+over|Auction|EOI|Expressions?\s+of\s+Interest|Contact\s+agent|Price\s+guide)?"
            r"\s*\$[\d,]{6,9}\s*[+km]?)", text, re.I)
        if m:
            raw = m.group(1).strip()
            pm = re.search(r"\$([\d,]{6,9})", raw)
            price = int(pm.group(1).replace(",", "")) if pm else None
        else:
            a = AUCTION.search(text)
            raw = a.group(1) if a else ""

        pc = postcodes.get(street) or postcodes.get(address) or suburb_pc.get(suburb, "")
        # "Address available on request" has no streetAddress in the schema graph,
        # so no postcode is available — but the slug always carries the state:
        # /property-house-act-jacka-149320544
        slug_state = re.search(r"-([a-z]{2,3})-[a-z0-9]+-\d+$", link.group(1))
        state = postcode_state(pc) or (
            slug_state.group(1).upper() if slug_state else "")
        out.append({
            "id": link.group(1).rsplit("-", 1)[-1],
            "url": url,
            "address": address,
            "street": street,
            "suburb": suburb,
            "postcode": pc,
            "state": state,
            "beds": beds,
            "baths": baths,
            "parking": cars,
            "price_num": price,
            # REA agents write "$659,000+" as a guide ("from"), unlike allhomes
            # where a price-less listing means auction/negotiation. A guide at or
            # under budget is in scope, so it counts; the "+" stays visible in
            # price_raw so nobody mistakes it for a fixed price.
            "price_high": price,
            "price_raw": raw or (f"${price:,}" if price else ""),
            "prop_type": ptype,
            "source": "rea",
        })
    return out


def parse_dir(dirpath: str) -> tuple[list[dict], int]:
    """Parse every page-N.html written by tools/rea_crawl.js.

    Returns (rows, page_count). Duplicate URLs across pages are collapsed —
    REA occasionally repeats a card in the "featured" slot.
    """
    import glob
    import os

    files = sorted(
        glob.glob(os.path.join(dirpath, "page-*.html")),
        key=lambda p: int(re.search(r"page-(\d+)\.html$", p).group(1)),
    )
    rows, seen = [], set()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for r in parse(fh.read()):
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                rows.append(r)
    return rows, len(files)


if __name__ == "__main__":
    import sys
    dom = open(sys.argv[1], encoding="utf-8").read()
    rows = parse(dom)
    print(f"parsed {len(rows)}")
    for r in rows[:15]:
        print(f"  {str(r['beds']):>2}b{str(r['baths']):>2}b {r['price_raw'][:22]:<24}"
              f"{r['prop_type'][:9]:<11}{r['suburb'][:16]:<17}{r['street'][:28]}")
