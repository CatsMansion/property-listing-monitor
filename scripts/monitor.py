"""On-market property monitor - allhomes + realestate.com.au.

Crawls both portals, merges duplicates by street+suburb, filters beds/baths/
budget locally, diffs against the previous run, renders a mobile dashboard,
and can emit a shareable long image.

Run from a project directory: outputs land in ./data, ./index.html, ./share.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from parse_allhomes import (parse, matches, within_budget,  # noqa: E402
                            PRICE_STRICT, PRICE_SHORT)
from strata import extract as extract_strata  # noqa: E402

WORK = os.path.abspath(os.environ.get("PLM_WORKDIR") or os.getcwd())
DATA = os.path.join(WORK, "data")
STATE_FILE = os.path.join(DATA, "state.json")
CURRENT_FILE = os.path.join(DATA, "current.json")
HTML_FILE = os.path.join(WORK, "index.html")
SHARE_DIR = os.path.join(WORK, "share")
TEMPLATE = os.path.join(HERE, "template.html")

SEARCH = "https://www.allhomes.com.au/sale/search?price=-{max_price}"
SEARCH_ALL = "https://www.allhomes.com.au/sale/search"
AEST = timezone(timedelta(hours=11))  # AEDT

# REA needs a real headed browser (Kasada), so its crawl is a Node/CDP job.
# The path form is the only filter syntax REA honours: ?minBeds / ?maxPrice are
# ignored, and "-with-2-bathrooms-" is silently dropped from the canonical URL -
# bathrooms are therefore filtered locally, same as allhomes.
REA_TOOL = os.path.join(HERE, "rea_crawl.js")
REA_RAW = os.path.join(DATA, "rea_raw")
REA_BASE = ("https://www.realestate.com.au/buy/with-{beds}-bedrooms-between-0-{cap}"
            "-in-{region}")

# State keys are "street|suburb" rather than per-portal ids, so a property keeps
# its history when it appears on the other portal. Bumping this rebuilds the
# watch list without spamming fake "new" badges.
STATE_VERSION = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

DEFAULTS = {
    "title": "Property monitor",
    "max_price": 700000,
    # Crawl above budget, filter locally.
    #
    # allhomes ranks price-less listings (auctions, "by negotiation") by an
    # internal estimate and applies the price filter to that estimate. Verified on
    # 105 Petterd St, Page (4b2b auction): absent from ?price=-900000, present with
    # no filter and at ?price=-1500000. So a tight cap drops auctions that could
    # sell under budget - but no cap at all pulls in 400+ unverifiable auctions.
    # 1.3x budget keeps everything plausibly reachable.
    "search_cap": None,          # None -> max_price * 1.3
    "min_beds": 3,
    "min_baths": 2,
    "states": [],                # [] = every state; e.g. ["ACT", "NSW"]
    "range_tolerance": 0.15,
    "rea": {
        "enabled": True,
        # Path slug from realestate.com.au: filter on the site, then copy the
        # "-in-..." segment. Plain text also works, see region_slug().
        "region": "canberra+-+greater+region%2C+act",
        "max_pages": 25,
    },
}

CONFIG = dict(DEFAULTS)


def region_slug(text):
    """Turn "canberra - greater region, act" into REA's path segment."""
    return text.strip().replace(",", "%2C").replace(" ", "+")


def load_config():
    """config.json next to the working directory, then CLI flags on top."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    path = os.path.join(WORK, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "rea" and isinstance(v, dict):
                cfg["rea"].update(v)
            else:
                cfg[k] = v

    ap = argparse.ArgumentParser(description="Property listing monitor")
    ap.add_argument("--max-price", type=int)
    ap.add_argument("--min-beds", type=int)
    ap.add_argument("--min-baths", type=int)
    ap.add_argument("--cap", type=int, help="search cap; 0 = no price filter")
    ap.add_argument("--states", help='comma separated, e.g. "ACT,NSW"')
    ap.add_argument("--title")
    ap.add_argument("--region", help="REA region slug or plain text")
    ap.add_argument("--pages", type=int, help="max REA pages")
    ap.add_argument("--no-rea", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    a = ap.parse_args()

    if a.max_price:
        cfg["max_price"] = a.max_price
    if a.min_beds:
        cfg["min_beds"] = a.min_beds
    if a.min_baths:
        cfg["min_baths"] = a.min_baths
    if a.cap is not None:
        cfg["search_cap"] = a.cap or None
    if a.states is not None:
        cfg["states"] = [x.strip().upper() for x in a.states.split(",") if x.strip()]
    if a.title:
        cfg["title"] = a.title
    if a.region:
        cfg["rea"]["region"] = region_slug(a.region)
    if a.pages:
        cfg["rea"]["max_pages"] = a.pages
    if a.no_rea:
        cfg["rea"]["enabled"] = False
    if cfg["search_cap"] is None:
        cfg["search_cap"] = int(cfg["max_price"] * 1.3)

    cfg["_args"] = a
    return cfg




CONFIG = load_config()


def log(msg):
    print(msg, flush=True)


def fetch(url, tries=3):
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            if r.status_code == 200:
                return r.text
            log(f"  ! HTTP {r.status_code} (try {attempt}/{tries}) {url[-40:]}")
        except Exception as e:
            log(f"  ! {type(e).__name__} (try {attempt}/{tries}) {url[-40:]}")
        if attempt < tries:
            time.sleep(1.5 * attempt + random.random())
    return None


def crawl(max_price: int | None) -> tuple[list[dict], dict]:
    base = SEARCH.format(max_price=max_price) if max_price else SEARCH_ALL
    first = fetch(base)
    if not first:
        raise RuntimeError("首页抓取失败，站点可能已变更或临时封锁")

    m = re.search(r'"resultsPages":"(\d+)"', first)
    pages = int(m.group(1)) if m else 1
    total = re.search(r"(\d[\d,]{0,6})\s*properties", first)
    total = total.group(1) if total else "?"

    scope = f"≤${max_price:,}" if max_price else "全量（无价格上限）"
    log(f"站点报告 {total} 套在售（{scope}），共 {pages} 页")

    rows = parse(first)
    if pages > 1:
        sep = "&" if "?" in base else "?"
        urls = [f"{base}{sep}page={p}" for p in range(2, pages + 1)]
        with ThreadPoolExecutor(max_workers=4) as ex:
            for i, html in enumerate(ex.map(fetch, urls), start=2):
                if html:
                    rows.extend(parse(html))
                else:
                    log(f"  ! 第 {i} 页抓取失败，已跳过")
    return rows, {"total_on_site": total, "pages": pages}


def crawl_rea() -> tuple[list[dict], bool]:
    """Drive the headed-browser crawler, then parse the dumped DOM.

    Returns (rows, ok). A failure here must never abort the run — the allhomes
    half is still valid, and the caller suppresses "dropped" alerts when a
    source is missing so an REA outage doesn't look like 40 sales.
    """
    if not os.path.exists(REA_TOOL):
        log("  ! 未找到 tools/rea_crawl.js，跳过 REA")
        return [], False

    url = REA_BASE.format(beds=CONFIG["min_beds"], cap=CONFIG["max_price"],
                          region=CONFIG["rea"]["region"])
    for old in os.listdir(REA_RAW) if os.path.isdir(REA_RAW) else []:
        if old.startswith("page-") and old.endswith(".html"):
            os.remove(os.path.join(REA_RAW, old))

    try:
        proc = subprocess.run(
            ["node", REA_TOOL, REA_RAW, url,
             str(CONFIG["rea"].get("max_pages", 25)), "25000"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception as e:
        log(f"  ! REA 爬虫异常：{type(e).__name__}: {e}")
        return [], False

    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-3:]
    for ln in tail:
        log("  " + ln.strip())

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown"]
        log(f"  ! REA 爬虫退出码 {proc.returncode}：{err[0][:160]}")
        return [], False

    import parse_rea
    rows, pages = parse_rea.parse_dir(REA_RAW)
    if not rows:
        log("  ! REA 未解析出房源")
        return [], False
    log(f"  REA 解析到 {len(rows)} 条（{pages} 页）")
    return rows, True


def _key(r: dict) -> str:
    """Cross-source identity: street + suburb, punctuation and case folded.

    allhomes and REA list the same dwelling under different ids and different
    price labels ("1/23 Gilmore Place" is "$689,000 - $729,000" on one and
    "$689,000" on the other), so ids cannot be used to merge them.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(r.get('street'))}|{norm(r.get('suburb'))}"


def merge_sources(rows: list[dict]) -> list[dict]:
    """Collapse duplicates across portals into one record per dwelling.

    The record that can prove its price wins; if it has no price at all, the
    other portal's price is adopted (that is real information, not a guess).
    Both URLs are kept so the dashboard can link to either.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_key(r), []).append(r)

    out = []
    for key, group in groups.items():
        if key == "|":  # unparseable address; keep as-is rather than merge blindly
            out.extend(group)
            continue

        def rank(r):
            hi = r.get("price_high")
            return (
                0 if (hi is not None and hi <= CONFIG["max_price"]) else 1,
                0 if r.get("price_num") is not None else 1,
                len(r.get("price_raw") or ""),
            )

        group.sort(key=rank)
        best = dict(group[0])
        best["id"] = "k:" + key
        best["sources"] = sorted({r.get("source", "?") for r in group})
        best["urls"] = [{"source": r.get("source", "?"), "url": r["url"]} for r in group]

        if best.get("price_num") is None:
            for r in group[1:]:
                if r.get("price_num") is not None:
                    best["price_num"] = r["price_num"]
                    best["price_high"] = r.get("price_high")
                    best["price_raw"] = r.get("price_raw")
                    best["price_from"] = r.get("source")
                    break
        out.append(best)
    return out


def dedupe(rows):
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


def classify(rows):
    """Split matches into "price known" and "price unknown" buckets.

    Three price kinds are distinguished on each row via r["bucket"]:
      fixed  — a real ceiling at or under budget ("$689,000", "$650k - $695k")
      guide  — a floor ("$659,000+", "from $499,000"): in scope, but the vendor
               will want at least that much and may want more
      unknown — auction / by negotiation / no price at all
    """
    confirmed, unknown = [], []
    ceiling = CONFIG["max_price"] * (1 + CONFIG["range_tolerance"])
    for r in rows:
        # Development landing pages ("Mona - MONA - Inspired Living in ...") are
        # not dwellings; they slip past the slug check because their slug does
        # contain digits. Their prices are "from" prices for a whole estate.
        if re.search(r"\s[-–]\s", r.get("address") or ""):
            continue
        if not matches(r, CONFIG["max_price"], CONFIG["min_beds"], CONFIG["min_baths"]):
            continue
        if CONFIG["states"] and r["state"] not in CONFIG["states"]:
            continue
        hi = r.get("price_high")
        if hi is not None and hi > ceiling:
            # e.g. "$550,000 - $2,000,000" — not a realistic sub-$700k buy
            continue
        if within_budget(r, CONFIG["max_price"]):
            r["bucket"] = "guide" if (r.get("price_raw") or "").rstrip().endswith("+") else "fixed"
            confirmed.append(r)
        else:
            r["bucket"] = "unknown"
            unknown.append(r)
    key = lambda x: (x["price_num"] or 9e9, x["street"])
    return sorted(confirmed, key=key), sorted(unknown, key=lambda x: x["street"])


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"listings": {}, "runs": 0}


DETAIL_CACHE_FILE = os.path.join(DATA, "detail_cache.json")
ESTIMATE_CACHE_FILE = os.path.join(DATA, "estimate_cache.json")
STRATA_CACHE_FILE = os.path.join(DATA, "strata_cache.json")

# allhomes/Domain ship an automated valuation on the detail page:
#   "priceEstimate":{"__typename":"PriceEstimate","lower":770000,
#                    "medium":900000,"upper":1030000,"confidence":"HIGH",
#                    "lastUpdated":"2026-10-17"}
# Flat object, no nesting, so a single regex is enough. The separate
# "priceGuide" field (what the *agent* is willing to say) was null on 52/52
# auction/negotiation listings, with the page itself saying so.
ESTIMATE_RE = re.compile(
    r'"priceEstimate":\{"__typename":"PriceEstimate","lower":(\d+),"medium":(\d+),'
    r'"upper":(\d+),"confidence":"(\w+)"')
ESTIMATE_TTL = 7 * 86400  # an AVM drifts; don't trust a month-old number
STRATA_TTL = 30 * 86400   # a levy is struck once a year; the ad copy is stable


def load_cache():
    if os.path.exists(DETAIL_CACHE_FILE):
        with open(DETAIL_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _tidy_price(label: str) -> str:
    """Detail pages are inconsistent: "$689000+", "$ 589,000+", "$699,000+",
    and JSON-escaped slashes like "30\\u002F10\\u002F2026"."""
    if not label:
        return ""
    if "\\u" in label:
        try:
            label = json.loads('"' + label.replace('"', '\\"') + '"')
        except Exception:
            label = label.replace("\\u002F", "/")
    label = label.strip()
    m = re.fullmatch(r"\$\s*([\d,]{4,9})\s*(\+?)", label)
    if m:
        return f"${int(m.group(1).replace(',', '')):,}{m.group(2)}"
    return label


def _parse_label(label: str):
    """Turn a detail-page price string back into (low, high).

    Returns (None, None) for non-numeric labels ("By negotiation", auction dates).
    A trailing "+" is a floor, so high stays None — the property is *at least*
    that much and cannot be confirmed as within budget.
    """
    if not label:
        return None, None
    nums = [int(x.replace(",", "")) for x in PRICE_STRICT.findall(label)]
    if nums:
        lo, hi = min(nums), max(nums)
        if lo == hi:
            return lo, (None if label.rstrip().endswith("+") else lo)
        return lo, hi
    m = PRICE_SHORT.search(label)
    if m:
        unit = 1_000 if m.group(2).lower() == "k" else 1_000_000
        v = int(float(m.group(1)) * unit)
        return v, (None if label.rstrip().endswith("+") else v)
    return None, None


def detail_price_label(html: str) -> str:
    """The listing's own price string. Verified: the first `"price"` field in the
    page state always belongs to the primary listing; later ones are comparable
    properties shown further down."""
    m = re.search(r'"price"\s*:\s*"([^"]{0,48})"', html)
    if m:
        return _tidy_price(m.group(1))
    m = re.search(r'"price"\s*:\s*(\d{4,9})', html)
    return f"${int(m.group(1)):,}" if m else ""


def _apply_label(r, label):
    """Write an enriched price label back onto the record, numbers included.

    Without this the label is cosmetic: a card with no price whose detail page
    says "$829,900+" would otherwise sit in the 'unverified' bucket looking like
    a possible sub-$700k property.
    """
    r["price_raw"] = label
    lo, hi = _parse_label(label)
    if lo is not None:
        r["price_num"], r["price_high"] = lo, hi


def enrich_unknown(candidates, cache):
    """Resolve a real label for listings whose card showed no price at all.
    Auction dates are left alone — they're already informative."""
    blank = [r for r in candidates if not r["price_raw"]]
    todo = [r for r in blank if r["id"] not in cache]
    for r in blank:
        if cache.get(r["id"]):
            _apply_label(r, cache[r["id"]])
    if not todo:
        return 0
    log(f"补取 {len(todo)} 套无价房源的详情页…")
    n = 0
    for r in todo:
        html = fetch(r["url"], tries=2)
        label = detail_price_label(html) if html else ""
        cache[r["id"]] = label
        if label:
            _apply_label(r, label)
            n += 1
        time.sleep(0.25)
    with open(DETAIL_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    return n


def _allhomes_url(r):
    """The allhomes URL for a merged row, whichever portal won the merge."""
    for u in r.get("urls") or []:
        if u.get("source") == "allhomes":
            return u["url"]
    return r["url"] if r.get("source") == "allhomes" else None


def load_estimate_cache():
    if os.path.exists(ESTIMATE_CACHE_FILE):
        with open(ESTIMATE_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def enrich_estimates(rows, cache) -> int:
    """Attach allhomes' automated valuation to the price-less listings.

    Only the "unknown" bucket needs this: auctions and by-negotiation listings
    publish no price at all, and 52/52 of them had priceGuide == null. The AVM
    is the only number available, and it is the site's own model — labelled as
    such in the dashboard, never presented as the vendor's expectation.
    """
    now = time.time()
    todo = [r for r in rows
            if not (cache.get(r["id"]) and now - cache[r["id"]].get("at", 0) < ESTIMATE_TTL)]
    for r in rows:
        c = cache.get(r["id"])
        if c and c.get("lo"):
            r["est"] = c
    if not todo:
        return 0

    log(f"取 {len(todo)} 套待核实房源的站点估值…")
    n = 0
    for r in todo:
        url = _allhomes_url(r)
        if not url:
            cache[r["id"]] = {"at": now}  # REA-only: no AVM exists there
            continue
        html = fetch(url, tries=2)
        m = ESTIMATE_RE.search(html) if html else None
        if m:
            entry = {"lo": int(m.group(1)), "med": int(m.group(2)),
                     "hi": int(m.group(3)), "conf": m.group(4), "at": now}
            r["est"] = entry
            n += 1
        else:
            entry = {"at": now}
        cache[r["id"]] = entry
        time.sleep(0.2)

    with open(ESTIMATE_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    return n


def load_strata_cache():
    if os.path.exists(STRATA_CACHE_FILE):
        with open(STRATA_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def enrich_strata(rows, cache) -> int:
    """Read the strata levy out of each allhomes detail page.

    There is no structured field for this - the number only exists in the
    agent's prose (see scripts/strata.py). Rows whose ad copy states no figure
    are cached as misses so the next run doesn't re-fetch them, and they show
    a blank cell rather than a zero.
    """
    now = time.time()
    urls = {}
    for r in rows:
        c = cache.get(_allhomes_url(r) or "")
        if c and now - c.get("at", 0) < STRATA_TTL:
            if c.get("q"):
                r["strata"] = {"q": c["q"], "raw": c.get("raw", "")}
        else:
            u = _allhomes_url(r)
            if u:
                urls[u] = r

    if not urls:
        return 0
    log(f"从 {len(urls)} 个详情页读 strata…")
    n = 0
    for url, r in urls.items():
        html = fetch(url, tries=2)
        q, raw = extract_strata(html) if html else (None, None)
        cache[url] = {"q": q, "raw": raw, "at": now}
        if q:
            r["strata"] = {"q": q, "raw": raw}
            n += 1
        time.sleep(0.2)
    with open(STRATA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    return n


def strata_ratio(r):
    """Annual levy as a percentage of price, plus whether it is an upper bound.

    The denominator is only ever a number the vendor published. Auctions have no
    price at all, so they get no ratio - dividing by an AVM would dress a guess
    up as a measurement. A "from $659,000" guide gives a floor, so the ratio is
    an upper bound and is rendered with a "<=" prefix.
    """
    q = (r.get("strata") or {}).get("q")
    lo, hi = r.get("price_num"), r.get("price_high")
    # price_num, not bucket: a bucket of "unknown" means "not provably within
    # budget", which includes listings with a perfectly good range price.
    if not q or not lo:
        return None, False
    mid = (lo + hi) / 2 if hi and hi > lo else lo
    pct = q * 4 / mid * 100
    # 0.02% is a $500/yr levy on a $2.5m house; 10% is beyond any real scheme.
    if not (0.02 <= pct <= 10):
        return None, False
    bound = (r.get("price_raw") or "").rstrip().endswith("+")
    return round(pct, 2), bound


def diff(confirmed, unknown, state, carry_missing=False):
    """Update state in place; return change summary."""
    now = datetime.now(AEST).isoformat(timespec="seconds")
    known = state["listings"]
    new_ids, dropped_ids, price_changes = [], [], []
    live = {}

    for r in confirmed:
        prev = known.get(r["id"])
        entry = prev or {"first_seen": now, "price_history": []}
        entry.update({
            "last_seen": now,
            "address": r["address"],
            "suburb": r["suburb"],
            "state": r["state"],
            "price_num": r["price_num"],
            "price_high": r.get("price_high"),
            "price_raw": r["price_raw"],
            "beds": r["beds"],
            "baths": r["baths"],
            "parking": r["parking"],
            "prop_type": r["prop_type"],
            "url": r["url"],
            "bucket": r["bucket"],
        })
        hist = entry.setdefault("price_history", [])
        if prev is None:
            new_ids.append(r["id"])
        elif prev.get("price_num") != r["price_num"]:
            old = prev.get("price_num")
            if old and r["price_num"] and r["price_num"] < old:
                price_changes.append((r, old))
            hist.append({"at": now, "from": old, "to": r["price_num"]})
        live[r["id"]] = entry

    for r in unknown:
        prev = known.get(r["id"])
        entry = prev or {"first_seen": now, "price_history": []}
        entry.update({
            "last_seen": now,
            "address": r["address"],
            "suburb": r["suburb"],
            "state": r["state"],
            "price_num": None,
            "price_raw": r["price_raw"],
            "beds": r["beds"],
            "baths": r["baths"],
            "parking": r["parking"],
            "prop_type": r["prop_type"],
            "url": r["url"],
            "bucket": r["bucket"],
        })
        if prev is None:
            new_ids.append(r["id"])
        live[r["id"]] = entry

    for lid, entry in known.items():
        if lid not in live:
            if carry_missing:
                # A source is missing this run, so "not in the crawl" proves
                # nothing. Keep the record (and its first_seen) so tomorrow's
                # successful run doesn't report it as brand new.
                live[lid] = entry
            else:
                dropped_ids.append(entry)

    state["listings"] = live
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = now
    state["version"] = STATE_VERSION
    return {"new": new_ids, "dropped": dropped_ids, "price_drops": price_changes}


def money(v):
    return f"${v:,.0f}" if v else None


def _fmt_addr(r):
    """One address format for both portals.

    allhomes writes "93/10 Thynne Street, Bruce ACT 2617"; REA writes
    "93/10 Thynne Street, Bruce" — the tail is rebuilt from the parsed fields so
    the list doesn't look half-finished.
    """
    street = (r.get("street") or "").strip()
    suburb = (r.get("suburb") or "").strip()
    tail = " ".join(x for x in (r.get("state") or "", r.get("postcode") or "") if x)
    if not suburb:
        return (r.get("address") or street).strip()
    return f"{street}, {suburb} {tail}".strip()


def render(confirmed, unknown, changes, meta, state):
    now = datetime.now(AEST).strftime("%Y-%m-%d %H:%M AEDT")
    new_set = set(changes["new"])
    drop_map = {r["id"]: old for r, old in changes["price_drops"]}
    first_seen = {k: v.get("first_seen", "") for k, v in state["listings"].items()}

    items = []
    for bucket, rows in (("confirmed", confirmed), ("unknown", unknown)):
        for r in rows:
            ratio, bound = strata_ratio(r)
            items.append({
                "id": r["id"],
                "addr": _fmt_addr(r),
                "street": r["street"],
                "suburb": r["suburb"],
                "state": r["state"],
                "price": r["price_raw"] or "价格未公开",
                "priceNum": r["price_num"],
                "beds": r["beds"],
                "baths": r["baths"],
                "parking": r["parking"],
                "type": r["prop_type"] or "—",
                "url": r["url"],
                "sources": r.get("sources") or [r.get("source", "allhomes")],
                "urls": r.get("urls") or [{"source": r.get("source", "allhomes"),
                                           "url": r["url"]}],
                "priceFrom": r.get("price_from"),
                "est": ({k: v for k, v in r["est"].items() if k != "at"}
                        if r.get("est", {}).get("lo") else None),
                "bucket": r.get("bucket", "unknown"),
                "strata": r.get("strata"),
                "strataChecked": bool(_allhomes_url(r)),
                "stale": bool(r.get("stale")),
                "ratio": ratio,
                "ratioBound": bound,
                "isNew": r["id"] in new_set,
                "oldPrice": drop_map.get(r["id"]),
                "firstSeen": first_seen.get(r["id"], "")[:10],
            })

    n_new = len([i for i in items if i["isNew"]])
    n_drop = len(drop_map)
    n_fixed = sum(1 for i in items if i["bucket"] == "fixed")
    n_guide = sum(1 for i in items if i["bucket"] == "guide")
    n_unk = len(unknown)
    # auctions whose valuation still *could* land under budget
    n_stale = sum(1 for i in items if i["stale"])
    n_reach = sum(1 for i in items
                  if i["bucket"] == "unknown" and i["est"]
                  and i["est"]["lo"] <= CONFIG["max_price"])

    payload = {
        "generatedAt": now,
        "config": {k: v for k, v in CONFIG.items() if k != "_args"},
        "meta": meta,
        "stats": {"total": len(items), "fixed": n_fixed, "guide": n_guide,
                  "unknown": n_unk, "new": n_new,
                  "reach": n_reach, "stale": n_stale,
                  "dropped": len(changes["dropped"]), "priceDrops": n_drop},
        "items": items,
    }

    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    # Publishable snapshot: one self-contained file, no data/ dumps, no scripts.
    # This is the directory that gets uploaded, so the share link never exposes
    # the raw crawl or the Python source.
    os.makedirs(SHARE_DIR, exist_ok=True)
    with open(os.path.join(SHARE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return payload


def main():
    os.makedirs(DATA, exist_ok=True)
    args = CONFIG["_args"]
    baseline = args.baseline

    if args.render_only:
        with open(CURRENT_FILE, encoding="utf-8") as f:
            cur = json.load(f)
        state = load_state()
        empty = {"new": [], "dropped": [], "price_drops": []}
        # keep the crawl metadata so --render-only still tells the truth about
        # which sources answered on the run that produced this data
        meta = cur.get("meta") or {"total_on_site": "?", "pages": 0}
        p = render(cur["confirmed"], cur["unknown"], empty, meta, state)
        log(f"已按现有数据重绘看板：{p['stats']['total']} 套")
        return p

    ts = datetime.now(AEST).strftime("%H:%M:%S")
    log(f"[{ts}] 开始抓取 allhomes…")

    rows, meta = crawl(CONFIG["search_cap"])
    rows = dedupe(rows)
    log(f"allhomes 解析到 {len(rows)} 条（去重后）")

    rea_rows, rea_ok = ([], False)
    if CONFIG["rea"]["enabled"]:
        log("开始抓取 realestate.com.au（需真实浏览器，约 1 分钟）…")
        rea_rows, rea_ok = crawl_rea()
        if not rea_ok:
            log("  ! REA 本次未取得数据，仅用 allhomes（不判定下架）")
    meta["rea_ok"] = rea_ok
    meta["rea_rows"] = len(rea_rows)

    # keep the full crawl so cross-source coverage can be audited later
    with open(os.path.join(DATA, "all_rows.json"), "w", encoding="utf-8") as f:
        json.dump({"allhomes": rows, "rea": rea_rows}, f, ensure_ascii=False)

    # Shape check per source first (beds/baths + no obvious over-budget price),
    # then merge the two portals by street+suburb, then resolve the blank-price
    # ones from their allhomes detail pages, then decide the bucket.
    def shape_ok(r):
        return matches(r, CONFIG["max_price"], CONFIG["min_beds"], CONFIG["min_baths"])

    merged = merge_sources([r for r in rows if shape_ok(r)] +
                           [r for r in rea_rows if shape_ok(r)])
    n_dual = sum(1 for r in merged if len(r.get("sources", [])) > 1)
    log(f"合并两源后 {len(merged)} 套（其中 {n_dual} 套两个网站都有）")

    cache = load_cache()
    enrich_unknown([r for r in merged if r.get("source") == "allhomes"], cache)

    confirmed, unknown = classify(merged)

    n_stale = 0
    if not rea_ok and os.path.exists(CURRENT_FILE):
        # REA failing must not make 39 listings disappear from the page: to the
        # friend reading it, a vanished listing looks sold. Keep last run's
        # REA-only rows, flagged, and drop them the moment REA answers again.
        with open(CURRENT_FILE, encoding="utf-8") as f:
            prev = json.load(f)
        seen = {r["id"] for r in confirmed + unknown}
        cur_by_id = {r["id"]: r for r in confirmed + unknown}
        for r in prev.get("confirmed", []) + prev.get("unknown", []):
            if "rea" not in (r.get("sources") or [r.get("source")]):
                continue
            if r["id"] in seen:
                # still on the market, just not re-confirmed by REA today: keep
                # its provenance, but never overwrite today's allhomes price
                cur = cur_by_id[r["id"]]
                srcs = cur.setdefault("sources", [cur.get("source", "allhomes")])
                if "rea" not in srcs:
                    srcs.append("rea")
                urls = cur.setdefault("urls", [])
                for u in r.get("urls") or []:
                    if u.get("source") == "rea" and u not in urls:
                        urls.append(u)
                continue
            r["stale"] = True
            (unknown if r.get("bucket") == "unknown" else confirmed).append(r)
            seen.add(r["id"])
            n_stale += 1
        if n_stale:
            log(f"  REA 缺席，沿用上次的 {n_stale} 套 REA 房源（标为未更新）")

    log(f"符合条件：价格已确认 {len(confirmed)} 套 / 价格待核实 {len(unknown)} 套")

    est_cache = load_estimate_cache()
    n_est = enrich_estimates(unknown, est_cache)
    if n_est:
        log(f"其中 {n_est} 套拿到站点估值")

    strata_cache = load_strata_cache()
    n_strata = enrich_strata(confirmed + unknown, strata_cache)
    n_ratio = sum(1 for r in confirmed + unknown if strata_ratio(r)[0])
    if n_strata:
        log(f"strata 读到 {n_strata} 套，其中 {n_ratio} 套可算占价比")
    # promising auctions first: a lower bound above budget can be written off
    unknown.sort(key=lambda r: ((r.get("est") or {}).get("lo") or 9e9, r["street"]))

    state = load_state()
    reset = state.get("version") != STATE_VERSION
    if reset:
        log("状态格式升级（改为跨源键），本次重建基线，不产生新增/下架标记")
        state = {"listings": {}, "runs": 0, "version": STATE_VERSION}

    changes = diff(confirmed, unknown, state, carry_missing=not rea_ok)
    if baseline or reset:
        # establishing the watch list — nothing is "new" on day one
        changes["new"] = []
        changes["price_drops"] = []
        if baseline:
            log("基线模式：本次不产生「新上」标记")
    if not rea_ok:
        # a missing source looks exactly like 40 sold properties — don't cry wolf
        if changes["dropped"]:
            log(f"REA 缺席，本次不判定下架（否则会误报 {len(changes['dropped'])} 套）")
        changes["dropped"] = []

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    with open(CURRENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"confirmed": confirmed, "unknown": unknown, "meta": meta},
                  f, ensure_ascii=False, indent=1)

    payload = render(confirmed, unknown, changes, meta, state)

    st = payload["stats"]
    log(f"本次：新增 {st['new']} · 降价 {st['priceDrops']} · 下架 {st['dropped']} · 在售 {st['total']}")
    for r, old in changes["price_drops"]:
        log(f"  降价 {r['address']}: {money(old)} → {r['price_raw']}")
    for lid in changes["new"]:
        e = state["listings"][lid]
        log(f"  新上 {e['address']} · {e['price_raw']} · {e['beds']}b{e['baths']}b")
    log(f"看板已更新：{HTML_FILE}")
    return payload


if __name__ == "__main__":
    main()
