// Reproducible browser regression against loopback-only synthetic state.
// Build first. Set PYTHON and CHROME_BIN when the defaults are unavailable.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';

const root = process.cwd();
const output = path.join(root, '.build', `ui-optimization-${Date.now()}`);
await fs.mkdir(output, { recursive: true });
const python = process.env.PYTHON ?? 'python';
const chromeBin = process.env.CHROME_BIN ?? (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : 'google-chrome');
const fixture = spawn(python, ['scripts/ui-optimization-fixture.py', '--root', output], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
let fixtureLog = '';
fixture.stderr.on('data', (chunk) => { fixtureLog = (fixtureLog + chunk.toString()).slice(-20000); });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let chrome, ws, evaluate, screenshot;
try {
  const port = await new Promise((resolve, reject) => {
    let text = '';
    const timer = setTimeout(() => reject(new Error('Fixture startup timed out: ' + fixtureLog)), 15000);
    fixture.once('exit', (code) => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${fixtureLog}`)); });
    fixture.stdout.on('data', (chunk) => { text += chunk.toString(); if (text.includes('\n')) { clearTimeout(timer); try { resolve(JSON.parse(text.split('\n')[0]).port); } catch (error) { reject(error); } } });
  });
  const origin = `http://127.0.0.1:${port}`;
  chrome = spawn(chromeBin, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + path.join(output, 'chrome'), 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  let debugPort;
  for (let i = 0; i < 100; i++) { try { debugPort = (await fs.readFile(path.join(output, 'chrome/DevToolsActivePort'), 'utf8')).split('\n')[0]; break; } catch {} await sleep(100); }
  assert(debugPort, 'Chrome debugging endpoint unavailable');
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  let nextId = 0;
  const pending = new Map(), errors = [];
  ws.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    if (message.id) { const item = pending.get(message.id); pending.delete(message.id); if (item) message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result); }
    else if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => { const id = ++nextId; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params })); });
  evaluate = async (expression) => { const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text); return result.result.value; };
  const wait = async (expression) => { for (let i = 0; i < 120; i++) { if (await evaluate(`Boolean(${expression})`)) return; await sleep(100); } throw new Error('Timed out: ' + expression); };
  const click = async (text, scope = 'body') => { for (let i = 0; i < 120; i++) { if (await evaluate(`(()=>{const button=[...document.querySelectorAll(${JSON.stringify(`${scope} button`)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!button||button.disabled)return false;button.click();return true;})()`)) return; await sleep(100); } throw new Error('Missing enabled button: ' + text); };
  const change = async (selector, value) => evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});if(!input)throw Error('Missing input');Object.getOwnPropertyDescriptor(input.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const go = async (hash, title) => { await evaluate(`location.hash=${JSON.stringify(hash)}`); await wait(`document.querySelector('.suite-page-heading h1')?.textContent===${JSON.stringify(title)}`); await wait("!document.querySelector('.suite-content')?.textContent.includes('正在加载页面…')"); await sleep(120); };
  const control = (body) => fetch(origin + '/fixture/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const read = (file) => evaluate(`fetch('/api/v1/file/'+${JSON.stringify(file)}).then(r=>r.json()).then(v=>v.data.content)`);
  screenshot = async (name) => { const result = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await fs.writeFile(path.join(output, name), Buffer.from(result.data, 'base64')); };
  const assertFits = async (label) => assert(await evaluate('document.documentElement.scrollWidth<=innerWidth+2'), label + ' overflows horizontally');
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: origin });
  const login = async () => { await wait("document.querySelector('input[name=password]')"); await change('input[name=password]', 'synthetic-browser-password'); await evaluate("document.querySelector('.suite-login-submit').click()"); await wait("document.querySelector('.suite-page-heading')"); };
  await login();
  await wait("document.querySelectorAll('.suite-status-component').length===7");
  assert(await evaluate("[...document.querySelectorAll('.suite-section h2')].findIndex(x=>x.textContent.includes('每秒'))<[...document.querySelectorAll('.suite-section h2')].findIndex(x=>x.textContent.includes('各项服务'))"));
  await control({ age: 24 }); await click('暂停自动刷新'); await click('立即刷新'); await sleep(1600);
  assert((await evaluate('document.body.textContent')).includes('采样时正常'));
  assert(!(await evaluate('document.body.textContent')).includes('运行采集暂不可用'));
  await control({ age: 0 }); await click('恢复自动刷新');

  await go('#/lists', '域名分流'); await wait("document.querySelector('textarea')?.value.includes('domain:example.com')");
  const originalList = await evaluate("document.querySelector('textarea').value");
  await click('可视化编辑'); await click('文本编辑');
  assert.equal(await evaluate("document.querySelector('textarea').value"), originalList);
  assert(await evaluate("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置').disabled"));
  await change('textarea', originalList + 'domain:draft.example\n');
  await go('#/ttl', 'TTL 规则'); await wait("document.querySelector('textarea')?.value.includes('example.com@@')");
  const ttl = await evaluate("document.querySelector('textarea').value");
  await click('可视化编辑'); await click('文本编辑'); assert.equal(await evaluate("document.querySelector('textarea').value"), ttl);
  await go('#/lists', '域名分流'); await wait("document.querySelector('textarea')?.value.includes('draft.example')");

  await go('#/advanced', '高级配置'); await wait("document.querySelector('textarea')?.value.includes('LOCAL_IP')");
  await click('可视化编辑'); await click('添加变量');
  const newVariable = await evaluate("document.querySelectorAll('input[aria-label$=名称]').length");
  await change(`[aria-label="变量 ${newVariable} 名称"]`, 'EXTRA_VALUE');
  await change(`[aria-label="变量 ${newVariable} 值"]`, 'kept');
  await click('文本编辑');
  assert((await evaluate("document.querySelector('textarea').value")).includes('EXTRA_VALUE="kept"'));
  const env = (await evaluate("document.querySelector('textarea').value")) + 'RULES_TTL="55"\n';
  await change('textarea', env);
  await go('#/env', '基础设置'); await wait("document.querySelector('#setting-RULES_TTL')?.value==='55'");
  await control({ expire: true }); await evaluate("document.querySelector('[aria-label=刷新状态]').click()");
  await login(); await wait("document.querySelector('#setting-RULES_TTL')?.value==='55'");
  await click('保存配置'); await wait("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置')?.disabled");
  assert.equal(await read('custom_env.ini'), env);
  await change('#setting-RULES_TTL', '66'); await control({ external_change: true }); await click('保存配置');
  await wait("document.body.textContent.includes('已被其他页面或工具修改')");
  assert.equal(await evaluate("document.querySelector('#setting-RULES_TTL').value"), '66');
  assert.equal(await read('custom_env.ini'), 'VALUE="external"\n');

  await go('#/advanced?file=custom_mod.yaml', '高级配置'); await wait("document.querySelector('input[placeholder=\"a.com domain:b.com\"]')?.value==='nas.example'");
  // YAML node edits keep the existing comment instead of regenerating the file.
  await evaluate("document.querySelector('input[placeholder=\"a.com domain:b.com\"]').scrollIntoView()");
  await change('input[placeholder="a.com domain:b.com"]', 'changed.example');
  assert((await evaluate("document.querySelector('[aria-label=\"custom_mod.yaml 文件内容\"]').value")).includes('# preserve this comment'));
  await evaluate("[...document.querySelectorAll('.suite-file-tabs button')].find(button=>button.textContent.includes('custom_env.ini')).click()");
  await wait("document.querySelector('textarea')?.value.includes('RULES_TTL=\"66\"')");
  await wait("document.body.textContent.includes('磁盘文件已变化')");
  assert((await evaluate("document.querySelector('textarea').value")).includes('RULES_TTL="66"'));

  // Hold an already-committed save response across a route change and a newer
  // read/save. Releasing it must not roll the shared baseline back.
  await evaluate(`(()=>{const state={fetch:window.fetch,confirm:window.confirm,held:false};window.__saveOrder=state;window.confirm=()=>true;window.fetch=async(...args)=>{const response=await state.fetch.call(window,...args);if(!state.held&&String(args[0]).includes('/file/custom_env.ini')&&args[1]?.method==='PUT'){state.held=true;await new Promise(resolve=>state.release=resolve);state.released=true;}return response;};})()`);
  try {
    await go('#/env', '基础设置'); await wait("document.querySelector('#setting-RULES_TTL')?.value==='66'");
    await click('重新读取');
    await wait("document.querySelector('[aria-label=重新读取配置]')?.disabled===false&&[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置')?.disabled");
    assert.equal(await read('custom_env.ini'), 'VALUE="external"\n');
    await evaluate("document.querySelector('#setting-RULES_TTL').closest('.suite-setting').querySelector('input[type=checkbox]').click()");
    await wait("document.querySelector('#setting-RULES_TTL')?.disabled===false");
    await change('#setting-RULES_TTL', '55');
    await click('保存配置'); await wait('window.__saveOrder.release');
    await go('#/advanced', '高级配置');
    await go('#/env', '基础设置'); await wait("document.querySelector('#setting-RULES_TTL')?.value==='55'");
    await click('重新读取');
    await wait("document.querySelector('#setting-RULES_TTL')?.disabled===false&&[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置')?.disabled");
    await change('#setting-RULES_TTL', '66'); await click('保存配置');
    await wait("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置')?.disabled");
    assert((await read('custom_env.ini')).includes('RULES_TTL="66"'));
    await evaluate('window.__saveOrder.release()'); await wait('window.__saveOrder.released'); await sleep(150);
    assert(await evaluate("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置').disabled"), 'Late save response rolled back the shared baseline');
    await change('#setting-RULES_TTL', '77'); await click('保存配置');
    await wait("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='保存配置')?.disabled");
    assert((await read('custom_env.ini')).includes('RULES_TTL="77"'));
    assert(!(await evaluate('document.body.textContent')).includes('已被其他页面或工具修改'));
  } finally {
    await evaluate('(()=>{const state=window.__saveOrder;state.release?.();window.fetch=state.fetch;window.confirm=state.confirm;delete window.__saveOrder;})()');
  }

  await go('#/system?component=updater', '维护与日志'); await wait("document.querySelector('.suite-log')?.textContent.includes('synthetic updater')");
  assert.equal(await evaluate("document.querySelector('[aria-label=日志组件]').value"), 'updater');
  await go('#/system?component=manager', '维护与日志');
  await wait("document.querySelector('.suite-log')?.textContent.includes('synthetic manager')");
  assert.equal(await evaluate("document.querySelector('[aria-label=日志组件]').value"), 'manager');
  await evaluate('history.back()');
  await wait("document.querySelector('[aria-label=日志组件]')?.value==='updater'&&document.querySelector('.suite-log')?.textContent.includes('synthetic updater')");
  await evaluate("(()=>{const select=document.querySelector('[aria-label=日志组件]');select.value='manager';select.dispatchEvent(new Event('change',{bubbles:true}));})()");
  await wait("location.hash==='#/system?component=manager'&&document.querySelector('.suite-log')?.textContent.includes('synthetic manager')");
  await go('#/diagnostics?domain=example.com', '查询与诊断'); await click('开始查询'); await wait("document.querySelector('.suite-report-summary')");
  await click('开始查询');
  assert(await evaluate("document.querySelector('.suite-report-summary')!==null"));
  await wait("document.querySelector('.suite-report-heading')?.textContent.includes('本次')||![...document.querySelectorAll('button')].some(x=>x.textContent.includes('查询中'))");

  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 860, deviceScaleFactor: 1, mobile: false });
  for (const [hash, title] of [['#/ttl', 'TTL 规则'], ['#/lists', '域名分流'], ['#/advanced', '高级配置'], ['#/', '服务状态']]) {
    await go(hash, title); await assertFits(title);
    if (hash === '#/ttl') { await wait("document.querySelector('textarea')"); assert(await evaluate("document.querySelector('textarea').getBoundingClientRect().top<800"), 'TTL editor is too far down'); }
    await screenshot(hash === '#/' ? 'dashboard-mobile.png' : title + '-mobile.png');
  }
  await evaluate("document.querySelector('[aria-label=切换主题]').click()"); await assertFits('dark dashboard');
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log('PASS: pause semantics, editor round trips, retained/shared/auth-recovered drafts, revision save/conflict, out-of-order save responses, YAML comment preservation, same-page/history log navigation, retained diagnostic results, mobile/dark layouts, no runtime exceptions.');
  console.log('Screenshots: ' + output);
} catch (error) {
  if (evaluate) console.error(await evaluate('document.body.innerText').catch(() => fixtureLog));
  if (screenshot) await screenshot('failure.png').catch(() => {});
  throw error;
} finally { ws?.close(); chrome?.kill(); fixture.kill(); }
