---
name: property-listing-monitor
slug: property-listing-monitor
displayName: Property Listing Monitor
display_name: "房源监测"
description: Track a property search over time across allhomes and realestate.com.au, filter it locally by price/beds/baths/state, and render a self-contained dashboard plus a share poster. Ships a headed-browser CDP crawler that gets past Kasada, a three-bucket price classifier (fixed / from-price / unknown) that refuses to guess, a cross-portal merge that reports when the two sites disagree, and a diff that will not report mass sales when one source is down. Use when someone asks to watch listings, find homes under a budget, monitor new listings or price drops, or asks whether an auction listing is affordable.
description_zh: 长期跟踪一次房产搜索，跨 allhomes 与 realestate.com.au 抓取，本地按价格/卧室/卫浴/州过滤，产出单文件看板与可转发长图。附：能过 Kasada 的有头浏览器 CDP 抓取器、三档价格分类（固定价 / 起价 / 待核实）且拒绝猜价、从广告正文里抠 strata 且抠不到就留空、strata 年费占房价比、双源合并会在两站报价冲突时如实标注、单源故障时不会误报"大量售出"的差分器。适用于「帮我盯着房源」「70 万以下 3 房」「新上/降价提醒」「拍卖房到底买不买得起」。
category: data
version: 1.0.0
author: CatsMansion
agent_created: true
visibility: public
---

# 房源监测

一次配置，反复跑。每跑一次产出三样东西：`index.html` 看板、`share/index.html` 可直接托管的单文件快照、`poster.html` 可转发的长图源。

**这个工具的核心不是抓取，是「不撒谎」。** 房产数据里有大量看起来像价格、其实不是价格的东西——起价、拍卖、议价、Domain 的自动估值。把它们混成一个 `price` 字段，出来的表越漂亮越危险。

## 何时用

- 「帮我盯着堪培拉 70 万以下 3 房 2 卫」这类**带硬条件、要长期看**的搜索
- 想要**新上 / 降价**提醒，而不是每天重刷网站
- 要解释「拍卖房到底买不买得起」
- 要把结果**发给不用电脑的人**（微信长图）

**不用**：单次查一套房的成交历史（直接看网站更快）、商业地产、租房（本工具的过滤器不含周租金）。

## 四条铁律

1. **价格必须分桶，不许折中。**
   `fixed`（标价上限 ≤ 预算）/ `guide`（`$659,000+` 这种是**地板价**，不是价格）/ `unknown`（拍卖、议价、Contact Agent）。
   `within_budget()` 要求 `price_high is not None and price_high <= max_price`——**没有上限的一律不算够得着**。
   `$550,000-$2,000,000` 这种区间的房，不能因为下限低就算进 70 万以内。

2. **估值不是价格。**
   Domain 的 `priceEstimate`（AVM）只能作为**区间参考**显示，且带 7 天 TTL。绝不能拿 `medium` 当标价去筛。看板里它出现在价格单元格的第二行，写「估值」。

3. **单源故障不许读成「大量售出」。**
   `diff(carry_missing=True)` 会保留上一次的记录。`--no-rea` 实测：191 条 → 在售 152 条，state 仍保留 191，**0 条假降价、0 条假新上**。

4. **抓不到的那一半，要在页面上留位置。**
   REA 失败时，只保住 `state.json` 不够——页面上少掉 39 套，读起来就是"这些房子卖了"。
   所以上一次运行里**只出现在 REA** 的房源原样搬回来，打「REA 未更新」标签，REA 恢复后自然替换。

5. **两站不一致要标出来，不能取平均。**
   合并键是 `街道|区`（两站的 listing id 不同）。2026-10-24 实测 191 条中 98 条双源收录，价格冲突 **0**。一旦出现冲突，保留能证明预算的那条，另一条的价格记进 `price_from` 并标 `sources`。

## strata 占价比

表格右侧那列百分比 = **季度 strata × 4 ÷ 房价**。

- 数字只从**中介公布的价**算。拍卖、议价没有价 → 不算比例，改显示季度金额（`$785/季`）。
- 起价（`$689,000+`）只有下限 → 比值是**上限**，前面加 `≤`。
- 广告里没写 strata → `—`（独立房通常就没有，不是 0）。
- 没有结构化字段，全靠 `scripts/strata.py` 从广告正文里抠。抠不到就留空，不猜。

实测（2026-10-24，142 条）：76 条读到数额，58 条可算比例，区间 **0.30%–2.63%**。
超过 1.5% 标红——那是每年实打实的现金流。

## 两个源，两套脾气

| | allhomes | realestate.com.au |
|---|---|---|
| 渲染 | 服务端渲染，`schema.org` 微数据 | 客户端渲染 + Kasada 风控 |
| 抓法 | 普通 HTTP GET | 有头浏览器 + CDP，读 `outerHTML` |
| 筛选 | `?price=-700000` 有效 | 只有路径式 `with-3-bedrooms-between-0-700000-in-<region>/list-N` |
| 卫生间 | 只能本地筛 | **也只能本地筛** |
| 典型量 | 1878 行/次 | 221 行 / 9 页 |

**REA 的坑（实测，别再试）**：
- `?maxPrice=` / `?minBeds=` 会被 canonicaliser 剥掉，等于没筛。
- `-with-2-bathrooms-` 加在两个位置都**被静默丢弃**，URL 里看不到、结果也不变。
- `--headless --dump-dom` 只拿到 714–880 字节的壳。
- **窗口必须在屏幕内。** `--window-position=-32000,-32000` 会被当成 headless，全新 profile 卡 90 秒也过不了挑战；改回 `40,40` 后 30 秒内自动放行。

**allhomes 的坑**：
- 项目/楼盘卡里有多档套餐价（"land from $110,000"、"house & land $550,000"），全卡扫最小值会抓错。只能看地址链接**正上方**那 700 字节。
- 站方自己有 `$1,485,00` 这种少一位的错价，所以价格正则要求严格的千分位分组。
- `priceGuide`（中介愿意说的话）在 52/52 条拍卖/议价房里是 `null`——**页面上明说了没有**。真正有数的是 `priceEstimate`，45/65 条待核实房有。

## 快速开始

```bash
mkdir my-search && cd my-search
cat > config.json <<'JSON'
{
  "title": "堪培拉 70 万以下 3 房 2 卫",
  "max_price": 700000,
  "min_beds": 3,
  "min_baths": 2,
  "states": ["ACT"]
}
JSON

python ~/.workbuddy/skills/property-listing-monitor/scripts/monitor.py
open index.html
```

第一次跑会**冷启动**：托管的沙箱 TTFB 实测 7.1 秒，醒着时 0.08–0.10 秒。别以为卡死了。

### 常用参数

```bash
--max-price 750000     # 改预算
--min-beds 3 --min-baths 2
--states ACT,NSW       # 空 = 不限州
--cap 0                # 0 = 不设搜索上限（会拉进 400+ 条不可核价的拍卖）
--no-rea               # 只用 allhomes（REA 出故障时的降级）
--region "canberra - greater region, act"
--render-only          # 不抓取，只拿现有数据重画
```

## 搜索上限为什么是预算的 1.3 倍

allhomes 会按**内部估值**给无标价的房源排序，再套价格过滤。实测 105 Petterd St, Page（4 房 2 卫拍卖）：`?price=-900000` 里没有它，不带过滤有，`?price=-1500000` 也有。

所以卡死在 70 万会漏掉可能 68 万成交的拍卖房；完全不设上限又会拉进 400 多条无法核价的房。`1.3×` 是这两者之间的经验值，抓完在本地筛。

## 输出

```
data/
  state.json            # 跨次运行的历史（键是 街道|区）
  current.json          # 本次结果
  estimate_cache.json   # AVM 缓存，7 天过期
  rea_raw/page-N.html   # REA 原始 DOM，可复现解析
index.html              # 看板
share/index.html        # 单文件快照，可直接丢静态托管
poster.html             # 750px 长图源（数据已烤进去，无 JS）
```

看板筛选条：全部 / 新上 / 降价 / 固定价 / 起价 / 价格待核实 / 估值够得着 / 双源都收录 / 仅 ACT。
`index.html#reach` 可以直接链到「估值够得着」。

## 长图：不要烤 URL

`make_poster.py` 生成的是**静态图**，图里的链接会随着托管地址失效。
所以 poster 里**不写 URL**，只在页脚说明「另有 N 条未列出」。

正确发法是图和当前可用的链接**分开发**，链接失效了重发链接，图还能用。

## 移动端自查

不要用 `--window-size=390` 截图判断——桌面版 Edge 有约 500px 的最小窗口宽度，截出来的「移动端」是假的，会让人去修根本不存在的溢出。

用 `scripts/cdp_probe.js`，它走 `Emulation.setDeviceMetricsOverride`，打印真实 `scrollWidth` 和所有越界元素。

本次真正修掉的溢出：`nowrap` 的筛选条放在 row-flex 父级里，子元素默认 `min-width:auto` 把整页撑宽。修法是父级 `flex-direction:column` + 子元素 `min-width:0`。

## 定时

用宿主的定时能力每天跑一次即可。**不要定在早上 8 点**——实测新房源在上午陆续上线，跑太早会漏当天下午的。建议 18:00 之后跑，一次覆盖全天。

## 文件

| 文件 | 作用 |
|---|---|
| `scripts/monitor.py` | 编排：抓取 → 合并 → 分类 → 估值 → 差分 → 渲染 |
| `scripts/parse_allhomes.py` | 解析 allhomes 搜索卡；含全澳邮编→州映射（19/19 测试通过） |
| `scripts/parse_rea.py` | 解析 REA 的 `data/rea_raw/page-N.html` |
| `scripts/rea_crawl.js` | CDP 多页抓取器；`--warm` 先解一次挑战 |
| `scripts/template.html` | 看板模板 |
| `scripts/make_poster.py` | 长图生成 |
| `scripts/shot.js` | 整页截图（`captureBeyondViewport`） |
| `scripts/cdp_probe.js` | 真实视口溢出探测 |
| `references/traps.md` | 数据源陷阱全表（含实测证据） |
