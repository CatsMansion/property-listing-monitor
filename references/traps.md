# 数据源陷阱全表

实测日期 2026-10-24，标的：堪培拉及周边，≤70 万，≥3 房，≥2 卫。
每一条都写了**怎么验的**，因为"我记得是这样"在房产数据上不可靠。

---

## 1. allhomes 的项目卡会把套餐价当成房价

**症状**：某条房显示 `$110,000`，点进去是 55 万的 house & land 套餐。

**原因**：楼盘卡（`Mona - MONA - Inspired Living in Strathnairn` 这类）在一个 `<article>` 里列多档价格：地价、房+地、升级包。全卡取最小值就抓到地价。

**验法**：把该卡的 `<article>` 原文打出来，肉眼数有几个 `$`。

**修法**：两处。
1. `_price()` 只看地址链接**前 700 字节**，不看全卡。
2. `classify()` 用 `re.search(r"\s[-–]\s", address)` 把带破折号的项目名整条排除——它是落地页，不是房源。

---

## 2. allhomes 自己有错价

**症状**：正则抓到 `$1,485`。

**原因**：站方页面上写的是 `$1,485,00`（少一位），严格的千分位分组本该拒绝它，但宽松正则会把前三位当价格。

**修法**：`PRICE_STRICT = r"\$(\d{1,3}(?:,\d{3})+)")`，分组必须严格；再叠一道 `MIN_PLAUSIBLE = 100_000`。

---

## 3. `$659,000+` 不是价格，是地板价

**症状**：按 `maxPrice=700000` 筛，一堆 66 万的实际 80 万成交。

**原因**：`+` 表示"从多少起"。它证明不了上限。

**修法**：三档分类。`guide` 桶的 `price_high` 留空，只显示原样文案。**只有 `fixed` 桶才可能进"够得着"**。

REA 的 `$659,000+` 同理，`parse_rea.py` 里 `price_high = price` 是**故意**的——REA 的搜索本身就是从低往高排的指导价，把它算进范围是为了不漏，但看板上会标成起价。

---

## 4. 区间价的陷阱

**症状**：`$550,000 - $2,000,000` 被判进 70 万以内。

**原因**：只取了下限。

**修法**：`_price()` 同时返回 `(lo, raw, hi)`，`within_budget()` 看 `hi`。

---

## 5. 拍卖 / 议价房：`priceGuide` 是空的

**实测**：52/52 条拍卖或议价房，`priceGuide` 字段为 `null`，页面上明写"价格指南不可用"。

**结论**：**别指望从中介嘴里问出价**。这条路是死的，不要在上面加代码。

**真正有数的是 `priceEstimate`**（Domain 的 AVM）：

```json
"priceEstimate":{"__typename":"PriceEstimate","lower":770000,
                 "medium":900000,"upper":1030000,
                 "confidence":"HIGH","lastUpdated":"2026-10-17"}
```

- 覆盖：65 条待核实房里有 **45 条**有估值。
- 结构是**扁平对象**，没有嵌套的 `address`。

**早期 bug**：用 `\}\}` 去匹配对象结尾，永远匹配不到带嵌套 `address` 的对象，导致 221 个邮编**全部为空**。改成括号深度扫描后 221/221 解析成功（2 条靠区名兜底）。

**三条使用规则**：
1. 只显示区间，不显示 `medium` 单点。
2. 不参与 `within_budget()` 判定，只驱动"估值够得着"这个**独立筛选器**。
3. 7 天过期。AVM 会漂，一个月的数不能信。

---

## 6. REA 的 URL 过滤器只认路径式

| 写法 | 结果 |
|---|---|
| `?maxPrice=700000` | 被 canonicaliser 剥掉 |
| `?minBeds=3` | 同上 |
| `with-3-bedrooms-between-0-700000-in-canberra+-+greater+region%2C+act` | ✅ 有效 |
| 上述 + `-with-2-bathrooms-`（两种位置都试过） | ❌ 静默丢弃，结果不变 |

**结论**：卧室和价格在站上筛，**卫生间只能在本地筛**，和 allhomes 一样。

**验法**：抓回来的 URL 里搜 `bathroom`，搜不到就是被丢了；再看结果条数有没有变。

---

## 7. REA 的 Kasada 风控

**症状**：
- `curl` / `requests` → 429
- `--headless --dump-dom` → 714–880 字节的壳，`title` 为空

**修法**：有头 Edge + `--remote-debugging-port=9222`，Node 22 内置 `WebSocket` 驱动 `Page.navigate` / `Runtime.evaluate`，读 `document.documentElement.outerHTML`。

**致命细节（2026-10-24 实测）**：

```
--window-position=-32000,-32000  + 全新 profile
  → 90 秒 × 7 次，714 B，PROFILE_STILL_BLOCKED

--window-position=40,40          + 全新 profile
  → 30 秒内 796986 B，title="Search for Real Estate, Property & Homes"
  → PROFILE_WARMED
```

**屏幕外的窗口会被当成 headless。** 别为了"不打扰用户"把窗口挪出屏幕，那正好踩中检测。

**排查顺序**（别一上来就怀疑代码）：
1. 同一份代码 + 已知可用的 `PLM_EDGE_PROFILE` 能不能跑通？能 → 问题在 profile 或启动参数。
2. 换全新 profile 但**正常窗口位置**？能 → 问题就是窗口位置。
3. 两步都失败，再去看解析。

---

## 8. 双源合并：id 不能当键

两站的 listing id 完全无关。合并键用 `f"{norm(street)}|{norm(suburb)}"`。

**实测（2026-10-24）**：191 条唯一房源，98 条双源收录，**价格冲突 0**。

**rank() 的取舍**：优先选能证明预算的那条（有 `fixed` 价的 > 只有起价的 > 什么都没有的）。落选那条如果带价，写进 `price_from` 并保留 `sources` / `urls` 两个数组。

**为什么不能取平均**：两个中介对同一套房的报价不同，平均值是个**谁也没说过**的数字。

---

## 9. 单源故障会伪装成"卖光了"

**症状**：REA 挂掉那天，看板报「大量房源售出、若干条降价」。

**原因**：REA 的 191 条里有 93 条只出现在 REA，抓取失败 → 这 93 条从 `current` 消失 → 差分器认为它们下架了。

**修法**：`diff(confirmed, unknown, state, carry_missing=False)`。`crawl_rea()` 返回 `(rows, ok)`，`ok=False` 时用 `carry_missing=True`。

**验证**（临时副本里跑 `--no-rea`）：在售数 191 → 152，state 仍存 191，**假降价 0、假新上 0**。

---

## 10. 冷启动不是卡死

托管沙箱会睡。实测 TTFB：冷 7.1 秒，热 0.10 / 0.09 / 0.08 秒。

用户说"咋卡住了"的时候，先量 TTFB，再判断是不是页面 bug。

---

## 11. 移动端：不要相信 `--window-size=390`

桌面版 Edge 有约 **500px 的最小窗口宽度**。`--window-size=390` 截出来的图是 500px 宽的，据此判断"移动端没问题"是自欺。

**正确做法**：`Emulation.setDeviceMetricsOverride({width:390, height:844, deviceScaleFactor:2, mobile:true})`，然后打印 `document.documentElement.scrollWidth` 对比 `innerWidth`。

**本次真实溢出**：筛选条 `flex-wrap:nowrap` 放进 `.bar`（row-flex），子元素默认 `min-width:auto` 不肯收缩，把整页撑宽。截图看不出来，`scrollWidth` 一看就出来了。

```css
.bar{flex-direction:column;align-items:stretch;gap:8px}
.chips{width:100%;min-width:0;flex-wrap:nowrap;overflow-x:auto}
.stat{min-width:0;padding:11px 10px}
.row>div{min-width:0}
```

**教训**：响应式 bug 用**数字**验，不用眼睛验。

---

## 12. 长图里不要烤 URL

静态图一旦发出去就改不了。图里印的链接随托管地址失效后，图还在一遍遍传播一个死链接。

**做法**：`make_poster.py` 生成的长图**不含 URL**，页脚只写「另有 N 条未列出」。链接和图文分开发。

---

## 13. 价格过滤会把拍卖房挤掉

详见 SKILL.md「搜索上限为什么是预算的 1.3 倍」。

一句话：allhomes 按**内部估值**给无标价房排序再套价格过滤，`?price=-900000` 会把一套实际可能 68 万成交的拍卖房挡在外面。

**验法**：同一套房，分别用 `?price=-900000`、不带过滤、`?price=-1500000` 抓三次，看它在哪几次里出现。

---

## 14. strata 只存在于广告正文里

**没有结构化字段。** Domain 的 payload 里没有 `strataLevy`，`priceGuide` 也没有。
数字只出现在中介写的英文里，实测至少八种写法：

```
Strata Fee: $2,633 per quarter       Strata: $1,250 p.q ( Approx.)
• Strata: $3,097.80 pq              Strata Levies: $907 per quarter
Strata: $2,240 p.a.                 Strata Levy: 1216.08 per quarter   ← 没有 $
Strata: $2056.30 pq ($645.80 Including sinking fund)
Strata: $$676.80per quarter (appr.)  ← 原文就是两个 $
```

**做法**（`scripts/strata.py`）：
1. 先把 `<br>` 和 `<br>` 都换行，再剥标签——同一段文字在页面里有三种转义形态。
2. 标签 `strata|body corporate` 之后、**行尾之前**找金额；遇到 `rates|council|land tax|rental|water|EER` 立刻截断。
3. 金额必须**紧跟周期词**（`p.q.` / `pq` / `p/q` / `per quarter` / `p.a.` / `per annum`）才算数，否则会把「3 房」的 3 当成钱。
4. `p.a.` / `per annum` 除以 4 折成季度。
5. 取**第一个**可信匹配。69 个匹配分布在 63 条房源上，重复的基本是同一句话，不需要聚合。

**拒绝的情况**（返回 None，不猜）：
- `Strata Manager: Civium`（只有管理公司名）
- `Low strata per quarter`（有词没数）
- `No Strata fees!`（是卖点，不是费用）

**实测覆盖**：142 条里 76 条读到数额（53%）。其余是独立房，本来就没有 strata——显示 `—`，不是 0。

**占价比** = 季度 × 4 ÷ 房价。实测 58 条可算，区间 0.30%–2.63%。
分母只用**中介公布的价**：拍卖/议价没有价 → 不算比例，改显示季度金额（`$785/季`）。
起价（`$689,000+`）只有下限 → 比值是上限，前面加 `≤`。

---

## 15. 长图会给固定价房源盖「议价」章

**症状**：`$419,000` 的房源，长图上标签写着「议价」。

**原因**：`card()` 里 `kind = "拍卖" if raw.startswith("auction") else "议价"` 无条件执行，
所有非拍卖房都被盖章。这个标签本来只对「没有报价」那一段有意义。

**修法**：`show_pill=False` 只在不带价的那一段关掉。固定价/起价由右侧小字说明（`固定价 · strata 2.51%`）。

---

## 16. 整页截图 2x 会静默失败

**症状**：`shot.js` 打印了页面尺寸，然后什么都没有，文件也没写出来。

**原因**：13807 CSS px × 2 = 27614 px，超过 Chromium 的截图上限，`Page.captureScreenshot` 失败但脚本没报错。

**修法**：这种超长页面用 1x。需要细节就看裁剪，不要靠倍率。

---

## 17. Kasada 会按 IP 收回通行证

**实测时间线（2026-10-24）**：
- 上午：同一份代码抓到 221 条 / 9 页，profile 是热的。
- 下午：连续十几次尝试后，连 `ips.js` 都不再加载（`res=3`，714 B 壳），任何全新 profile 都过不去。
- 期间 `--window-position`、`--disable-backgrounding-occluded-windows`、
  `Emulation.setFocusEmulationEnabled`、模拟鼠标移动**全部无效**——原因是 IP 侧的风控，不是浏览器参数。

**结论**：别再把时间花在启动参数上。REA 抓不到就走降级，且**降级必须同时发生在展示层**：
只保住 `state.json` 而让页面少掉 39 套房源，读起来等于「这些房子卖了」。

`monitor.py` 的做法：REA 失败时，把上一次运行里**只出现在 REA 的**房源原样搬回来，
打上「REA 未更新」标签，等 REA 恢复后再自然替换。
