// One-off: measure a page at phone width and name the elements that overflow.
// node tools/cdp_probe.js <file-or-url>
const fs = require('fs');

const PORT = 9222;
const TARGET = process.argv[2];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
  if (!page) throw new Error('NO_TARGET');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    }
  });
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('WS_ERR')));
    setTimeout(() => rej(new Error('WS_TIMEOUT')), 10000);
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 2, mobile: true,
  });
  await send('Page.navigate', { url: TARGET });
  await sleep(2500);

  const expr = `(() => {
    const vw = document.documentElement.clientWidth;
    const out = { vw, scrollWidth: document.documentElement.scrollWidth,
                  bodyScroll: document.body.scrollWidth, offenders: [] };
    document.querySelectorAll('*').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 || r.width > vw + 1) {
        out.offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className || '').toString().slice(0, 46),
          w: Math.round(r.width), right: Math.round(r.right),
          text: (el.textContent || '').trim().slice(0, 34),
          kids: el.children.length,
        });
      }
    });
    out.offenders = out.offenders.slice(0, 18);
    return JSON.stringify(out, null, 1);
  })()`;

  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  console.log(r.result.value);

  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync('probe.png', Buffer.from(shot.data, 'base64'));
  ws.close();
  process.exit(0);
}

main().catch(e => { console.error('FATAL', e.message); process.exit(1); });
