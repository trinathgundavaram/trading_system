const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');
const fixtures = require('./fixtures.js');

const html = fs.readFileSync('/sessions/affectionate-blissful-heisenberg/mnt/trading_platform/ui/index.html','utf8');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack||e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(html, {
  runScripts: 'dangerously', url: 'http://localhost:8080/', virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (url) => {
      const path = String(url).replace('http://localhost:8080','');
      const key = Object.keys(fixtures).find(k => k === path) ||
                  Object.keys(fixtures).find(k => path.split('?')[0] === k.split('?')[0]);
      const body = key !== undefined ? fixtures[key] : null;
      return Promise.resolve({ ok: body !== null, status: body === null ? 404 : 200,
        statusText:'OK', text: () => Promise.resolve(JSON.stringify(body)),
        json: () => Promise.resolve(body) });
    };
    w.WebSocket = function(){ this.readyState=0; this.send=()=>{}; this.close=()=>{}; };
    w.Notification = undefined;
    w.matchMedia = () => ({matches:false, addListener(){}, removeListener(){}});
  }
});

const { window } = dom;
const tabs = ["dashboard","signals","positions","portfolio","control","performance",
              "learning","strategy","risk","journal","monitor","logs","news"];

process.on("exit",()=>{});setTimeout(()=>{console.log("TIMEOUT-EXIT");process.exit(0)},30000).unref?.();
(async () => {
  await new Promise(r => setTimeout(r, 600));
  const results = [];
  for (const mode of ["paper","real"]) {
    window.setViewMode(mode);
    for (const t of tabs) {
      const before = errors.length;
      try { window.showTab(t); } catch (e) { errors.push(`showTab(${t}) [${mode}] threw: ${e.stack}`); }
      await new Promise(r => setTimeout(r, 130));
      const content = window.document.getElementById('content');
      const txt = (content.textContent||'').trim();
      const flags = [];
      if (txt.length < 20) flags.push('EMPTY');
      if (/undefined|NaN|\[object Object\]/.test(txt)) flags.push('BAD-VALUE:' + (txt.match(/\S*(undefined|NaN|\[object Object\])\S*/)||[])[0]);
      if (/\$\{[A-Z_]+\}/.test(txt)) flags.push('RAW-ENV-PLACEHOLDER');
      if (/999\.0h/.test(txt)) flags.push('SENTINEL-999h');
      if (/Couldn't load/.test(txt)) flags.push('PANEL-ERROR');
      if (errors.length > before) flags.push('NEW-ERRORS');
      results.push(`${mode.padEnd(5)} ${t.padEnd(12)} ${String(txt.length).padStart(6)}ch  hash=${window.location.hash.padEnd(22)} ${flags.join(' ') || 'ok'}`);
      if (t==='signals' && mode==='paper') {
        const m = (content.innerHTML||'').match(/.{220}NaN.{120}/s);
        console.log('--- NaN CONTEXT ---\n' + (m?m[0]:'(not found in html)') + '\n--- end ---');
      }
    }
  }
  console.log(results.join('\n'));
  console.log('\n=== ERRORS (' + errors.length + ') ===');
  console.log(errors.slice(0,12).join('\n---\n') || 'none');
  process.exit(0);
})();
