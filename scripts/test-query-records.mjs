// Disposable loopback fixture + isolated headless browser; synthetic UI evidence.
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';

const root = process.cwd(), out = path.join(root, '.build', 'query-records-browser-' + Date.now());
const profile = path.join(out, 'chrome');
await fs.mkdir(out, { recursive: true });
const python = process.env.LOVESTDNS_TEST_PYTHON ?? 'python';
const chromeBin = process.env.LOVESTDNS_TEST_CHROME ?? (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : 'google-chrome');
const fixture = spawn(python, ['-B', 'scripts/query-records-fixture.py', '--root', path.join(out, 'fixture')], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
let fixtureOutput = '', fixtureError = '';
fixture.stdout.on('data', data => fixtureOutput += data);
fixture.stderr.on('data', data => fixtureError = (fixtureError + data).slice(-20000));
fixture.on('error', error => fixtureError += error.message);
const chrome = spawn(chromeBin, ['--headless=new', '--disable-gpu', '--in-process-gpu', '--no-first-run', '--no-default-browser-check', '--no-proxy-server', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
let chromeError = '';
chrome.stderr.on('data', data => chromeError = (chromeError + data).slice(-6000));
chrome.on('error', error => chromeError += error.message);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws, send, evaluate;
try {
  let port, base;
  for (let i = 0; i < 100; i++) {
    try { port = (await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; } catch {}
    const value = fixtureOutput.match(/\{"port": (\d+)\}/); if (value) base = 'http://127.0.0.1:' + value[1];
    if (port && base) break;
    await sleep(100);
  }
  assert(port, 'isolated headless Chrome unavailable'); assert(base, 'fixture unavailable: ' + fixtureError);
  const target = await (await fetch('http://127.0.0.1:' + port + '/json/new?about:blank', { method: 'PUT' })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  let id = 0; const pending = new Map(), errors = [];
  ws.addEventListener('message', event => { const m = JSON.parse(event.data); if (m.id) { const p = pending.get(m.id); pending.delete(m.id); if (p) m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); } else if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails); });
  send = (method, params = {}) => new Promise((resolve, reject) => { const next = ++id; const timer = setTimeout(() => { pending.delete(next); reject(new Error('Browser command timed out: ' + method)); }, 10000); pending.set(next, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } }); ws.send(JSON.stringify({ id: next, method, params })); });
  evaluate = async expression => { const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw Error(r.exceptionDetails.text); return r.result.value; };
  const wait = async expression => { for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await sleep(100); } throw Error('Timed out: ' + expression); };
  const click = label => evaluate(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)});if(!b)throw Error('Button missing');b.click();})()`);
  const input = (selector, value) => evaluate(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(n.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event(n.tagName==='SELECT'?'change':'input',{bubbles:true}));})()`);
  const control = async body => { const r = await fetch(base + '/fixture/query-control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); assert(r.ok); };
  const shot = async name => { await sleep(200); const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await fs.writeFile(path.join(out, name + '.png'), Buffer.from(r.data, 'base64')); };
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1360, height: 1050, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: base }); await wait("!!document.querySelector('input[name=password]')");
  await input('input[name=password]', 'synthetic-browser-password'); await evaluate("document.querySelector('.suite-login-submit').click()");
  await wait("document.querySelectorAll('.suite-nav a').length===6");
  await evaluate("location.hash='#/diagnostics'");
  await wait("document.body.textContent.includes('采集已关闭')");
  assert(await evaluate("document.body.textContent.includes('暂无符合条件的记录')"));
  await shot('default-off');
  await click('开启采集'); await wait("document.body.textContent.includes('正在采集')");
  await click('刷新记录'); await wait("document.querySelectorAll('.query-record-table tbody tr').length===50");
  assert(await evaluate("document.querySelector('.query-record-table tbody tr').textContent.includes('129.synthetic-query')"));
  await shot('recent-desktop');
  await evaluate("document.documentElement.dataset.theme='dark'"); await shot('recent-desktop-dark');
  await evaluate("document.documentElement.dataset.theme='light'");
  await click('查看较早记录'); await wait("document.querySelector('.query-record-table tbody tr')?.textContent.includes('79.synthetic-query')");
  await click('返回最近'); await wait("document.querySelector('.query-record-table tbody tr')?.textContent.includes('129.synthetic-query')");
  await input('[aria-label="筛选来源 IP"]', '192.0.2.20'); await input('[aria-label="筛选查询类型"]', '1'); await click('筛选');
  await wait("document.querySelector('.query-record-table tbody tr')?.textContent.includes('128.synthetic-query')");
  assert(await evaluate("[...document.querySelectorAll('.query-record-table tbody tr')].every(r=>r.textContent.includes('192.0.2.20') && r.textContent.includes('UDP'))"));
  await input('[aria-label="筛选查询域名"]', '%'); await click('筛选'); await wait("!document.querySelector('.query-record-table')");
  await input('[aria-label="筛选来源 IP"]', ''); await input('[aria-label="筛选查询类型"]', ''); await input('[aria-label="筛选查询域名"]', ''); await click('筛选');
  await wait("document.querySelectorAll('.query-record-table tbody tr').length===50");
  await control({ unavailable: true }); await click('刷新记录'); await wait("document.body.textContent.includes('合成采集服务不可用')");
  assert.equal(await evaluate("document.querySelectorAll('.query-record-table tbody tr').length"), 50);
  await control({ unavailable: false, malformed: true }); await click('刷新记录'); await wait("document.body.textContent.includes('查询记录响应格式无效')");
  assert.equal(await evaluate("document.querySelectorAll('.query-record-table tbody tr').length"), 50);
  await control({ malformed: false }); await click('刷新记录');
  await wait("!document.querySelector('.suite-notice.is-error')");
  for (const width of [390, 320]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false });
    await sleep(200);
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'page overflow at ' + width);
    assert(await evaluate("document.querySelector('.suite-table-scroll').scrollWidth>document.querySelector('.suite-table-scroll').clientWidth"), 'table needs its own scroll region');
    await shot('recent-mobile-' + width);
    await evaluate("document.documentElement.dataset.theme='dark'"); await shot('recent-mobile-' + width + '-dark');
    await evaluate("document.documentElement.dataset.theme='light'");
    assert(await evaluate("(()=>{const t=document.querySelector('.suite-table-scroll');t.scrollLeft=t.scrollWidth;return t.scrollLeft>0;})()"), 'table must scroll to response details');
    await shot('recent-mobile-' + width + '-response');
    await evaluate("document.querySelector('.suite-table-scroll').scrollLeft=0");
  }
  await click('关闭采集'); await wait("document.body.textContent.includes('采集已关闭')");
  assert.equal(await evaluate("document.querySelectorAll('.query-record-table tbody tr').length"), 50);
  await click('清空记录'); await click('取消');
  assert.equal(await evaluate("document.querySelectorAll('.query-record-table tbody tr').length"), 50);
  await click('清空记录'); await click('确认清空记录'); await wait("!document.querySelector('.query-record-table')");
  assert(await evaluate("document.body.textContent.includes('0 条')"));
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(out, 'result.json'), JSON.stringify({ passed: true, fixture: 'synthetic loopback browser fixture; no live DNS capture', widths: [1360, 390, 320], checks: ['default_off', 'explicit_enable_disable', '50_row_cursor_pagination', 'source_type_literal_domain_filters', 'unavailable_retains_previous_results', 'malformed_response_rejected', 'clear_confirmation_cancel_and_apply', 'light_dark_mobile_table_scroll', 'six_navigation_entries', 'no_runtime_exceptions'] }, null, 2));
  console.log('PASS: query recorder UI, filters, pagination, errors, clear, 1360/390/320 layouts. Output: ' + out);
} catch (error) { if (evaluate) console.log(await evaluate('document.body.innerText')); console.log(fixtureError.slice(-2500)); console.log(chromeError); throw error; }
finally { if (send) try { await Promise.race([send('Browser.close'), sleep(500)]); } catch {} if (ws) ws.close(); chrome.kill(); fixture.kill(); }
