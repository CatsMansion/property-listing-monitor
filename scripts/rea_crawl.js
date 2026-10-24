// Multi-page realestate.com.au crawler driven over CDP.
//
// REA sits behind Kasada, which only resolves inside a real headed browser.
// So instead of fetching HTML from Node, we drive Edge: navigate, wait for the
// listing grid to render, then read documentElement.outerHTML and hand it to
// the Python parser (listing-monitor/parse_rea.py).
//
// Usage:
//   node rea_crawl.js --warm [waitMs]          # solve the bot challenge once
//   node rea_crawl.js <outDir> <baseUrl> [maxPages] [waitMsPerPage]
//
// <baseUrl> must be the path-form search URL without the /list-N suffix, e.g.
//   https://www.realestate.com.au/buy/with-3-bedrooms-between-0-700000-in-canberra+-+greater+region%2C+act
//
// Verified facts baked in here:
//   * Only `with-N-bedrooms` and `between-A-B` survive REA's URL canonicaliser.
//     `-with-2-bathrooms-` is silently dropped (both orderings tested), so
//     bathrooms are filtered locally, exactly like allhomes.
//   * 25 cards per page. Total comes from "Showing 1 - 25 of 222 properties".

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const PORT = 9222;
const OUTDIR = process.argv[2];
const BASE = (process.argv[3] || '').replace(/\/+$/, '');
const MAXPAGES = Number(process.argv[4] || 0) || 1e9;
const WAIT = Number(process.argv[5] || 15000);

// Browser + profile are env-overridable and deliberately kept OUTSIDE the repo:
// a Chromium profile is hundreds of MB and must never be committed.
function findBrowser() {
  if (process.env.PLM_EDGE) return process.env.PLM_EDGE;
  const candidates = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  return candidates.find(p => { try { return fs.existsSync(p); } catch { return false; } })
      || candidates[0];
}
const EDGE = findBrowser();
const PROFILE = process.env.PLM_EDGE_PROFILE
  || path.join(os.tmpdir(), 'plm-edge-profile');

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function probe() {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(1500) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

let owned = null;

async function ensureBrowser() {
  if (await probe()) return false;
  console.log('  未发现调试实例，启动专用 Edge（窗口置于屏幕外）…');
  owned = spawn(EDGE, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=msEdgeSidebarV2',
    // MUST be a normal on-screen window. Measured 2026-10-24: with
    // --window-position=-32000,-32000 a brand-new profile never gets past
    // Kasada (880 B stub for 90 s, homepage included); the identical code with
    // a normal window clears it in under 30 s. Offscreen reads as headless.
    '--window-position=40,40',
    '--window-size=1200,900',
  ], { stdio: 'ignore' });

  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if (await probe()) return true;
  }
  throw new Error('EDGE_START_TIMEOUT');
}

async function pageTarget() {
  for (let i = 0; i < 20; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`, { signal: AbortSignal.timeout(2000) });
      const list = await r.json();
      const p = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (p) return p;
      const blank = list.find(t => t.type === 'other');
      if (blank) return blank;
    } catch { /* devtools not ready */ }
    await sleep(500);
  }
  throw new Error('NO_TARGET');
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  ws.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('WS_ERROR')));
    setTimeout(() => rej(new Error('WS_TIMEOUT')), 15000);
  });
  return { ws, send, ready };
}

const PROBE_JS = `(() => {
  const d = document;
  const html = d.documentElement.outerHTML;
  const txt = d.body ? d.body.innerText : '';
  const m = txt.match(/of\\s+([\\d,]+)\\s+properties/i);
  let maxPage = 0;
  d.querySelectorAll('[aria-label^="Go to page "]').forEach(el => {
    const n = parseInt(el.getAttribute('aria-label').replace(/\\D+/g, ''), 10);
    if (n > maxPage) maxPage = n;
  });
  return JSON.stringify({
    len: html.length,
    cards: d.querySelectorAll('article[class*="residential-card"]').length,
    href: location.href,
    total: m ? parseInt(m[1].replace(/,/g, ''), 10) : 0,
    maxPage,
    title: d.title,
    html,
  });
})()`;

async function grab(send, url) {
  await send('Page.navigate', { url });
  const deadline = Date.now() + WAIT;
  let best = null;
  while (Date.now() < deadline) {
    await sleep(1500);
    let v;
    try {
      const r = await send('Runtime.evaluate', { expression: PROBE_JS, returnByValue: true });
      v = JSON.parse(r.result.value);
    } catch (e) {
      continue;
    }
    if (!best || v.len > best.len) best = v;
    if (v.cards >= 5 && v.len > 50000) return v;
  }
  return best;
}

async function warm(ms) {
  // Kasada only lets a PROFILE through once that profile has cleared the
  // challenge at least once. A brand-new profile gets a ~880-byte stub even
  // after 25 s of waiting; the same code against a warmed profile returns a
  // 1.3 MB page instantly. So this mode exists to warm the profile in one step.
  await ensureBrowser();
  const target = await pageTarget();
  const { ws, send, ready } = connect(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'https://www.realestate.com.au/' });

  const deadline = Date.now() + ms;
  let ok = false;
  while (Date.now() < deadline) {
    await sleep(2000);
    const r = await send('Runtime.evaluate', {
      expression: 'JSON.stringify({len: document.documentElement.outerHTML.length, t: document.title, u: location.href})',
      returnByValue: true,
    });
    const v = JSON.parse(r.result.value);
    console.log(`  ${v.len}B  title="${(v.t || '').slice(0, 40)}"`);
    if (v.len > 60000) { ok = true; break; }
  }
  console.log(ok ? 'PROFILE_WARMED' : 'PROFILE_STILL_BLOCKED');
  if (owned) { try { await send('Browser.close'); } catch { /* ignore */ } }
  ws.close();
  process.exit(ok ? 0 : 1);
}

async function main() {
  if (OUTDIR === '--warm') {
    await warm(Number(BASE) || 60000);
    return;
  }
  if (!OUTDIR || !BASE) {
    console.error('usage: node rea_crawl.js <outDir> <baseUrl> [maxPages] [waitMs]');
    console.error('       node rea_crawl.js --warm [waitMs]');
    process.exit(2);
  }
  fs.mkdirSync(OUTDIR, { recursive: true });

  await ensureBrowser();
  const target = await pageTarget();
  const { ws, send, ready } = connect(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');

  let pages = MAXPAGES;
  let total = 0;
  let done = 0;
  let fails = 0;

  for (let n = 1; n <= pages; n++) {
    const url = `${BASE}/list-${n}`;
    const v = await grab(send, url);
    if (!v || v.cards < 5) {
      fails++;
      // Say WHERE it landed; "cards=0" alone cannot be debugged.
      console.log(`  page ${n}: FAILED (cards=${v ? v.cards : 0}, ` +
                  `len=${v ? v.len : 0}, title="${v ? (v.title || '').slice(0, 60) : ''}", ` +
                  `url=${v ? (v.href || '').slice(0, 90) : ''})`);
      if (fails >= 2) { console.log('  连续失败，提前停止'); break; }
      continue;
    }
    fails = 0;
    fs.writeFileSync(path.join(OUTDIR, `page-${n}.html`), v.html, 'utf8');
    done++;

    if (n === 1) {
      total = v.total;
      const byCount = total ? Math.ceil(total / 25) : 0;
      pages = Math.min(pages, Math.max(byCount, v.maxPage) || 1);
      console.log(`  共 ${total} 套 / ${pages} 页  |  ${v.href.slice(0, 110)}`);
    }
    console.log(`  page ${n}/${pages}: ${v.cards} 卡  ${(v.len / 1024 | 0)}KB`);
    if (n < pages) await sleep(1200);
  }

  console.log(`RESULT pages_written=${done} total=${total} out=${OUTDIR}`);

  if (owned) {
    try { await send('Browser.close'); } catch { /* ignore */ }
    ws.close();
    await sleep(800);
    try { owned.kill(); } catch { /* ignore */ }
  } else {
    ws.close();
  }
  process.exit(done > 0 ? 0 : 1);
}

main().catch(e => {
  console.error('FATAL', e.message);
  if (owned) { try { owned.kill(); } catch { /* ignore */ } }
  process.exit(1);
});
