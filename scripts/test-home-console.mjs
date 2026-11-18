// Run after build-integrated-ui.py. All changes target a disposable loopback fixture.
// Override LOVESTDNS_TEST_PYTHON and LOVESTDNS_TEST_CHROME for local executables.
import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';

const root=process.cwd(),stamp=Date.now();
const out=path.join(root,'.build','home-console-'+stamp);
const profile=path.join(out,'chrome');
await fs.mkdir(out,{recursive:true});
const python=process.env.LOVESTDNS_TEST_PYTHON ?? 'python';
const chromeBin=process.env.LOVESTDNS_TEST_CHROME ?? (process.platform==='win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : 'google-chrome');
const consoleHost='lovestdns-console.test';
const fixture=spawn(python,['-B','scripts/home-console-fixture.py','--root',path.join(out,'fixture')],{cwd:root,stdio:['ignore','pipe','pipe'],windowsHide:true});
let fixtureOutput='',fixtureError='';
fixture.stdout.on('data',data=>fixtureOutput+=data);
fixture.stderr.on('data',data=>fixtureError=(fixtureError+data).slice(-20000));
fixture.on('error',error=>fixtureError+=error.message);
const chrome=spawn(chromeBin,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--no-proxy-server','--host-resolver-rules=MAP '+consoleHost+' 127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore',windowsHide:true});
let chromeError='';
chrome.on('error',error=>chromeError=error.message);
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let ws,send,e;
const browserErrors = [];
try{
 let port,base;
 for(let i=0;i<100;i++){
  try{port=(await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n')[0];}catch{}
  const value=fixtureOutput.match(/\{"port": (\d+)\}/);if(value)base='http://127.0.0.1:'+value[1];
  if(port&&base)break;await sleep(100);
 }
 assert(port,'isolated Chrome unavailable: '+chromeError);assert(base,'fixture unavailable: '+fixtureError);
 const target=await (await fetch('http://127.0.0.1:'+port+'/json/new?about:blank',{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
 let id=0;const pending=new Map(),errors=[];
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timeout);if(p)m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);});
 send=(method,params={})=>new Promise((resolve,reject)=>{if(ws.readyState!==WebSocket.OPEN){reject(new Error('Browser disconnected during '+method));return;}const next=++id;const timeout=setTimeout(()=>{pending.delete(next);reject(new Error('Browser did not answer '+method));},8000);pending.set(next,{resolve,reject,timeout});ws.send(JSON.stringify({id:next,method,params}));});
 e=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async(expression,attempts=100)=>{for(let i=0;i<attempts;i++){if(await e(expression))return;await sleep(100);}throw Error('Timed out: '+expression);};
 const control=async(body,endpoint='/fixture/control')=>{const response=await fetch(base+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(response.ok,'fixture control failed');return response.json();};
 const click=label=>wait(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)});if(!b||b.matches(':disabled'))return false;b.click();return true;})()`);
 const input=async(selector,value)=>e(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(n.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 const shot=async name=>{await sleep(200);const r=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await fs.writeFile(path.join(out,name+'.png'),Buffer.from(r.data,'base64'));};
 await send('Page.enable');await send('Runtime.enable');
 ws.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
   browserErrors.push(message.params.args.map(arg => arg.description ?? arg.value).join(' '));
  }
 });
 await send('Emulation.setDeviceMetricsOverride',{width:1360,height:1050,deviceScaleFactor:1,mobile:false});
 const consoleUrl=new URL(base);consoleUrl.hostname=consoleHost;
 await send('Page.navigate',{url:consoleUrl.href});await wait("!!document.querySelector('input[name=password]')");
 assert.equal(await e('window.isSecureContext'),false,'the console must be tested on an ordinary HTTP origin');
 assert.equal(await e('typeof crypto.randomUUID'),'undefined');
 await input('input[name=password]','synthetic-browser-password');await e("document.querySelector('.suite-login-submit').click()");
 await wait("document.querySelectorAll('.suite-nav a').length===5 && !!document.querySelector('.suite-home')");
 await wait("document.querySelector('.suite-home-address')?.textContent.includes('15453')");
 assert.equal(await e("document.querySelectorAll('.suite-home-component').length"),8);
 assert.equal(await e("[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()==='刷新').length"),1);
 assert.equal(await e("document.querySelector('.suite-home-auxiliary').open"),false);
 assert(await e("(()=>{const box=document.querySelector('.suite-instance'),status=box.querySelector('.suite-status'),name=box.querySelector('strong');return status.getBoundingClientRect().right<=box.getBoundingClientRect().right-8 && name.scrollWidth>name.clientWidth && name.title.includes('用于验证长实例名称布局')})()"),'long instance name must truncate without pushing status out');
 await shot('home-desktop');
 await wait("document.querySelector('.suite-home-followup').textContent.includes('本次启动后暂无配置应用记录')");
 assert(!await e("document.querySelector('.suite-home-followup a[href=\"#/config?tab=apply\"]')!==null"));
 await click('测试本机解析');
 await wait("document.querySelector('.suite-probe-result')?.textContent.includes('本机解析测试通过')");
 assert(await e("document.querySelector('.suite-probe-result').textContent.includes('2 / 2 个域名取得 A 记录')"));
 await e("document.querySelector('a[href=\"#/diagnostics?tab=device\"]').click()");
 await wait("document.body.innerText.includes('在设备自身执行')");
 await e("location.hash='#/lists?file=trackerslist.txt'");
 await wait("document.querySelector('textarea')?.matches(':disabled')===false && document.querySelector('textarea')?.value.includes('Tracker')");
 const trackerTemplate=await e("document.querySelector('textarea').value");
 assert(await e("document.querySelector('.suite-file-toolbar').textContent.includes('无未保存修改') && document.querySelector('.suite-file-toolbar button').disabled"));
 await input('textarea',trackerTemplate+'http://tracker.example/announce\n');
 await wait("document.querySelector('.suite-file-toolbar').textContent.includes('有未保存修改')");
 await e("location.hash='#/'");
 await wait("document.querySelector('.suite-home-followup')?.textContent.includes('查看未保存草稿')");
 await e("location.hash='#/lists?file=trackerslist.txt'");
 await wait("document.querySelector('textarea')?.value.includes('tracker.example')");
 await input('textarea',trackerTemplate);
 await wait("document.querySelector('.suite-file-toolbar').textContent.includes('无未保存修改')");
 await e("location.hash='#/ttl'");
 await wait("document.querySelector('.suite-nav [aria-current=page]')?.textContent==='解析配置'");
 await e("location.hash='#/system?tab=apply'");
 await wait("document.activeElement?.id==='config-apply' && document.querySelector('.suite-nav [aria-current=page]')?.textContent==='解析配置'");
 await e("location.hash='#/config?tab=apply'");
 await wait("location.hash==='#/config?tab=apply' && document.activeElement?.id==='config-apply'");
 await wait("!!document.querySelector('.suite-config-state .suite-config-version')");
 assert(await e("(()=>{const heading=document.getElementById('config-apply').getBoundingClientRect();return heading.top>=document.querySelector('.suite-topbar').getBoundingClientRect().bottom && heading.bottom<innerHeight;})()"),'the application heading must be visible after navigation');
 assert(await e("!!document.querySelector('.suite-notice a[href=\"#/config?tab=apply\"]')"),'the configuration page must expose the application link');
 await click('预览并校验当前配置');
 await wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='应用此预览配置')");
 assert.equal((await control({},'/fixture/home-control')).submissions,0,'navigation and preview must not submit a task');
 await e("location.hash='#/advanced?file=custom_env.ini'");await wait("document.querySelector('textarea')?.matches(':disabled')===false && document.querySelector('textarea').value.includes('CNAUTO') && document.querySelector('.suite-file-toolbar button')?.disabled===true");
 const text=await e("document.querySelector('textarea').value");
 await input('textarea',text+'\n# home workflow check\n');
 await wait("document.querySelector('.suite-file-toolbar button').disabled===false");
 await e("document.querySelector('.suite-config-workflow>summary').click()");
 await wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='查看重启范围')");
 assert(await e("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围').disabled"),'draft must block restart');
 await e("document.querySelector('.suite-file-toolbar button').click()");
 await wait("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围')?.disabled===false");
 await control({lose_response:true,restart_seconds:45},'/fixture/home-control');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:780,deviceScaleFactor:1,mobile:false});
 await e("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围').scrollIntoView({block:'center'})");
 await click('查看重启范围');await click('确认重启 DNS');
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('正在提交')");
 assert(await e("(()=>{const status=document.querySelector('.suite-action-status').getBoundingClientRect();return status.top>=76 && status.bottom<innerHeight;})()"),'mobile feedback should appear where the action was triggered');
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('正在重启 DNS')");
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await sleep(350);
 assert(!await e("document.body.innerText.includes('暂时无法提交维护操作')"),'active restart must suppress the generic worker warning');
 await e("window.__homeReloadMarker=true");
 await send('Page.reload');
 await wait("!window.__homeReloadMarker && document.querySelector('.suite-action-status')?.textContent.includes('正在重启 DNS')");
 await control({restart_seconds:0},'/fixture/home-control');
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('重启完成 · DNS 已恢复应答')",130);
 assert.equal((await control({},'/fixture/home-control')).submissions,1,'lost response, reload and polling must not replay the restart');
 await e("window.__homeReloadMarker=true");
 await send('Page.reload');
 await wait("!window.__homeReloadMarker && document.querySelector('.suite-config-workflow>summary')?.textContent.includes('查看最近任务')");
 await e("document.querySelector('.suite-config-workflow>summary').click()");
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('重启完成 · DNS 已恢复应答')");
 await e("document.querySelector('.suite-action-status a').click()");
 assert.equal(await e('document.activeElement?.id'),'config-query-domain');
 await send('Emulation.setDeviceMetricsOverride',{width:1360,height:1050,deviceScaleFactor:1,mobile:false});
 await input('.suite-inline-query input','example.com');await click('验证样本查询');
 await wait("document.body.textContent.includes('本次样本取得目标记录')");
 await shot('config-query-desktop');
 await input('textarea',text+'\n# new draft after sample\n');
 await wait("document.body.textContent.includes('历史样本 · 当前配置效果待确认')");
 assert(await e("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围').disabled"));
 await input('textarea',text+'\n# home workflow check\n');
 await click('验证样本查询');await wait("document.body.textContent.includes('本次样本取得目标记录')");
 await control({external_change:true});
 await wait("document.body.textContent.includes('历史样本 · 当前配置效果待确认')",250);
 await e("location.hash='#/diagnostics?tab=query'");await wait("!!document.getElementById('diagnostic-target')");
 await e("location.hash='#/advanced?file=custom_env.ini'");await wait("document.querySelector('textarea')?.value.includes('external')");
 await e("document.querySelector('.suite-config-workflow>summary').click()");
 await wait("!!document.querySelector('.suite-inline-query input')");
 await input('.suite-inline-query input','example.com');
 await click('验证样本查询');await wait("document.body.textContent.includes('本次样本取得目标记录')");
 await control({config_error:true},'/fixture/home-control');
 await wait("document.body.textContent.includes('当前配置核对暂不可用')",250);
 assert(await e("document.body.textContent.includes('历史样本 · 当前配置效果待确认')"));
 await control({config_error:false},'/fixture/home-control');
 await e("location.hash='#/advanced?file=custom_mod.yaml'");
 await wait("document.querySelector('textarea[aria-label^=\"custom_mod.yaml\"]')?.matches(':disabled')===false");
 const yaml=await e("document.querySelector('textarea').value");
 await input('textarea',yaml+'\n# changed custom rule\n');
 await e("document.querySelector('.suite-file-toolbar button').click()");
 await wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='预览并校验当前配置'&&!b.disabled)");
 await click('预览并校验当前配置');
 await click('应用此预览配置');
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('分流已加载')");
 await e("location.hash='#/lists?file=force_forward_list.txt'");
 await wait("document.querySelector('.suite-file-toolbar code')?.textContent==='force_forward_list.txt' && document.querySelector('textarea')?.matches(':disabled')===false");
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:620,deviceScaleFactor:1,mobile:false});
 await input('textarea','domain:example.net\n');
 await e("document.querySelector('textarea').scrollIntoView({block:'start'});window.scrollBy(0,-215)");
 await e("document.querySelector('.suite-file-toolbar button').click()");
 await wait("document.querySelector('.suite-save-feedback')?.textContent.includes('已保存')");
 assert(await e("(()=>{const next=document.querySelector('.suite-save-next').getBoundingClientRect();return next.top>=76 && next.bottom<innerHeight;})()"),'mobile next step must remain visible after saving a scrolled editor');
 await click('查看下一步');
 assert.equal(await e('document.activeElement?.parentElement?.id'),'config-next-step');
 assert(await e("(()=>{const panel=document.querySelector('#config-next-step summary').getBoundingClientRect();return panel.top>=document.querySelector('.suite-editor-toolbar').getBoundingClientRect().bottom && panel.bottom<innerHeight;})()"));
 await wait("document.querySelector('#config-query-domain')?.value==='example.net'");
 await shot('saved-rule-next-step-mobile');
 await send('Emulation.setDeviceMetricsOverride',{width:1360,height:1050,deviceScaleFactor:1,mobile:false});
 assert.equal((await control({},'/fixture/home-control')).submissions,2,'watched lists must not start maintenance');
 await control({manager_error:true},'/fixture/home-control');
 await e("location.hash='#/system?tab=tasks'; window.dispatchEvent(new Event('online'))");
 await wait("document.body.innerText.includes('暂时无法读取任务')");
 await control({manager_error:false},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('online'))");
 await wait("!document.body.innerText.includes('暂时无法读取任务')");
 await control({fail_next_restart:true},'/fixture/home-control');
 await e("location.hash='#/config?tab=apply'");
 await click('查看重启范围');
 await click('确认重启 DNS');
 const failure=".suite-action-status:has(a[href='#/system?component=bootstrap'])";
 await wait(`document.querySelector(${JSON.stringify(failure)})?.textContent.includes('合成测试：新配置初始化失败')`);
 assert(await e(`document.querySelector(${JSON.stringify(failure)}).textContent.includes('运行配置已恢复。DNS 服务已启动。')`));
 await shot('restart-failure-desktop');
 await e(`document.querySelector(${JSON.stringify(failure)}).querySelector('a').click()`);
 await wait("location.hash==='#/system?component=bootstrap' && document.querySelector('[aria-label=日志组件]')?.value==='bootstrap'");
 assert.equal((await control({},'/fixture/home-control')).submissions,3,'following the failure must not start another task');
 await control({drop_next_submission:true},'/fixture/home-control');
 await e("location.hash='#/config?tab=apply'");
 await click('查看重启范围');await click('确认重启 DNS');
 await wait("document.body.innerText.includes('暂未查到受理记录')");
 const lost=(await control({reject_next_submission:true},'/fixture/home-control')).last_request_id;
 assert.match(lost,/^[a-f0-9]{32}$/);
 await click('用原编号重试重启 DNS');
 await wait("document.querySelector('.suite-action-status')?.textContent.includes('提交失败。合成测试：重试被明确拒绝')");
 assert.equal(await e("sessionStorage.getItem('lovestdns-pending-maintenance')"),null);
 const rejected=await control({},'/fixture/home-control');
 assert.equal(rejected.last_request_id,lost,'a retry must use the original request id');
 assert.equal(rejected.submissions,3,'a rejected retry must not create a task');
 await wait("['确认重启 DNS','预览并校验当前配置'].every(label=>[...document.querySelectorAll('button')].some(b=>b.textContent===label&&!b.disabled))");
 await control({probe_failure:true},'/fixture/home-control');
 await e("location.hash='#/'");
 await wait("document.querySelector('.suite-home-followup')?.textContent.includes('重启 DNS · 失败')");
 await e("window.__realNow = Date.now; Date.now = () => window.__realNow() - 3600000");
 try {
  await click('测试本机解析');
  await wait("document.querySelector('.suite-probe-region').textContent.includes('正在向本机 DNS 查询')");
  await wait("document.querySelector('.suite-probe-result.is-failed')?.textContent.includes('部分或全部样本解析失败')");
  assert(await e("document.querySelector('.suite-probe-result').textContent.includes('1 / 2 个域名取得 A 记录')"));
  assert(await e("document.querySelector('.suite-probe-result li:last-child').textContent.includes('www.google.com') && document.querySelector('.suite-probe-result li:last-child').textContent.includes('SERVFAIL')"));
  await shot('home-probe-failure');
 } finally {
  await e("Date.now = window.__realNow; delete window.__realNow");
  await control({probe_failure:false},'/fixture/home-control');
 }
 await click('测试本机解析');
 await wait("document.querySelector('.suite-probe-result.is-passed')?.textContent.includes('本机解析测试通过')");
 await control({restart_seconds:60},'/fixture/home-control');
 assert.equal(await e(`(async()=>{
  const session=await (await fetch('/auth/session')).json();
  const response=await fetch('/api/v1/maintenance/tasks',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token},
    body:JSON.stringify({action:'restart_dns_core',request_id:'aa'.repeat(16),expected_sha256:null})});
  return response.status;
 })()`),202);
 await e("window.dispatchEvent(new Event('online'))");
 await wait("document.querySelector('.suite-home-health h2')?.textContent.includes('正在重启 DNS') && document.querySelector('.suite-probe-result.is-historical')?.textContent.includes('历史样本')");
 await control({restart_seconds:0,mixed_config:true},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('online'))");
 await wait("document.querySelector('.suite-home-followup')?.textContent.includes('1 项配置与应用记录一致，8 项暂无本次启动的应用记录')");
 assert(await e("document.querySelector('.suite-probe-result.is-historical') && !document.querySelector('.suite-probe-result.is-passed')"),'a completed restart must not restore older green probe evidence');
 await click('测试本机解析');
 await wait("document.querySelector('.suite-probe-result.is-passed')?.textContent.includes('本机解析测试通过')");
 await e("location.hash='#/diagnostics?tab=query'");await wait("!!document.getElementById('diagnostic-target')");
 assert(await e("[...document.getElementById('diagnostic-target').options].some(o=>o.textContent.includes('5304'))"));
 await e("const s=document.getElementById('diagnostic-target');s.value=[...s.options].find(o=>o.textContent.includes('转发通道')&&o.textContent.includes('TCP')).value;s.dispatchEvent(new Event('change',{bubbles:true}))");
 assert.equal(await e("document.querySelector('input[aria-label=\"DNS 端口\"]').value"),'5304');
 assert.equal(await e("document.querySelector('select[aria-label=\"DNS 传输\"]').value"),'tcp');
 await e("location.hash='#/settings'");await wait("document.body.textContent.includes('应用与前端')");
 assert(await e("document.querySelector('pre.suite-log').textContent.includes('sudo /usr/local/sbin/lovestdns-syntheticctl health')"));
 assert(!await e("document.body.textContent.includes('sudo lovestdnsctl')"));
 assert(await e("document.body.textContent.includes('/etc/lovestdns-synthetic/lovestdns-synthetic.env')"));
 assert(await e("document.body.textContent.includes('a'.repeat(40)) && document.body.textContent.includes('b'.repeat(64)) && document.body.textContent.includes('c'.repeat(64))"));
 await e("document.querySelector('.suite-debug summary').focus()");
 await send('Page.bringToFront');
 await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
 await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
 assert(await e("document.querySelector('.suite-debug').open"));await e('window.scrollTo(0,0)');await shot('settings-desktop');
 await control({identity:'unknown'},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await wait("document.body.textContent.includes('当前实例未提供可核对的维护命令')");
 assert(!await e("document.body.textContent.includes('sudo lovestdnsctl')"));
 assert(await e("![...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='复制')"));
 await control({identity:'mismatch'},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await wait("document.body.textContent.includes('安装收据与当前发布不一致')");
 assert(!await e("document.body.textContent.includes('c'.repeat(64))"));
 await control({identity:'named'},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await wait("document.body.textContent.includes('c'.repeat(64))");
 await e("document.querySelector('a[href=\"#/settings?tab=updates\"]').click()");
 await wait("document.querySelector('.suite-update-network select')?.value==='preview'");
 await e("const channel=document.querySelector('.suite-update-network select'); channel.value='stable'; channel.dispatchEvent(new Event('change',{bubbles:true}))");
 await wait("document.querySelector('.suite-update-network').textContent.includes('更新网络有未保存的修改')");
 await e("document.querySelector('a[href=\"#/system?tab=updates\"]').click()");
 await wait("document.body.textContent.includes('更新设置有未保存的修改') && document.body.textContent.includes('0.1.2-dev.synthetic')");
 assert(await e("['检查更新','更新到此版本'].every(label=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()===label&&b.disabled))"));
 await e("history.back()");
 await wait("document.querySelector('.suite-update-network select')?.value==='stable'");
 await click('还原修改');
 await wait("document.querySelector('.suite-update-network select')?.value==='preview'");
 await e("document.querySelector('a[href=\"#/system?tab=updates\"]').click()");
 await wait("['检查更新','更新到此版本'].every(label=>[...document.querySelectorAll('button')].some(b=>b.textContent.trim()===label&&!b.disabled))");
 assert.equal((await control({},'/fixture/home-control')).update_queued,false,'navigation and draft restoration must not queue an update');
 for(const width of [390,320]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  for(const route of ['/', '/config', '/advanced?file=custom_env.ini','/diagnostics','/diagnostics?tab=device','/system?tab=tasks','/settings']){
   await e(`location.hash=${JSON.stringify('#'+route)}`);await sleep(350);
   assert(await e('document.documentElement.scrollWidth<=innerWidth+1'),'page overflow '+width+' '+route);
   assert(await e("document.querySelector('.suite-account>summary').getBoundingClientRect().height<=44"),'account label wraps');
   assert(await e("document.querySelector('.suite-breadcrumb strong').getBoundingClientRect().height<30"),'breadcrumb wraps');
   if(width===390 && route==='/')await shot('home-mobile');
   if(route==='/settings'){
    await e("document.documentElement.dataset.theme='dark'");await shot('settings-'+width+'-dark');
    await e("document.querySelector('pre.suite-log').scrollIntoView({block:'center'})");await shot('settings-commands-'+width+'-dark');
    await e("document.documentElement.dataset.theme='light'");
   }
  }
 }
 await e("location.hash='#/'");await sleep(200);await e("document.documentElement.dataset.theme='dark'");await shot('home-mobile-dark');
 assert.deepEqual(errors,[]);
 const checks=[
  'five_navigation_entries','single_home_refresh','listener_summary','long_instance_name_keeps_status_inside_sidebar',
  'unknown_record_no_restart_prompt','mixed_config_summary','dns_probe_progress_and_partial_failure','restart_marks_probe_historical','device_check',
  'ttl_navigation','config_status_opens_apply_controls','legacy_apply_deep_link','missing_template_starts_clean','draft_blocks_restart',
  'http_maintenance_submission','lost_response_and_reload_no_replay','expected_restart_progress','real_manager_failure',
  'failed_restart_reports_rollback_and_opens_logs','rejected_retry_rechecks_and_unblocks','save_apply_task_query',
  'custom_rule_apply','watched_rule_prefills_domain','new_draft_invalidates_sample','external_change_invalidates_sample',
  'unavailable_config_invalidates_sample','observed_target_presets','named_instance_commands','unknown_identity_no_default_command',
  'mismatched_receipt_hides_archive_digest','update_network_draft_blocks_stale_release','responsive_routes_and_header','no_runtime_exceptions',
 ];
 await fs.writeFile(path.join(out,'result.json'),JSON.stringify({passed:true,widths:[1360,390,320],checks},null,2));
 console.log('PASS: home, save/apply/query, draft and external change guards, observed presets, 1360/390/320 layouts, no browser exceptions. Output: '+out);
}catch(error){if(e)try{console.log(await e('document.body.innerText'));}catch{}console.log(browserErrors.slice(-4).join('\n'));console.log(fixtureError.slice(-2500));throw error;}
finally{if(send)try{await Promise.race([send('Browser.close'),sleep(500)]);}catch{}if(ws)ws.close();chrome.kill();fixture.kill();}
