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
  const click = async (text, scope = 'body') => { for (let i = 0; i < 120; i++) { if (await evaluate(`(()=>{const button=[...document.querySelectorAll(${JSON.stringify(`${scope} button`)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!button||button.matches(':disabled'))return false;button.click();return true;})()`)) return; await sleep(100); } throw new Error('Missing enabled button: ' + text); };
  const change = async (selector, value) => evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});if(!input)throw Error('Missing input');Object.getOwnPropertyDescriptor(input.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const go = async (hash, title) => { await evaluate(`location.hash=${JSON.stringify(hash)}`); await wait(`document.querySelector('.suite-page-heading h1')?.textContent===${JSON.stringify(title)}`); await wait("!document.querySelector('.suite-content')?.textContent.includes('正在加载页面…')"); await sleep(120); };
  const control = (body) => fetch(origin + '/fixture/navigation', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  screenshot = async (name) => { const result = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await fs.writeFile(path.join(output, name), Buffer.from(result.data, 'base64')); };
  const assertFits = async (label) => assert(await evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), label + ' overflows horizontally');
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: origin });
  await send('Page.bringToFront');
  await wait("document.activeElement?.matches('input[name=password]')");
  assert(await evaluate(`(() => {
    const input = document.querySelector('input[name=password]');
    const label = input.labels[0];
    const toggle = document.querySelector('.suite-password-input button');
    return input.id && input.autocomplete === 'current-password' && label?.htmlFor === input.id
      && label.textContent.trim() === '管理员密码' && !toggle.closest('label') && toggle.type === 'button';
  })()`), 'Password label should be associated with only the password field');
  assert.equal(await evaluate("document.querySelector('.suite-login-card h1').textContent"), '登录 DNS 管理面板');
  assert.equal(await evaluate("document.querySelector('.suite-login-footer code').textContent"), 'sudo lovestdnsctl password');

  const press = async (key, windowsVirtualKeyCode, text = '') => {
    const params = { key, code: key, windowsVirtualKeyCode };
    await send('Input.dispatchKeyEvent', { type: 'keyDown', ...params, text });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', ...params });
  };
  const focusRing = () => evaluate(`(() => {
    const style = getComputedStyle(document.activeElement);
    return { color: style.outlineColor, style: style.outlineStyle, width: style.outlineWidth, offset: style.outlineOffset };
  })()`);
  const contrast = (foreground, background) => {
    const luminance = (color) => color.match(/[\d.]+/g).slice(0, 3).map((channel) => {
      const value = Number(channel) / 255;
      return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
    }).reduce((sum, channel, index) => sum + channel * [.2126, .7152, .0722][index], 0);
    const [low, high] = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
    return (high + .05) / (low + .05);
  };
  const viewports = [[1440, 900], [1280, 720], [768, 1024], [390, 844], [667, 375], [360, 640]];
  for (const theme of ['light', 'dark']) {
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }] });
    await wait(`document.documentElement.dataset.theme === '${theme}'`);
    await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
    await evaluate("document.querySelector('input[name=password]').focus()");
    await press('Tab', 9);
    await wait("document.activeElement.matches('.suite-password-input button[aria-label=显示密码]:focus-visible')");
    const toggleRing = await focusRing();
    await press('Enter', 13, '\r');
    await wait("document.activeElement.getAttribute('aria-label') === '隐藏密码' && document.querySelector('input[name=password]').type === 'text'");
    await press('Enter', 13, '\r');
    await wait("document.activeElement.getAttribute('aria-label') === '显示密码' && document.querySelector('input[name=password]').type === 'password'");
    await press('Tab', 9);
    await wait("document.activeElement.matches('.suite-login-remember input:checked:focus-visible')");
    const rememberRing = await focusRing();
    await screenshot(`login-focus-${theme}.png`);
    await press('Tab', 9);
    await wait("document.activeElement.matches('.suite-login-submit:focus-visible')");
    assert.deepEqual(rememberRing, toggleRing, `${theme} password toggle and checkbox focus rings should match`);
    assert.deepEqual(rememberRing, await focusRing(), `${theme} submit and checkbox focus rings should match`);
    assert.equal(rememberRing.style, 'solid');
    assert.equal(rememberRing.width, '2px');
    assert(Number.parseFloat(rememberRing.offset) >= 2);
    assert.match(rememberRing.color, /^rgb\(/, `${theme} checkbox focus ring should be opaque`);

    const colors = await evaluate(`(() => {
      const style = (selector) => getComputedStyle(document.querySelector(selector));
      const card = style('.suite-login-card').backgroundColor;
      const password = style('input[name=password]');
      const submit = style('.suite-login-submit');
      return { card, pairs: {
        heading: [style('.suite-login-card h1').color, card],
        description: [style('.suite-login-card > p').color, card],
        passwordLabel: [style('.suite-field > label').color, card],
        passwordText: [password.color, password.backgroundColor],
        passwordBorder: [password.borderColor, card],
        toggle: [style('.suite-password-input button').color, password.backgroundColor],
        rememberLabel: [style('.suite-login-remember').color, card],
        rememberChecked: [style('.suite-login-remember input').accentColor, card],
        submitText: [submit.color, submit.backgroundColor],
        submitFill: [submit.backgroundColor, card],
        resetHint: [style('.suite-login-footer').color, style('.suite-login').backgroundColor],
      } };
    })()`);
    assert(contrast(rememberRing.color, colors.card) >= 3, `${theme} checkbox focus ring contrast`);
    for (const [part, pair] of Object.entries(colors.pairs)) {
      const ratio = contrast(...pair);
      assert(ratio >= 4.5, `${theme} login ${part} contrast is ${ratio.toFixed(2)}`);
    }

    for (const [width, height] of viewports) {
      await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width <= 768 });
      const viewport = `${width}x${height} ${theme}`;
      await assertFits(`Login at ${viewport}`);
      const layout = await evaluate(`(() => {
        const label = document.querySelector('.suite-login-remember');
        const row = label.getBoundingClientRect();
        const box = label.control.getBoundingClientRect();
        return { labelHeight: row.height, checkboxSize: [box.width, box.height], pageHeight: document.documentElement.scrollHeight,
          touchX: box.left + box.width / 2, touchTop: row.top + 3, touchBottom: row.bottom - 3 };
      })()`);
      assert(layout.pageHeight <= height, `Login should fit vertically at ${viewport}: ${layout.pageHeight}px`);
      assert(layout.labelHeight >= 44, `Remember login touch target at ${viewport}`);
      assert(layout.checkboxSize.every((size) => size >= 16 && size <= 18), `Remember login checkbox size at ${viewport}`);
      if (width === 360) {
        await send('Emulation.setTouchEmulationEnabled', { enabled: true });
        for (const [y, checked] of [[layout.touchTop, false], [layout.touchBottom, true]]) {
          await send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: layout.touchX, y }] });
          await send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
          await wait(`document.querySelector('.suite-login-remember input').checked === ${checked}`);
          if (!checked) {
            const border = await evaluate("getComputedStyle(document.querySelector('.suite-login-remember input')).borderColor");
            assert(contrast(border, colors.card) >= 4.5, `${theme} unchecked checkbox border contrast`);
          }
        }
        await send('Emulation.setTouchEmulationEnabled', { enabled: false });
      }
      if (width === 390 || width === 667) await screenshot(`login-${width}x${height}-${theme}.png`);
    }
  }
  await send('Emulation.setEmulatedMedia', { features: [] });
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
  console.log('PASS: login semantics, keyboard focus, contrast, touch targets and layout across six viewports in light and dark mode');
  const previousLoginPage = await evaluate('performance.timeOrigin');
  await send('Page.reload');
  await wait(`performance.timeOrigin !== ${previousLoginPage}`);
  const login = async () => { await wait("document.querySelector('input[name=password]')"); await change('input[name=password]', 'synthetic-browser-password'); await evaluate("document.querySelector('.suite-login-submit').click()"); await wait("document.querySelector('.suite-page-heading')"); };
  await login();
  await evaluate('window.confirm = () => true');
  const nav = async (text, scope = '.suite-nav') => {
    await wait(`[...document.querySelectorAll(${JSON.stringify(scope + ' a')})].some(link => link.textContent.trim() === ${JSON.stringify(text)})`);
    await evaluate(`[...document.querySelectorAll(${JSON.stringify(scope + ' a')})].find(link => link.textContent.trim() === ${JSON.stringify(text)}).click()`);
  };
  const selected = async (scope, text) => wait(`document.querySelector(${JSON.stringify(scope + ' a[aria-current=page]')})?.textContent.trim() === ${JSON.stringify(text)}`);
  const assertSidebarAlignment = async () => {
    const positions = await evaluate(`Array.from(document.querySelectorAll('.suite-nav a'), (link) => {
      const label = link.querySelector('span');
      return { name: label.textContent, text: label.getBoundingClientRect().left, icon: link.querySelector('svg').getBoundingClientRect().left };
    })`);
    assert.equal(new Set(positions.map(item => item.text)).size, 1, `Sidebar labels should align: ${JSON.stringify(positions)}`);
    assert.equal(new Set(positions.map(item => item.icon)).size, 1, 'Sidebar icons should align');
  };
  const settingsNav = '[aria-label="系统设置分区"]';
  const gatewayToggle = '.suite-gateway-toggle input';
  const gatewayTab = `${settingsNav} a[href="#/settings?tab=gateway"]`;
  const settingsLinks = () => evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(settingsNav + ' > *')}), (link) => link.tagName === 'A' ? link.textContent : null)`);
  const activeTabStyle = (scope) => evaluate(`(() => {
    const style = getComputedStyle(document.querySelector(${JSON.stringify(scope + ' a[aria-current=page]')}));
    const properties = ['fontSize', 'fontWeight', 'color', 'backgroundColor', 'borderBottomWidth',
      'borderBottomColor', 'borderTopLeftRadius', 'paddingInlineStart', 'paddingBlockStart'];
    return Object.fromEntries(properties.map(property => [property, style[property]]));
  })()`);
  const assertCompactDetails = async (selector) => {
    const measured = await evaluate(`(() => {
      const list = document.querySelector(${JSON.stringify(selector)});
      const fontSizes = (query) => [...new Set([...list.querySelectorAll(query)].map(node => getComputedStyle(node).fontSize))];
      const weights = [...new Set([...list.querySelectorAll('dd')].map(node => getComputedStyle(node).fontWeight))];
      return { labels: fontSizes('dt'), values: fontSizes('dd'), weights };
    })()`);
    assert.deepEqual(measured, { labels: ['12px'], values: ['13px'], weights: ['500'] }, `${selector} should use compact metadata typography`);
  };
  const back = async (expected) => {
    await evaluate('history.back()');
    await wait(`location.hash === ${JSON.stringify(expected)}`);
  };
  const routingBreadcrumb = async (label) => {
    const expected = JSON.stringify(label);
    await wait(`document.querySelector('.suite-breadcrumb strong')?.textContent === ${expected} &&
      document.querySelector('.suite-list-tabs button[aria-pressed="true"]')?.textContent === ${expected}`);
    assert.equal(await evaluate("document.querySelector('.suite-breadcrumb > span')?.textContent"), '域名分流');
  };

  await wait("document.querySelectorAll('.suite-nav a').length===6");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.suite-nav a')].map(link=>link.querySelector('span').textContent)"),
    ['概览', '域名分流', '查询与诊断', '广告过滤', '维护', '系统设置']);
  await nav('域名分流');
  await wait("document.querySelector('textarea')?.value.includes('domain:example.com')");
  await routingBreadcrumb('DNSCrypt');
  await assertSidebarAlignment();
  await screenshot('sidebar-desktop.png');
  const editorFont = await evaluate("getComputedStyle(document.querySelector('.textarea-code')).fontFamily");
  await click('强制转发');
  await wait("location.hash.includes('force_forward_list.txt')");
  await routingBreadcrumb('强制转发');
  await wait("document.querySelector('textarea') && !document.querySelector('textarea').disabled");
  await change('textarea', 'domain:unsaved-navigation.example\n');
  await click('可视化编辑');
  await nav('自定义转发', '.suite-local-links');
  await wait("document.activeElement.id==='settings-custom_forward'");
  assert(await evaluate("document.activeElement.getBoundingClientRect().top >= document.querySelector('.suite-file-toolbar').getBoundingClientRect().bottom"),
    'A setting reached from the list editor must be visible below the sticky toolbar');
  await back('#/lists?file=force_forward_list.txt');
  await routingBreadcrumb('强制转发');
  await wait("[...document.querySelectorAll('button')].some(button=>button.textContent==='可视化编辑' && button.classList.contains('active'))");
  await wait("[...document.querySelectorAll('input')].some(input=>input.value==='unsaved-navigation.example')");
  await nav('概览');
  await wait("document.querySelector('.suite-page-heading h1')?.textContent === '概览'");
  await nav('域名分流');
  await wait("location.hash === '#/lists?file=force_forward_list.txt'");
  await routingBreadcrumb('强制转发');
  await click('强制递归', '.suite-list-tabs');
  await wait("location.hash === '#/lists?file=force_recurse_list.txt'");
  await routingBreadcrumb('强制递归');
  await go('#/lists?file=custom_cn_mark.txt', '域名分流');
  await routingBreadcrumb('自定义 CN 域名标记');
  await screenshot('routing-breadcrumb-desktop.png');
  await click('BT Tracker 列表', '.suite-list-tabs');
  await routingBreadcrumb('BT Tracker 列表');
  await back('#/lists?file=custom_cn_mark.txt');
  await routingBreadcrumb('自定义 CN 域名标记');
  await go('#/lists?file=unknown.txt&context=fixture', '域名分流');
  await wait("location.hash === '#/lists?file=custom_cn_mark.txt&context=fixture'");
  await routingBreadcrumb('自定义 CN 域名标记');

  await nav('系统设置');
  await wait("document.querySelector('#settings-search')");
  await selected(settingsNav, '运行参数');
  await go('#/env', '运行参数');
  await go('#/ttl', 'TTL 规则');
  await wait("document.querySelector('textarea')?.value.includes('example.com@@')");
  await nav('查看此文件历史', '.suite-file-toolbar');
  await selected('[aria-label="维护分区"]', '配置历史');
  await wait("document.querySelector('[aria-label=历史配置文件]')?.value==='force_ttl_rules.txt'");
  await back('#/ttl');
  await go('#/deploy', '服务信息');
  await selected(settingsNav, '服务信息');
  await go('#/settings', '服务信息');
  await nav('查看主机命令', '.suite-notice');
  await wait("document.activeElement.id==='host-commands'");

  await go('#/status', '运行状态');
  await selected('[aria-label="维护分区"]', '运行状态');
  await wait("document.querySelector('details.suite-home-component dd')");
  await evaluate("document.querySelector('details.suite-home-component').open = true");
  await assertCompactDetails('.suite-home-component[open] dl');
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
  await go('#/settings?tab=external', '外部接入');
  await wait(`document.querySelector('${gatewayToggle}')?.checked`);
  assert(!await evaluate("document.querySelector('#gateway-integration')"));
  const externalFonts = await evaluate(`(() => {
    const style = (selector) => getComputedStyle(document.querySelector(selector));
    const body = style('.suite-app').fontFamily;
    const help = style('.suite-detail-help');
    return {
      consistent: ['.suite-api-setting button', '.suite-gateway-toggle input', '.suite-detail-list a', '.suite-detail-help']
        .every((selector) => style(selector).fontFamily === body),
      technical: style('.suite-detail-list code').fontFamily,
      body,
      helpWeight: help.fontWeight,
      helpSize: help.fontSize,
      noteSize: style('.suite-section .suite-note').fontSize,
    };
  })()`);
  assert(externalFonts.consistent, 'External integration controls and explanations should use the console UI font');
  assert.equal(externalFonts.technical, editorFont, 'API addresses and configuration files should use the same code font');
  assert.notEqual(externalFonts.technical, externalFonts.body);
  assert.equal(externalFonts.helpWeight, '400');
  assert.equal(externalFonts.helpSize, '12px');
  assert.equal(externalFonts.noteSize, '13px');
  await nav('网关联动', settingsNav);
  await selected(settingsNav, '网关联动');
  await wait(`document.querySelector('[aria-label="网关 API 地址"]')?.value`);
  assert(!await evaluate("document.querySelector('.suite-api-setting')"));
  await screenshot('gateway-settings-1280-light.png');
  await send('Emulation.setDeviceMetricsOverride', { width: 320, height: 900, deviceScaleFactor: 1, mobile: false });
  await wait("document.querySelector('.suite-sidebar').getBoundingClientRect().right <= 0");
  await assertFits('gateway settings at 320px');
  assert(await evaluate(`(() => {
    const row = document.querySelector('.suite-gateway-settings .suite-target-row');
    const width = row.getBoundingClientRect().width;
    return Array.from(row.querySelectorAll('input')).every(input => input.getBoundingClientRect().width === width);
  })()`), 'Each gateway field should fill the form on phones');
  await screenshot('gateway-settings-320-light.png');
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 960, deviceScaleFactor: 1, mobile: false });
  await click('测试连接');
  await wait("document.body?.textContent.includes('连接测试通过')");
  await nav('查看网关观测 →', '#gateway-integration');
  await wait("document.querySelector('.suite-gateway-fakeip')");
  await click('展开', '.suite-gateway-table');
  await wait("document.querySelector('.suite-gateway-detail')?.textContent.includes('mihomo_decision_unavailable')");
  await assertCompactDetails('.suite-gateway-detail dl');
  await evaluate("document.querySelector('.suite-gateway-fakeip').open=true");
  await change('[aria-label="FakeIP 查询值"]', 'example.com');
  await click('查询 FakeIP');
  await wait("document.querySelector('.suite-gateway-fakeip').textContent.includes('198.18.0.3')");
  await assertCompactDetails('.suite-gateway-fakeip dl');
  await back('#/settings?tab=gateway');
  await wait("document.body?.textContent.includes('连接测试通过')");
  await go('#/settings?tab=external&section=gateway', '网关联动');
  await wait("location.hash === '#/settings?tab=gateway'");
  await wait(`document.querySelector('[aria-label="网关 API 地址"]')?.value`);
  await nav('外部接入', settingsNav);
  await wait(`document.querySelector('${gatewayToggle}')?.checked`);
  await evaluate(`window.confirm = () => false; document.querySelector('${gatewayToggle}').click()`);
  assert(await evaluate(`document.querySelector('${gatewayToggle}').checked`), 'Cancelling must preserve the active connection');
  await evaluate(`window.confirm = () => true; document.querySelector('${gatewayToggle}').click()`);
  await wait(`!document.querySelector('${gatewayTab}') && document.querySelector('${gatewayToggle}')?.checked === false`);
  const gatewayConfig = () => evaluate("fetch('/api/v1/gateway/config').then(response => response.json()).then(result => result.data)");
  assert.equal((await gatewayConfig()).has_token, false);

  await go('#/settings?tab=gateway', '网关联动');
  await wait(`document.querySelector('${gatewayToggle}') && !document.querySelector('#gateway-integration')`);
  await nav('外部接入', settingsNav);
  await wait(`document.querySelector('${gatewayToggle}')?.checked === false`);
  await evaluate(`document.querySelector('${gatewayToggle}').click()`);
  await wait(`document.querySelector('${gatewayTab}')`);
  assert.equal((await gatewayConfig()).enabled, false, 'Displaying the setup tab must not enable gateway reads');
  await evaluate(`document.querySelector('${gatewayToggle}').click()`);
  await wait(`!document.querySelector('${gatewayTab}')`);
  await evaluate(`document.querySelector('${gatewayToggle}').click()`);
  await nav('网关联动', settingsNav);
  const previousGatewayPage = await evaluate('performance.timeOrigin');
  await send('Page.reload');
  await wait(`performance.timeOrigin !== ${previousGatewayPage}`);
  await selected(settingsNav, '网关联动');
  await wait(`document.querySelector('[aria-label="网关 API 地址"]:enabled')`);
  await change('[aria-label="网关 API 地址"]', 'https://192.0.2.100:18788/api/v1');
  await change('[aria-label="网关只读 Token"]', 'synthetic-gateway-observer-token-only');
  await click('保存并启用');
  await wait("document.body?.textContent.includes('联动已启用')");
  assert.equal((await gatewayConfig()).enabled, true);
  await click('断开并清除凭据');
  await wait(`location.hash === '#/settings?tab=external' && !document.querySelector('${gatewayTab}')`);
  assert.equal((await gatewayConfig()).has_token, false);
  await wait(`document.querySelector('${gatewayToggle}')?.disabled === false`);
  await screenshot('gateway-opt-in-1280-light.png');
  await go('#/diagnostics?tab=query', '主动查询');
  assert(!await evaluate(`document.querySelector('.suite-diagnostic-tabs a[href="#/diagnostics?tab=gateway"]')`));
  await go('#/gateway', '网关观测');
  await wait("location.hash==='#/diagnostics?tab=gateway'");
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

  await send('Emulation.setDeviceMetricsOverride', { width: 2560, height: 1000, deviceScaleFactor: 1, mobile: false });
  for (const [route, title, file] of [
    ['#/', '概览', 'overview'],
    ['#/lists?file=force_dnscrypt_list.txt', '域名分流', 'routing'],
    ['#/diagnostics?tab=compare', '链路对照', 'diagnostics'],
    ['#/filter', '拦截记录', 'filtering'],
    ['#/system?tab=status', '运行状态', 'maintenance'],
    ['#/settings?tab=runtime', '运行参数', 'settings'],
  ]) {
    await go(route, title);
    const frame = await evaluate(`(() => {
      const bounds = (selector) => document.querySelector(selector).getBoundingClientRect();
      const main = bounds('.suite-main');
      const content = bounds('.suite-content');
      return {
        left: content.left - main.left,
        right: main.right - content.right,
        heading: bounds('.suite-page-heading h1').left - bounds('.suite-breadcrumb').left,
      };
    })()`);
    assert.deepEqual(frame, { left: 0, right: 0, heading: 0 }, `${title} should fill the workspace and align with the breadcrumb`);
    await assertFits(`${title} at 2560px`);
    await screenshot(`wide-${file}.png`);
  }

  for (const width of [1360, 390, 320]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
    for (const theme of ['light', 'dark']) {
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
      await go('#/system?tab=status', '运行状态');
      const sharedStyle = await activeTabStyle('[aria-label="维护分区"]');
      if (width < 900) {
        await evaluate("document.querySelector('[aria-label=打开导航]').click()");
        await wait("document.querySelector('.suite-sidebar.is-open')?.getBoundingClientRect().left === 0");
        await assertSidebarAlignment();
        if (width === 390 && theme === 'dark') await screenshot('sidebar-mobile-dark.png');
      }
      await nav('系统设置');
      await wait("document.querySelector('#settings-search')");
      if (width < 900) await wait("document.activeElement.matches('.suite-page-heading h1') && document.querySelector('.suite-sidebar').getBoundingClientRect().right <= 0");
      await assertFits(`settings ${width} ${theme}`);
      assert.deepEqual(await activeTabStyle(settingsNav), sharedStyle, `Settings and maintenance tabs should match at ${width}px in ${theme} mode`);
      assert.deepEqual(await settingsLinks(), ['运行参数', '配置状态与应用', 'TTL 规则', '高级配置文件', '软件更新', '服务信息', '外部接入', '来源设备']);
      const tabs = await evaluate(`Array.from(document.querySelectorAll('${settingsNav} > a'), (link) => {
        const {top, height} = link.getBoundingClientRect();
        const text = document.createRange();
        text.selectNodeContents(link);
        return {top, height, lines: new Set(Array.from(text.getClientRects(), (box) => box.top)).size};
      })`);
      assert(tabs.every(tab => tab.lines === 1), 'Settings tab labels should fit on one line');
      if (width >= 900) assert(tabs.every(tab => tab.top === tabs[0].top), 'Desktop settings tabs should share one row');
      else assert(tabs.every(tab => tab.height >= 44), 'Settings tabs should keep their mobile touch targets');
      await screenshot(`settings-${width}-${theme}.png`);
    }
  }

  await go('#/lists?file=custom_cn_mark.txt', '域名分流');
  await routingBreadcrumb('自定义 CN 域名标记');
  await assertFits('domain routing at 320px');
  await screenshot('routing-breadcrumb-320.png');
  await go('#/settings?tab=runtime', '运行参数');
  await control({ role: 'observer', gateway: true });
  const previousAdminPage = await evaluate('performance.timeOrigin');
  await send('Page.reload');
  await wait(`performance.timeOrigin !== ${previousAdminPage} && document.querySelector('${settingsNav}')`);
  assert.deepEqual(await settingsLinks(), ['服务信息']);
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
  await go('#/settings?tab=gateway', '网关联动');
  await wait("document.body?.textContent.includes('需要管理权限')");

  await control({ role: 'admin', deployment: 'container' });
  await send('Page.reload');
  await wait(`document.querySelector('${settingsNav} a[href="#/settings?tab=runtime"]')`);
  await go('#/settings?tab=updates', '软件更新');
  await wait("document.body?.textContent.includes('通过宿主机更新容器')");
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log('PASS: aligned sidebar, list breadcrumbs, shared tabs, permissions, aliases, editor and task returns, optional gateway settings, release channel, full-width pages, mobile and themes');
  console.log('Screenshots: ' + output);
} catch (error) {
  if (evaluate) console.error(await evaluate('location.hash + "\\n" + document.body.innerText').catch(() => fixtureLog));
  if (screenshot) await screenshot('failure.png').catch(() => {});
  throw error;
} finally { ws?.close(); chrome?.kill(); fixture.kill(); }
