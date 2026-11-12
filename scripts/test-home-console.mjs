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
const fixture=spawn(python,['-B','scripts/home-console-fixture.py','--root',path.join(out,'fixture')],{cwd:root,stdio:['ignore','pipe','pipe'],windowsHide:true});
let fixtureOutput='',fixtureError='';
fixture.stdout.on('data',data=>fixtureOutput+=data);
fixture.stderr.on('data',data=>fixtureError=(fixtureError+data).slice(-20000));
fixture.on('error',error=>fixtureError+=error.message);
const chrome=spawn(chromeBin,['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--no-proxy-server','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore',windowsHide:true});
let chromeError='';
chrome.on('error',error=>chromeError=error.message);
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let ws,send,e;
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
 ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.id){const p=pending.get(m.id);pending.delete(m.id);if(p)m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}else if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);});
 send=(method,params={})=>new Promise((resolve,reject)=>{const next=++id;pending.set(next,{resolve,reject});ws.send(JSON.stringify({id:next,method,params}));});
 e=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text);return r.result.value;};
 const wait=async(expression,attempts=100)=>{for(let i=0;i<attempts;i++){if(await e(expression))return;await sleep(100);}throw Error('Timed out: '+expression);};
 const control=async(body,endpoint='/fixture/control')=>{const response=await fetch(base+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert(response.ok,'fixture control failed');};
 const click=label=>e(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(label)});if(!b)throw Error('Button missing');b.click();})()`);
 const input=async(selector,value)=>e(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(n.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
 const shot=async name=>{await sleep(200);const r=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await fs.writeFile(path.join(out,name+'.png'),Buffer.from(r.data,'base64'));};
 await send('Page.enable');await send('Runtime.enable');
 await send('Emulation.setDeviceMetricsOverride',{width:1360,height:1050,deviceScaleFactor:1,mobile:false});
 await send('Page.navigate',{url:base});await wait("!!document.querySelector('input[name=password]')");
 await input('input[name=password]','synthetic-browser-password');await e("document.querySelector('.suite-login-submit').click()");
 await wait("document.querySelectorAll('.suite-nav a').length===6 && !!document.querySelector('.suite-home')");
 await wait("document.querySelector('.suite-home-address')?.textContent.includes('15453')");
 assert.equal(await e("document.querySelectorAll('.suite-home-component').length"),8);
 assert.equal(await e("[...document.querySelectorAll('button')].filter(b=>b.textContent.trim()==='刷新').length"),1);
 assert.equal(await e("document.querySelector('.suite-home-auxiliary').open"),false);
 assert(await e("(()=>{const box=document.querySelector('.suite-instance'),status=box.querySelector('.suite-status'),name=box.querySelector('strong');return status.getBoundingClientRect().right<=box.getBoundingClientRect().right-8 && name.scrollWidth>name.clientWidth && name.title.includes('用于验证长实例名称布局')})()"),'long instance name must truncate without pushing status out');
 await shot('home-desktop');
 await e("location.hash='#/advanced?file=custom_env.ini'");await wait("!!document.querySelector('textarea') && document.querySelector('.suite-file-toolbar button')?.disabled===true");
 const text=await e("document.querySelector('textarea').value");
 await input('textarea',text+'\n# home workflow check\n');
 await wait("document.querySelector('.suite-file-toolbar button').disabled===false");
 await e("document.querySelector('.suite-config-workflow>summary').click()");
 await wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='查看重启范围')");
 assert(await e("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围').disabled"),'draft must block restart');
 await e("document.querySelector('.suite-file-toolbar button').click()");
 await wait("[...document.querySelectorAll('button')].find(b=>b.textContent==='查看重启范围')?.disabled===false");
 await click('查看重启范围');await click('确认重启');
 await wait("document.body.textContent.includes('合成测试任务完成')");
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
 await e("location.hash='#/diagnostics'");await wait("!!document.getElementById('diagnostic-target')");
 await e("location.hash='#/advanced?file=custom_env.ini'");await wait("document.querySelector('textarea')?.value.includes('external')");
 await e("document.querySelector('.suite-config-workflow>summary').click()");
 await wait("!!document.querySelector('.suite-inline-query input')");
 await input('.suite-inline-query input','example.com');
 await click('验证样本查询');await wait("document.body.textContent.includes('本次样本取得目标记录')");
 await control({config_error:true},'/fixture/home-control');
 await wait("document.body.textContent.includes('当前配置核对暂不可用')",250);
 assert(await e("document.body.textContent.includes('历史样本 · 当前配置效果待确认')"));
 await control({config_error:false},'/fixture/home-control');
 await e("location.hash='#/diagnostics'");await wait("!!document.getElementById('diagnostic-target')");
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
 assert(await e("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='复制').disabled"));
 await control({identity:'mismatch'},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await wait("document.body.textContent.includes('安装收据与当前发布不一致')");
 assert(!await e("document.body.textContent.includes('c'.repeat(64))"));
 await control({identity:'named'},'/fixture/home-control');
 await e("window.dispatchEvent(new Event('lovestdns-config-changed'))");
 await wait("document.body.textContent.includes('c'.repeat(64))");
 for(const width of [390,320]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  for(const route of ['/', '/config', '/advanced?file=custom_env.ini','/diagnostics','/settings']){
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
 await fs.writeFile(path.join(out,'result.json'),JSON.stringify({passed:true,widths:[1360,390,320],checks:['six_navigation_entries','single_home_refresh','listener_summary','long_instance_name_keeps_status_inside_sidebar','draft_blocks_restart','save_apply_task_query','new_draft_invalidates_sample','external_change_invalidates_sample','unavailable_config_invalidates_sample','observed_target_presets','named_instance_commands','unknown_identity_no_default_command','mismatched_receipt_hides_archive_digest','responsive_routes_and_header','no_runtime_exceptions']},null,2));
 console.log('PASS: home, save/apply/query, draft and external change guards, observed presets, 1360/390/320 layouts, no browser exceptions. Output: '+out);
}catch(error){if(e)console.log(await e('document.body.innerText'));console.log(fixtureError.slice(-2500));throw error;}
finally{if(send)try{await Promise.race([send('Browser.close'),sleep(500)]);}catch{}if(ws)ws.close();chrome.kill();fixture.kill();}
