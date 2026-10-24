// Full-page PNG screenshot over CDP (headless --screenshot crops to the window).
// node tools/shot.js <file-or-url> <out.png> [cssWidth] [scale]
const fs = require('fs');

const PORT = 9222;
const TARGET = process.argv[2];
const OUT = process.argv[3] || 'shot.png';
const WIDTH = Number(process.argv[4] || 750);
const SCALE = Number(process.argv[5] || 2);
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
    width: WIDTH, height: 1200, deviceScaleFactor: 1, mobile: false,
  });
  await send('Page.navigate', { url: TARGET });
  await sleep(1800);

  const dim = await send('Runtime.evaluate', {
    expression: 'JSON.stringify({h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth})',
    returnByValue: true,
  });
  const { h, w } = JSON.parse(dim.result.value);
  console.log(`页面 ${w}×${h}，按 ${SCALE}x 输出`);

  const shot = await send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
    clip: { x: 0, y: 0, width: w, height: h, scale: SCALE },
  });
  fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
  console.log(`已写出 ${OUT}  ${(fs.statSync(OUT).size / 1024 | 0)}KB`);
  ws.close();
  process.exit(0);
}

main().catch(e => { console.error('FATAL', e.message); process.exit(1); });
