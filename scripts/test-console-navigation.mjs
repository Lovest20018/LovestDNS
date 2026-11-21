// Reproducible browser regression against loopback-only synthetic state.
// Build first. Set PYTHON and CHROME_BIN when the defaults are unavailable.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';

const root = process.cwd();
const output = path.join(root, '.build', `console-navigation-${Date.now()}`);
await fs.mkdir(output, { recursive: true });
const python = process.env.PYTHON ?? 'python';
const chromeBin = process.env.CHROME_BIN ?? (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : 'google-chrome');
const fixture = spawn(python, ['scripts/console-navigation-fixture.py', '--root', output], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
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
  chrome = spawn(chromeBin, ['--headless=new', '--disable-gpu', '--in-process-gpu', '--no-proxy-server', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + path.join(output, 'chrome'), 'about:blank'], { windowsHide: true, stdio: 'ignore' });
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
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 10000);
    pending.set(id, { resolve: (result) => { clearTimeout(timer); resolve(result); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    ws.send(JSON.stringify({ id, method, params }));
  });
  evaluate = async (expression) => { const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text); return result.result.value; };
  const wait = async (expression) => { for (let i = 0; i < 120; i++) { if (await evaluate(`Boolean(${expression})`)) return; await sleep(100); } throw new Error('Timed out: ' + expression); };
  const click = async (text, scope = 'body') => { for (let i = 0; i < 120; i++) { if (await evaluate(`(()=>{const button=[...document.querySelectorAll(${JSON.stringify(`${scope} button`)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!button||button.disabled)return false;button.click();return true;})()`)) return; await sleep(100); } throw new Error('Missing enabled button: ' + text); };
  const change = async (selector, value) => evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});if(!input)throw Error('Missing input');Object.getOwnPropertyDescriptor(input.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const go = async (hash, title) => { await evaluate(`location.hash=${JSON.stringify(hash)}`); await wait(`document.querySelector('.suite-page-heading h1')?.textContent===${JSON.stringify(title)}`); await wait("!document.querySelector('.suite-content')?.textContent.includes('正在加载页面…')"); await sleep(120); };
  const control = (body) => fetch(origin + '/fixture/navigation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  screenshot = async (name) => { const result = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await fs.writeFile(path.join(output, name), Buffer.from(result.data, 'base64')); };
  const assertFits = async (label) => assert(await evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), label + ' overflows horizontally');
  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: origin });
  const login = async () => { await wait("document.querySelector('input[name=password]')"); await change('input[name=password]', 'synthetic-browser-password'); await evaluate("document.querySelector('.suite-login-submit').click()"); await wait("document.querySelector('.suite-page-heading')"); };
  await login();
  await evaluate('window.confirm = () => true');
  const nav = async (text, scope = '.suite-nav') => {
    await wait(`[...document.querySelectorAll(${JSON.stringify(scope + ' a')})].some(link => link.textContent.trim() === ${JSON.stringify(text)})`);
    await evaluate(`[...document.querySelectorAll(${JSON.stringify(scope + ' a')})].find(link => link.textContent.trim() === ${JSON.stringify(text)}).click()`);
  };
  const selected = async (scope, text) => wait(`document.querySelector(${JSON.stringify(scope + ' a[aria-current=page]')})?.textContent.trim() === ${JSON.stringify(text)}`);
  const back = async (expected) => {
    await evaluate('history.back()');
    await wait(`location.hash === ${JSON.stringify(expected)}`);
  };

  await wait("document.querySelectorAll('.suite-nav a').length===6");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.suite-nav a')].map(link=>link.querySelector('span').textContent)"),
    ['概览', '域名分流', '查询与诊断', '广告过滤', '维护', '系统设置']);
  await nav('域名分流');
  await wait("document.querySelector('textarea')?.value.includes('domain:example.com')");
  await click('强制转发');
  await wait("location.hash.includes('force_forward_list.txt')");
  await wait("document.querySelector('textarea') && !document.querySelector('textarea').disabled");
  await change('textarea', 'domain:unsaved-navigation.example\n');
  await click('可视化编辑');
  await nav('自定义转发', '.suite-local-links');
  await wait("document.activeElement.id==='settings-custom_forward'");
  assert(await evaluate("document.activeElement.getBoundingClientRect().top >= document.querySelector('.suite-file-toolbar').getBoundingClientRect().bottom"),
    'A setting reached from the list editor must be visible below the sticky toolbar');
  await back('#/lists?file=force_forward_list.txt');
  await wait("[...document.querySelectorAll('button')].some(button=>button.textContent==='可视化编辑' && button.classList.contains('active'))");
  await wait("[...document.querySelectorAll('input')].some(input=>input.value==='unsaved-navigation.example')");
  await nav('概览'); await nav('域名分流');
  await wait("location.hash.includes('force_forward_list.txt')");

  await nav('系统设置');
  await wait("document.querySelector('#settings-search')");
  await selected('.suite-settings-nav', '运行参数');
  await go('#/env', '运行参数');
  await go('#/ttl', 'TTL 规则');
  await wait("document.querySelector('textarea')?.value.includes('example.com@@')");
  await nav('查看此文件历史', '.suite-file-toolbar');
  await selected('[aria-label="维护分区"]', '配置历史');
  await wait("document.querySelector('[aria-label=历史配置文件]')?.value==='force_ttl_rules.txt'");
  await back('#/ttl');
  await go('#/deploy', '服务信息');
  await selected('.suite-settings-nav', '服务信息');
  await go('#/settings', '服务信息');
  await nav('查看主机命令', '.suite-notice');
  await wait("document.activeElement.id==='host-commands'");

  await go('#/status', '运行状态');
  await selected('[aria-label="维护分区"]', '运行状态');
  await nav('缓存与快照', '[aria-label="维护分区"]');
  await selected('[aria-label="维护分区"]', '缓存与快照');
  await back('#/status');
  await go('#/cache', '缓存与快照');
  await selected('[aria-label="维护分区"]', '缓存与快照');
  await go('#/system?tab=tasks', '运行状态');
  await wait("document.activeElement.id==='maintenance-tasks'");
  await go('#/', '概览');
  await nav('updater', '.suite-notice');
  await selected('[aria-label="维护分区"]', '组件日志');
  await wait("document.querySelector('.suite-log')?.textContent.includes('synthetic updater diagnostic')");
  await back('#/');

  await go('#/diagnostics?tab=query&domain=example.com', '主动查询');
  await change('[aria-label="DNS 服务"]', '192.0.2.5');
  await change('[aria-label="DNS 端口"]', '5300');
  await evaluate(`document.querySelector('[aria-label="DNS 传输"]').value='tcp'; document.querySelector('[aria-label="DNS 传输"]').dispatchEvent(new Event('change',{bubbles:true}))`);
  await click('开始查询');
  await wait("document.querySelector('.suite-report-heading')");
  await nav('网关观测', '[aria-label="查询与诊断分区"]');
  await selected('[aria-label="查询与诊断分区"]', '网关观测');
  await back('#/diagnostics?tab=query&domain=example.com');
  await wait(`document.querySelector('.suite-report-heading') && document.querySelector('[aria-label="DNS 端口"]').value==='5300' && document.querySelector('[aria-label="DNS 传输"]').value==='tcp'`);
  await go('#/settings?tab=gateway', '外部接入');
  await wait("document.activeElement.id==='gateway-integration'");
  await wait(`document.querySelector('[aria-label="网关 API 地址"]')?.value`);
  await click('测试连接');
  await wait("document.body?.textContent.includes('连接测试通过')");
  await nav('查看网关观测 →', '#gateway-integration');
  await wait("document.querySelector('.suite-gateway-fakeip')");
  await evaluate("document.querySelector('.suite-gateway-fakeip').open=true");
  await change('[aria-label="FakeIP 查询值"]', 'example.com');
  await click('查询 FakeIP');
  await wait("document.querySelector('.suite-gateway-fakeip').textContent.includes('198.18.0.3')");
  await back('#/settings?tab=gateway');
  await wait("document.body?.textContent.includes('连接测试通过')");
  await control({ gateway: false });
  await go('#/gateway', '网关观测');
  await wait("location.hash==='#/diagnostics?tab=gateway'");
  await send('Page.reload');
  await wait("document.body?.textContent.includes('尚未启用网关联动')");

  await go('#/system?tab=updates', '软件更新');
  await wait("location.hash==='#/settings?tab=updates'");
  await wait("document.querySelector('.suite-update-notes')?.textContent.includes('Synthetic browser fixture')");
  await evaluate("document.querySelector('.suite-update-network-panel').open=true");
  await wait("document.querySelector('.suite-update-network-panel select')?.value==='preview'");
  await evaluate("const select=document.querySelector('.suite-update-network-panel select');select.value='stable';select.dispatchEvent(new Event('change',{bubbles:true}))");
  await evaluate("const mode=document.querySelectorAll('.suite-update-network-panel select')[1];mode.value='socks5';mode.dispatchEvent(new Event('change',{bubbles:true}))");
  await wait("document.querySelector('.suite-update-proxy-fields input')");
  await change('.suite-update-proxy-fields input[placeholder]', '192.0.2.10');
  await change('.suite-update-proxy-fields input[type=number]', '1080');
  await click('保存并检查更新');
  await wait("document.body?.textContent.includes('已提交版本检查')");
  const updateSettings = await (await fetch(origin + '/fixture/navigation-state')).json();
  assert.deepEqual(updateSettings.network, { channel: 'stable', mode: 'socks5', host: '192.0.2.10', port: 1080 });
  await nav('广告过滤');
  await wait("document.querySelector('.suite-page-heading h1')?.textContent==='拦截记录'");
  assert.equal((await (await fetch(origin + '/fixture/navigation-state')).json()).filter_refreshes, 0);
  await back('#/settings?tab=updates');

  for (const width of [1360, 390, 320]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
    for (const theme of ['light', 'dark']) {
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
      if (width < 900) await evaluate("document.querySelector('[aria-label=打开导航]').click()");
      await nav('系统设置');
      await wait("document.querySelector('#settings-search')");
      if (width < 900) await wait("document.activeElement.matches('.suite-page-heading h1')");
      await assertFits(`settings ${width} ${theme}`);
      await screenshot(`settings-${width}-${theme}.png`);
    }
  }

  await control({ role: 'observer' });
  await send('Page.reload');
  await wait(`!document.querySelector('.suite-settings-nav [aria-label="DNS 配置"]') && document.querySelector('.suite-settings-nav')`);
  await evaluate("document.querySelector('[aria-label=打开导航]').click()");
  await nav('系统设置');
  await wait("location.hash==='#/settings?tab=service'");
  assert(!await evaluate(`document.querySelector('.suite-nav a[href="#/lists"]')`));
  await go('#/system?tab=logs', '组件日志');
  await wait("document.body?.textContent.includes('需要管理权限')");
  await go('#/cache', '缓存与快照');
  assert(!await evaluate("[...document.querySelectorAll('button')].some(button=>button.textContent.includes('保存 Redis 快照'))"));
  await go('#/settings?tab=external', '外部接入');
  await wait("document.body?.textContent.includes('需要管理权限')");

  await control({ role: 'admin', deployment: 'container' });
  await send('Page.reload');
  await wait(`document.querySelector('.suite-settings-nav [aria-label="DNS 配置"]')`);
  await go('#/settings?tab=updates', '软件更新');
  await wait("document.body?.textContent.includes('通过宿主机更新容器')");
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log('PASS: grouped navigation, permissions, aliases, editor and task returns, gateway, release channel, mobile and themes');
  console.log('Screenshots: ' + output);
} catch (error) {
  if (evaluate) console.error(await evaluate('location.hash + "\\n" + document.body.innerText').catch(() => fixtureLog));
  if (screenshot) await screenshot('failure.png').catch(() => {});
  throw error;
} finally { ws?.close(); chrome?.kill(); fixture.kill(); }
