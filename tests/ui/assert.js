const fs=require('fs'); const {JSDOM,VirtualConsole}=require('jsdom'); const fixtures=require('./fixtures.js');
const html=fs.readFileSync('/sessions/affectionate-blissful-heisenberg/mnt/trading_platform/ui/index.html','utf8');
function boot(url){
  const vc=new VirtualConsole(); const errs=[]; vc.on('jsdomError',e=>errs.push(e.message));
  const dom=new JSDOM(html,{runScripts:'dangerously',url,virtualConsole:vc,beforeParse(w){
    w.fetch=(u)=>{const p=String(u).replace('http://localhost:8080','').split('?')[0];
      const k=Object.keys(fixtures).find(k=>k.split('?')[0]===p); const b=k!==undefined?fixtures[k]:null;
      return Promise.resolve({ok:b!==null,status:b===null?404:200,statusText:'OK',
        text:()=>Promise.resolve(JSON.stringify(b)),json:()=>Promise.resolve(b)});};
    w.WebSocket=function(){const self=this;this.readyState=1;this.send=()=>{};this.close=()=>{};
      setTimeout(()=>{ if(self.onopen) self.onopen();
        if(self.onmessage) self.onmessage({data:JSON.stringify(fixtures['/api/state'])}); },10);};
    w.Notification=undefined;}});
  return {dom,errs};
}
const A=[]; const check=(n,c)=>A.push([n,!!c]);
(async()=>{
  // 1. Deep link straight to a non-default tab, as a browser reload would.
  let {dom}=boot('http://localhost:8080/#logs'); let w=dom.window;
  await new Promise(r=>setTimeout(r,700));
  check('reload on #logs restores Logs tab (not Dashboard)', w.document.querySelector('.navitem.active span').textContent==='Logs');
  check('  ...and page title says Logs', w.document.getElementById('page-title').textContent==='Logs');

  // 2. Deep link carrying a book.
  ({dom}=boot('http://localhost:8080/#portfolio/real')); w=dom.window;
  await new Promise(r=>setTimeout(r,700));
  check('reload on #portfolio/real restores tab AND book', w.document.querySelector('.navitem.active span').textContent==='Portfolio' && w.document.querySelector('#book-toggle button.active').textContent==='Real');
  check('  ...title reflects the book', w.document.getElementById('page-title').textContent==='Portfolio (Real)');

  // 3. Bare "/" stamps a hash so the FIRST reload already works.
  ({dom}=boot('http://localhost:8080/')); w=dom.window;
  await new Promise(r=>setTimeout(r,700));
  check('bare / defaults to dashboard and stamps hash', w.document.querySelector('.navitem.active span').textContent==='Dashboard' && w.document.location.hash==='#dashboard/paper');

  // 4. Garbage hash falls back instead of rendering nothing.
  ({dom}=boot('http://localhost:8080/#../../etc/passwd')); w=dom.window;
  await new Promise(r=>setTimeout(r,700));
  check('garbage hash falls back to dashboard', w.document.querySelector('.navitem.active span').textContent==='Dashboard');

  // Now the content assertions, on one booted instance.
  ({dom}=boot('http://localhost:8080/#signals')); w=dom.window;
  await new Promise(r=>setTimeout(r,700));
  const sig=w.document.getElementById('content').innerHTML;
  check('vetoed row shows VETOED, not HOLD', sig.includes('>VETOED<'));
  check('ALREADY_OPEN row shows HOLDING', sig.includes('>HOLDING<'));
  check('genuine BUY still shows BUY', sig.includes('>BUY<'));

  w.showTab('control'); await new Promise(r=>setTimeout(r,400));
  const ctl=w.document.getElementById('content').innerHTML;
  check('account field shows masked value, not ${RH_ACCOUNT_NUMBER}', ctl.includes('••••1234') && !ctl.includes('${RH_ACCOUNT_NUMBER}'));
  check('account field is read-only when env-managed', /id="rh-account-number"[^>]*readonly/.test(ctl));
  check('Execution Posture panel present', ctl.includes('Execution Posture'));
  check('  ...surfaces order circuit breaker', ctl.includes('Order circuit breaker'));
  check('  ...surfaces who places the order', ctl.includes('Who places the order'));
  check('  ...surfaces validation receipt', ctl.includes('Backtest validation receipt'));
  check('  ...force_paper reported as the veto it is', ctl.includes('vetoes everything'));

  w.showTab('dashboard'); await new Promise(r=>setTimeout(r,400));
  const dash=w.document.getElementById('content').innerHTML;
  check('Market Pulse now includes Regime', dash.includes('Regime') && dash.includes('BULL'));
  check('999h macro sentinel rendered as "none scheduled"', dash.includes('none scheduled') && !dash.includes('999.0h'));
  check('veto summary banner explains the 0-buy-signal scan', /vetoed before scoring ran/.test(dash));
  check('  ...names the dominant veto code', dash.includes('STALE_QUOTE'));

  w.showTab('news'); await new Promise(r=>setTimeout(r,400));
  check('News mood also free of the 999h sentinel', !w.document.getElementById('content').innerHTML.includes('999.0h'));

  w.showTab('learning'); await new Promise(r=>setTimeout(r,400));
  const lrn=w.document.getElementById('content').innerHTML;
  check('Learning no longer duplicates the metrics strip', !lrn.includes('Closed patterns'));
  check('  ...and no longer crashes on null profit_factor', !lrn.includes("Couldn't load"));

  w.setViewMode('paper'); w.showTab('positions'); await new Promise(r=>setTimeout(r,400));
  const pp=w.document.getElementById('content').innerHTML;
  check('Paper positions hides the confirm_fill panel', !pp.includes('Manage a fill'));
  w.setViewMode('real'); await new Promise(r=>setTimeout(r,400));
  const rp=w.document.getElementById('content').innerHTML;
  check('Real positions shows the confirm_fill panel', rp.includes('Manage a fill'));
  check('  ...without the false UI-cannot-place-trades claim', !rp.includes("can't place or confirm trades"));

  w.setViewMode('real'); w.showTab('portfolio'); await new Promise(r=>setTimeout(r,700));
  const rpf=w.document.getElementById('content').innerHTML;
  check('headline is total account value, not cash', rpf.includes('2004.15') && rpf.includes('total account value'));
  check('  ...cash and holdings shown so the total is checkable', rpf.includes('1859.94') && rpf.includes('144.21'));
  check('  ...flags local-vs-broker market value disagreement', /Local book values open positions at/.test(rpf));
  check('reconciliation names the cross-account position', rpf.includes('CLF') && /Not held in the configured account/.test(rpf));
  check('All-Time Realized is labelled real-book-only', rpf.includes('real book only'));
  check('  ...and is flagged unverifiable against the ledger', /cannot be\s+reproduced from your trades|gap against the figure above/.test(rpf));

  w.setViewMode('paper');
  w.showTab('logs'); await new Promise(r=>setTimeout(r,400));
  const lg=w.document.getElementById('content').innerHTML;
  check('traceback frames marked as continuations', (lg.match(/log-line cont/g)||[]).length>=2);

  check('Chart.js CDN dependency removed', !html.includes('cdnjs.cloudflare.com'));
  check('theme is light on warm paper (not clinical white)', /--bg-0:\s*#fdfdfc/i.test(html));
  check('accent is indigo, not a P&L colour', /--indigo:\s*#4338ca/i.test(html) && /--accent:\s*var\(--indigo\)/.test(html));
  check('gain/loss stay green/red (semantic, untouched by the re-theme)',
        /--green:\s*#0f8a4d/i.test(html) && /--red:\s*#c8341b/i.test(html));
  check('filter controls recede until hovered/focused',
        /tr\.col-filter-row input\[type=text\], tr\.col-filter-row select \{[^}]*background: transparent/.test(html));
  check('native select arrow replaced with a styled chevron',
        /appearance: none/.test(html) && /background-image: url\("data:image\/svg\+xml/.test(html));
  check('active filters render as removable chips', html.includes('filter-chip') && html.includes('_renderFilterChips'));
  check('toast has all four kinds styled', ['.toast.success','.toast.error','.toast.warn','.toast.info'].every(s=>html.includes(s)));
  check('websocket upgrades to wss on https', html.includes("location.protocol === \"https:\" ? \"wss:\" : \"ws:\""));

  const pass=A.filter(a=>a[1]).length;
  console.log(A.map(([n,ok])=>`${ok?'PASS':'FAIL'}  ${n}`).join('\n'));
  console.log(`\n${pass}/${A.length} assertions passed`);
  process.exit(pass===A.length?0:1);
})();
