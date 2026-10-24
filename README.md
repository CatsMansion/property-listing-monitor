# property-listing-monitor

盯一次房产搜索，天天给你答案。

跨 **allhomes**（Domain）和 **realestate.com.au**（REA Group）抓取，本地按价格 / 卧室 / 卫浴 / 州过滤，把中介广告里的 strata 抠出来算成年费占房价的比例，产出单文件看板 + 可转发长图 + 跨次运行的「新上 / 降价」差分。

```bash
mkdir my-search && cd my-search
echo '{"max_price":700000,"min_beds":3,"min_baths":2,"states":["ACT"]}' > config.json
python ~/.workbuddy/skills/property-listing-monitor/scripts/monitor.py
open index.html
```

---

## 它解决的真问题

搜房工具的默认行为是**假装每个房源都有价格**。实际上：

- 拍卖房没有价格，只有日期
- 议价房没有价格，只有「联系中介」
- 「$659,000+」是地板价，不是价格
- 「$550,000 - $2,000,000」的下限毫无信息量
- Domain 有一个自动估值，看起来像价格，但不是

把它们塞进一个 `price` 字段排序，出来的表越整齐越骗人。

这个工具把它们拆成三档，**只有能证明上限 ≤ 预算的才进「够得着」**：

| 档 | 含义 | 出现在 |
|---|---|---|
| `fixed` | 有明确标价或区间，上限已知 | 够得着判定 |
| `guide` | `$659,000+`，只有地板 | 单独一栏，可点开 |
| `unknown` | 拍卖 / 议价 / Contact Agent | 「价格待核实」+ 估值参考 |

---

## strata 占价比

价格右边那列百分比 = **季度 strata × 4 ÷ 房价**。

站方没有这个字段。数字只写在广告正文里，八种写法（`$2,633 per quarter` / `$1,250 p.q` / `$3,097.80 pq` / `$2,240 p.a.` / `1216.08 per quarter` …），由 `scripts/strata.py` 逐条抠。

| 情况 | 显示 |
|---|---|
| 有价 + 有 strata | `2.51%`（≥1.5% 标红，≤0.5% 标绿） |
| 起价 `$689,000+` | `≤1.19%`——分母是下限，比值是上限 |
| 拍卖 / 议价 | `$785/季`——没有报价，算不出比例，但金额是真的 |
| 广告里没写 | `—`——不是 0，独立房通常本来就没有 |

实测（142 条）：76 条读到数额，58 条可算比例，区间 **0.30%–2.63%**。

## 三个不常见的设计

**1. 双源合并，且会告状**
两站的 listing id 无关，合并键是 `街道|区`。2026-10-24 实测 191 条里 98 条双源收录，价格冲突 0。真出现冲突时保留能证明预算的那条，另一条记进 `price_from`——不取平均，因为平均值是**谁也没说过**的数字。

**2. 单源挂掉不会误报**
`--no-rea` 实测：在售 191 → 152，但 state 仍保留 191 条，**假降价 0、假新上 0**。原理是 `diff(carry_missing=True)`：抓取失败时保留上次记录，而不是判定为「已售出」。

**3. 抓不到的那一半不会凭空消失**
REA 失败时，上一次运行里只出现在 REA 的房源会原样搬回来，打「REA 未更新」标签，页头弹一条说明。否则页面上少掉几十套，读起来就是「这些房子卖了」。

**4. 过 Kasada 的有头浏览器**
REA 用 Kasada 风控，`curl` 给 429，`--headless --dump-dom` 只给 714–880 字节的壳。抓取器驱动真实 Edge + CDP 读 `outerHTML`。
**窗口必须在屏幕内**——挪到 `-32000,-32000` 会被识别成 headless，全新 profile 卡 90 秒也过不去；`40,40` 时 30 秒内放行。

---

## 输出

```
data/state.json           # 跨次历史，键是 街道|区
data/current.json         # 本次结果
data/estimate_cache.json  # AVM 缓存，7 天过期
data/rea_raw/page-N.html  # REA 原始 DOM，可复现解析
index.html                # 看板
share/index.html          # 单文件快照，可直接静态托管
poster.html               # 750px 长图源，数据已烤进去，无 JS
```

看板筛选：全部 / 新上 / 降价 / 固定价 / 起价 / 价格待核实 / 估值够得着 / 双源都收录 / 仅 ACT。
排序：价格低→高 / strata 占比 低→高 / 高→低 / 最新上架 / 按区名。没有比例的垫底，不当作 0%。`index.html#reach` 可直达「估值够得着」。

---

## 参数

```bash
--max-price 750000
--min-beds 3 --min-baths 2
--states ACT,NSW          # 空 = 不限州
--cap 0                   # 0 = 不设搜索上限（会拉进 400+ 条不可核价的拍卖）
--no-rea                  # 只用 allhomes
--region "canberra - greater region, act"
--pages 9                 # REA 最大页数
--render-only             # 只重画，不抓
--baseline                # 首次建立基线，不报「新上」
```

**搜索上限默认是预算的 1.3 倍。** allhomes 按内部估值给无标价房排序再套价格过滤：一套 4 房 2 卫拍卖房在 `?price=-900000` 里查不到，不带过滤有，`?price=-1500000` 也有。卡死在 70 万会漏掉它；完全不设上限会拉进 400 多条无法核价的房。

---

## 长图请和链接分开发

`make_poster.py` 生成的是**静态图**，图里印的 URL 会随托管地址失效，而图会一直传播下去。所以长图里**不写 URL**，只在页脚说明「另有 N 条未列出」。

---

## 已知边界

- 只做**买**，不做租。
- 过滤靠本地，所以抓取量大于结果量——这是刻意的（`--cap` 解释见上）。
- AVM 覆盖不全：65 条待核实房里 45 条有估值，剩下 20 条只能标「待核实」。
- strata 覆盖 53%（142 条里 76 条）——取决于中介写不写，站方没有可靠来源。
- Kasada 会按 IP 收回通行证：同一天上午能抓，下午连传感器脚本都不再加载。那时 REA 会整体降级。
- REA 抓取依赖本机装的 Edge/Chrome。没有浏览器就退化到 `--no-rea`。
- 首次运行冷启动 TTFB 实测 7.1 秒，醒着时 0.08–0.10 秒。

---

## 文件

| 文件 | 作用 |
|---|---|
| `scripts/monitor.py` | 编排：抓取 → 合并 → 分类 → 估值 → 差分 → 渲染 |
| `scripts/parse_allhomes.py` | 解析 allhomes 卡片；全澳邮编→州映射 |
| `scripts/parse_rea.py` | 解析 REA 原始 DOM |
| `scripts/rea_crawl.js` | CDP 多页抓取；`--warm` 先解一次挑战 |
| `scripts/template.html` | 看板模板 |
| `scripts/strata.py` | 从广告正文提取 strata 与占价比 |
| `scripts/make_poster.py` | 长图生成 |
| `scripts/shot.js` | 整页截图 |
| `scripts/cdp_probe.js` | 真实视口溢出探测 |
| `references/traps.md` | 13 条数据源陷阱的实测记录 |

环境变量：`PLM_WORKDIR`（工作目录）、`PLM_EDGE`（浏览器路径）、`PLM_EDGE_PROFILE`（profile 目录，默认在系统临时目录）。

## License

MIT © 2026 CatsMansion
