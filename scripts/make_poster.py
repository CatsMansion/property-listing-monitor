"""Build a WeChat-friendly poster (長图) of today's shortlist.

The hosted link cold-starts (~7 s of white screen after the sandbox sleeps),
which reads as "frozen" inside WeChat. A single tall PNG has no server, no
network and no wait — it opens instantly and forwards natively.

Output: poster.html (750 px wide, data baked in, no JS) — screenshot with
tools/shot.js into a PNG.
"""

import json
import os
import re
from datetime import datetime

WORK = os.path.abspath(os.environ.get("PLM_WORKDIR") or os.getcwd())
DATA = os.path.join(WORK, "data")
OUT = os.path.join(WORK, "poster.html")

with open(os.path.join(DATA, "current.json"), encoding="utf-8") as f:
    cur = json.load(f)

cfg = {}
if os.path.exists(os.path.join(WORK, "config.json")):
    with open(os.path.join(WORK, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

TITLE = cfg.get("title", "房源精选")
CFG_PRICE = cfg.get("max_price", 700_000)
MIN_BEDS = cfg.get("min_beds", 3)
MIN_BATHS = cfg.get("min_baths", 2)
STATES = cfg.get("states") or []

rows = cur["confirmed"] + cur["unknown"]

cond = (f"{MIN_BEDS} 房以上 · {MIN_BATHS} 卫以上 · ${CFG_PRICE:,} 以下"
        + (f" · {('/'.join(STATES))}" if STATES else ""))


def money(n):
    return f"${n:,}" if n else "—"


def short(n):
    return f"${round(n / 1000)}k" if n else "—"


reach = [r for r in rows
         if r["bucket"] == "unknown" and (r.get("est") or {}).get("lo", 9e9) <= CFG_PRICE]
reach.sort(key=lambda r: r["est"]["lo"])

priced = [r for r in rows if r["bucket"] in ("fixed", "guide") and r.get("price_num")]
priced.sort(key=lambda r: r["price_num"])
cheap = priced[:12]

n_fixed = sum(1 for r in rows if r["bucket"] == "fixed")
n_guide = sum(1 for r in rows if r["bucket"] == "guide")
n_unk = sum(1 for r in rows if r["bucket"] == "unknown")
_now = datetime.now()
today = f"{_now.year}年{_now.month}月{_now.day}日"


def card(r, right_main, right_sub, show_pill=True):
    raw = r["price_raw"] or ""
    pill = ""
    # Only the price-less section needs the 拍卖/议价 pill. Stamping 议价 on a
    # listing advertised at $419,000 is simply wrong, so it is opt-in.
    if show_pill:
        kind = "拍卖" if raw.lower().startswith("auction") else "议价"
        # "Auction 31/10/26" carries a date, a bare "Auction" does not — slicing
        # the prefix would leave the English word glued to the Chinese label.
        m = re.search(r"Auction\s+(\d{2}/\d{2}/\d{2})", raw, re.I)
        when = m.group(1) if m else ""
        pill = f'<span class="pill">{kind}{" " + when if when else ""}</span>'
    meta = f'{r["beds"]} 房 · {r["baths"]} 卫'
    if r.get("parking"):
        meta += f' · {r["parking"]} 车位'
    sub = f'{r["suburb"]} {r.get("postcode") or ""}'.strip()
    return f'''<div class="card">
  <div class="left">
    <div class="addr">{r["street"]}</div>
    <div class="meta">{meta} · {sub} {pill}</div>
  </div>
  <div class="right">
    <div class="main">{right_main}</div>
    <div class="sub">{right_sub}</div>
  </div>
</div>'''


cards_a = "\n".join(
    card(r, short(r["est"]["lo"]) + "–" + short(r["est"]["hi"]), "站点估值")
    for r in reach)

def _ratio(r):
    """Annual levy / price, same rule as the dashboard: published price only."""
    q = (r.get("strata") or {}).get("q")
    if not q or not r.get("price_num"):
        return None
    lo, hi = r["price_num"], r.get("price_high") or r["price_num"]
    mid = (lo + hi) / 2 if hi > lo else lo
    pct = q * 4 / mid * 100
    return pct if 0.02 <= pct <= 10 else None


def _right_sub(r):
    base = "起价" if r["bucket"] == "guide" else "固定价"
    pct = _ratio(r)
    if pct is None:
        return base
    return f'{base} · strata {"≤" if r["bucket"] == "guide" else ""}{pct:.2f}%'


cards_b = "\n".join(
    card(r, money(r["price_num"]) + ("+" if r["bucket"] == "guide" else ""),
         _right_sub(r), show_pill=False)
    for r in cheap)

html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{TITLE}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{width:750px;background:#f5f6f8;color:#131720;
    font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
  .hero{{background:#0f766e;color:#fff;padding:34px 34px 28px}}
  .hero h1{{font-size:33px;font-weight:700;letter-spacing:-.5px}}
  .hero .cond{{margin-top:10px;font-size:16px;opacity:.9}}
  .hero .date{{margin-top:4px;font-size:13px;opacity:.72}}
  .nums{{display:flex;gap:10px;margin-top:22px}}
  .num{{flex:1;background:rgba(255,255,255,.14);border-radius:12px;padding:13px 12px}}
  .num .v{{font-size:27px;font-weight:700;letter-spacing:-.6px}}
  .num .l{{font-size:11.5px;opacity:.85;margin-top:2px;white-space:nowrap}}
  .sec{{padding:26px 34px 6px}}
  .sec h2{{font-size:19px;font-weight:700;display:flex;align-items:center;gap:9px}}
  .sec h2 .bar{{width:4px;height:19px;background:#0f766e;border-radius:2px}}
  .sec .hint{{color:#6b7280;font-size:12.5px;margin:7px 0 14px;line-height:1.6}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;
    padding:13px 16px;margin-bottom:9px;display:flex;align-items:center;gap:14px}}
  .left{{flex:1;min-width:0}}
  .addr{{font-size:16px;font-weight:650;letter-spacing:-.2px}}
  .meta{{font-size:12.5px;color:#5c6472;margin-top:5px;display:flex;
    align-items:center;gap:6px;flex-wrap:wrap}}
  .pill{{background:#ffedd5;color:#9a3412;font-size:11px;font-weight:600;
    padding:2px 7px;border-radius:5px}}
  .right{{text-align:right;white-space:nowrap}}
  .main{{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.3px}}
  .sub{{font-size:11.5px;color:#6b7280;margin-top:2px}}
  .foot{{padding:22px 34px 34px;color:#8a919e;font-size:12px;line-height:1.85}}
  .foot b{{color:#5c6472}}
</style></head><body>

<div class="hero">
  <h1>{TITLE}</h1>
  <div class="cond">{cond}</div>
  <div class="date">{today} · allhomes + realestate.com.au 双源合并</div>
  <div class="nums">
    <div class="num"><div class="v">{len(rows)}</div><div class="l">符合条件</div></div>
    <div class="num"><div class="v">{n_fixed}</div><div class="l">固定价</div></div>
    <div class="num"><div class="v">{n_guide}</div><div class="l">起价 $X+</div></div>
    <div class="num"><div class="v">{len(reach)}</div><div class="l">估值够得着</div></div>
  </div>
</div>

<div class="sec">
  <h2><span class="bar"></span>拍卖 / 议价里，估值够得着的 {len(reach)} 套</h2>
  <div class="hint">这类房源不公开价格。下面是 allhomes 自己的估值模型给出的区间（不是业主要价），
    下限仍在 70 万内 —— 打电话前先按这个判断值不值得跑一趟。</div>
  {cards_a}
</div>

<div class="sec">
  <h2><span class="bar"></span>有明确报价的 {len(priced)} 套里，最便宜的 {len(cheap)} 套</h2>
  <div class="hint">固定价 = 报价或区间上限 ≤ $700k；起价 = 「$659,000+」，能谈但还有往上走的余地。</div>
  {cards_b}
</div>

<div class="foot">
  <b>数据说明</b><br>
  · 来源：allhomes.com.au（Domain 集团）+ realestate.com.au，同一套房两站都登的已合并为一条。<br>
  · 卧室/卫生间由本工具本地筛选 —— 两家站点的筛选参数都不可信（allhomes 参数被忽略，
    REA 只认路径里的卧室段、会丢掉卫浴段）。<br>
  · 站点估值是 allhomes 的自动估值模型，<b>不是业主要价，也不能当报价依据</b>，只用来判断优先级。<br>
  · 共 {len(rows)} 套符合条件，本图列出 {len(reach) + len(cheap)} 套；其余为报价更高的固定价/起价，
    以及暂时拿不到估值的拍卖与议价房源。
</div>

</body></html>'''

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"poster.html 已生成：{len(reach)} 套估值够得着 + {len(cheap)} 套最低价")
