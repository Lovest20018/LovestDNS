import { useEffect, useRef, useState } from 'react';
import { ListFilter, RefreshCw, Shield } from 'lucide-react';
import { applyFilter, checkFilter, filterLabels } from '../filterApi';
import type { FilterAction, FilterCheck, FilterSource, FilterState } from '../filterApi';
import { errorText, when } from '../suiteApi';
import { Empty, Notice, Section, Status } from '../components/SuiteUI';
import BlockedQueries from '../components/BlockedQueries';
import { filterStatus, useFilterView } from '../filterContext';
import './SuitePage.css';
import './SuiteFiltering.css';

const intervals = [[0, '仅手动'], [21600, '每 6 小时'], [86400, '每天'], [604800, '每周']] as const;
const reasons: Record<string, string> = {
  pattern: '复杂匹配表达式', cosmetic: '元素隐藏规则', modifier: '扩展修饰符',
  hosts_rewrite: '指定 IP 的重写', invalid_domain: '域名格式无效', line_too_long: '规则行超长',
  exception_or_badfilter: '无法保留的例外或禁用规则', preprocessing_directive: '预处理指令',
};

function currentTab() {
  const value = new URLSearchParams(window.location.hash.split('?')[1]).get('tab');
  return value === 'rules' || value === 'allow' ? value : 'records';
}

export default function SuiteFiltering() {
  const { state, error: readError, refresh, publish, setDraft } = useFilterView();
  const [tab, setTab] = useState(currentTab);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    const sync = () => setTab(currentTab());
    window.addEventListener('hashchange', sync);
    return () => { window.removeEventListener('hashchange', sync); request.current?.abort(); };
  }, []);

  const perform = async (action: FilterAction) => {
    if (!state || busy) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await applyFilter(action, state.revision, controller.signal);
      if (controller.signal.aborted) return;
      publish(result.state);
      if (result.job.phase === 'failed') setError(result.job.message);
      else {
        if (action.action === 'configure') setDraft(null);
        setMessage(result.job.message);
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(errorText(e));
        await refresh();
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return <div className="suite-page dns-filter-page">
    <nav className="suite-page-tabs" aria-label="广告过滤分区">
      {([['records', '拦截记录'], ['rules', '订阅与规则'], ['allow', '放行列表']] as const).map(([key, label]) =>
        <a key={key} href={key === 'records' ? '#/filter' : '#/filter?tab=' + key}
          aria-current={tab === key ? 'page' : undefined}>{label}</a>)}
    </nav>
    {(error || readError) && <Notice tone="error">{error || readError} <button onClick={() => void refresh()}>重新读取</button></Notice>}
    {message && <Notice>{message}</Notice>}
    {busy && <p aria-live="polite" className="suite-note">正在处理过滤任务…</p>}
    {!state ? <Empty title={readError ? 'DNS 过滤管理暂不可用' : '正在读取 DNS 过滤配置'} />
      : <>
        <div className="dns-filter-status"><div className="suite-button-row">
          <Status value={!readError && state.live_enabled && state.available && state.live_revision === state.revision ? 'good' : 'neutral'} label={(readError ? '上次读取 · ' : '') + filterStatus(state)} />
          {state.live_revision && <small>{state.rules.toLocaleString()} 条匹配规则</small>}
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}
            disabled={busy}><RefreshCw size={14} />刷新过滤状态</button>
        </div>
          {state.mode === 'recursive' && <Notice>当前使用纯递归入口。切换到 MosDNS 分流模式后可使用过滤。</Notice>}
          {state.mode === 'unavailable' && <Notice tone="warning">尚未连接支持过滤的 MosDNS 入口。</Notice>}
          {state.available && state.live_revision !== state.revision && <Notice tone="warning">解析核心与已保存的过滤版本不同，服务正在核对。</Notice>}
          {state.live_revision && tab === 'rules' && <small>活动版本 <code>{state.live_revision.slice(0, 12)}</code></small>}
        </div>
        <div className="dns-filter-rules" hidden={tab !== 'rules'}>
        <Section title="过滤设置" icon={Shield}>
          <FilterEditor state={state} busy={busy} apply={perform} />
        </Section>
        <Section title="过滤订阅" icon={ListFilter}>
          <NewSubscription disabled={busy || !state.available} apply={perform} />
          {state.draft && <Preview key={state.draft.id} draft={state.draft} disabled={busy || !state.available} apply={perform} />}
          {state.sources.map(item => <Subscription key={item.id + item.interval + item.enabled}
            source={item} live={!readError && state.available && state.live_enabled && state.live_revision === state.revision} disabled={busy || !state.available} apply={perform} />)}
          {!!state.jobs.length && <details className="suite-editor-help">
            <summary>最近过滤任务</summary><ul>{state.jobs.map(job =>
              <li key={job.id}>{job.phase === 'failed' ? '失败' : job.phase === 'pending' ? '处理中' : '完成'} · {job.message || job.action}</li>)}</ul>
          </details>}
        </Section>
        </div>
        {tab === 'allow' && <Section title="精确放行" icon={Shield}>
          <p className="suite-note">作用于整个实例。可以从已拦截的查询记录中添加放行。</p>
          {Object.entries(state.allow).length ? <ul className="dns-filter-allow">{Object.entries(state.allow).sort().map(([name, until]) =>
            <li key={name}><code>{name}</code><span>{until === null ? '永久' : '到期 ' + when(until)}</span>
              <button className="btn btn-secondary btn-sm" disabled={busy || !state.available}
                onClick={() => void perform({ action: 'allow', domain: name, mode: 'remove' })}>移除</button></li>)}</ul>
            : <p className="suite-note">暂无精确放行</p>}
        </Section>}
      </>}
    {tab === 'records' && <BlockedQueries />}
    {tab === 'records' && state && <details className="dns-filter-domain-tool"><summary>检查域名</summary><DomainCheck available={state.available} revision={state.live_revision} /></details>}
  </div>;
}

type Apply = (action: FilterAction) => Promise<void>;

function FilterEditor({ state, busy, apply }: { state: FilterState; busy: boolean; apply: Apply }) {
  const { draft, setDraft } = useFilterView();
  const { enabled, local, revision } = draft ?? { enabled: state.enabled, local: state.local_rules, revision: state.revision };
  const conflict = revision !== state.revision;
  function edit(nextEnabled: boolean, nextLocal: string) {
    setDraft(nextEnabled === state.enabled && nextLocal === state.local_rules ? null
      : { enabled: nextEnabled, local: nextLocal, revision });
  }
  return <form className="dns-filter-editor" onSubmit={e => { e.preventDefault(); if (draft && !conflict) void apply({ action: 'configure', enabled, local_rules: local }); }}>
    <label><input type="checkbox" checked={enabled} disabled={!state.available || busy} onChange={e => edit(e.target.checked, local)} /> 启用 DNS 去广告</label>
    <label>本地规则<textarea className="form-input" value={local} rows={5} maxLength={16 * 1024}
      spellCheck={false} disabled={!state.available || busy} onChange={e => edit(enabled, e.target.value)}
      placeholder={'ads.example.test\n||tracker.example.test^\n@@||safe.tracker.example.test^'} /></label>
    <p className="suite-note">支持精确域名、域名及子域、两端带星号的固定域名片段、域名例外和阻断 hosts 行。命中时返回 REFUSED。</p>
    {conflict && draft && <Notice tone="warning">编辑期间活动配置已变化。当前保存的开关为{state.enabled ? '开启' : '关闭'}。
      <pre>{state.local_rules || '当前没有本地规则'}</pre>
      <button type="button" onClick={() => setDraft({ ...draft, revision: state.revision })}>已核对，保留草稿</button>
      <button type="button" onClick={() => setDraft(null)}>使用当前配置</button></Notice>}
    <button className="btn btn-primary" disabled={!draft || conflict || busy || !state.available}>应用过滤设置</button>
  </form>;
}

function DomainCheck({ available, revision }: { available: boolean; revision: string | null }) {
  const [domain, setDomain] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FilterCheck | null>(null);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const check = async () => {
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setBusy(true);
    setError('');
    setResult(null);
    try { setResult(await checkFilter(domain.trim(), request.signal)); }
    catch (e) { if (!request.signal.aborted) setError(errorText(e)); }
    finally { if (!request.signal.aborted) setBusy(false); }
  };
  return <div className="dns-filter-check">
    <form className="dns-filter-inline" onSubmit={e => { e.preventDefault(); void check(); }}>
      <label>查询名<input className="form-input" value={domain} maxLength={254} placeholder="example.test"
        onChange={e => { setDomain(e.target.value); setResult(null); }} disabled={busy || !available} /></label>
      <button className="btn btn-secondary" disabled={busy || !available || !domain.trim()}>按当前规则测试</button>
    </form>
    {error && <Notice tone="error">{error}</Notice>}
    {result && <Notice>{result.revision === revision ? '当前规则判定' : '此前规则判定'} · {filterLabels[result.action]}
      {result.rule && <> · <code>{result.rule}</code> · {result.source}</>}
      {result.revision && <> · 版本 <code>{result.revision.slice(0, 12)}</code></>}
    </Notice>}
  </div>;
}

function Interval({ value, disabled, onChange }: { value: number; disabled: boolean; onChange: (v: number) => void }) {
  return <label>刷新周期<select className="form-select" value={value} disabled={disabled}
    onChange={event => onChange(Number(event.target.value))}>
    {intervals.map(([seconds, label]) => <option key={seconds} value={seconds}>{label}</option>)}
  </select></label>;
}

function NewSubscription({ disabled, apply }: { disabled: boolean; apply: Apply }) {
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [interval, setInterval] = useState(86400);
  return <form className="dns-filter-inline dns-filter-new" onSubmit={e => {
    e.preventDefault();
    void apply({ action: 'preview', name: name.trim(), url: url.trim(), interval });
  }}>
    <label>订阅名称<input className="form-input" value={name} placeholder="我的去广告列表" maxLength={64} onChange={e => setName(e.target.value)} disabled={disabled} required /></label>
    <label>订阅地址<input className="form-input" type="url" value={url} placeholder="https://…" maxLength={2048} onChange={e => setUrl(e.target.value)} disabled={disabled} required /></label>
    <Interval value={interval} disabled={disabled} onChange={setInterval} />
    <button className="btn btn-secondary" disabled={disabled || !name.trim() || !url.trim()}>获取预览</button>
  </form>;
}

function Preview({ draft, disabled, apply }: { draft: NonNullable<FilterState['draft']>; disabled: boolean; apply: Apply }) {
  const [ack, setAck] = useState(false);
  const counts = draft.parse;
  const partial = !!(counts.invalid || counts.unsupported);
  const blocked = !!counts.protected || !counts.valid;
  return <div className="dns-filter-preview">
    <h3>{draft.name} · {draft.host}</h3>
    <p>有效 {counts.valid} · 重复 {counts.duplicates} · 注释或空行 {counts.ignored} · 无效 {counts.invalid} · 不支持 {counts.unsupported} · 阻止导入 {counts.protected}</p>
    {counts.issues.length > 0 && <details open><summary>查看规则行及原因（最多 100 条）</summary>
      <ul>{counts.issues.map(issue => <li key={issue.line}>第 {issue.line} 行 · {reasons[issue.reason] ?? issue.reason}
        <code>{issue.rule}{issue.truncated ? '…' : ''}</code></li>)}</ul>
    </details>}
    {blocked && <Notice tone="warning">该版本无法启用。请检查例外语义和有效规则数量。</Notice>}
    {partial && !blocked && <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} /> 接受部分导入有效规则</label>}
    <button className="btn btn-primary" disabled={disabled || blocked || (partial && !ack)}
      onClick={() => void apply({ action: 'apply_preview', preview: draft.id, partial: ack })}>应用订阅</button>
  </div>;
}

function Subscription({ source, live, disabled, apply }: { source: FilterSource; live: boolean; disabled: boolean; apply: Apply }) {
  const [interval, setInterval] = useState(source.interval);
  const [enabled, setEnabled] = useState(source.enabled);
  const [confirm, setConfirm] = useState(false);
  const save = (remove: boolean) => void apply({ action: 'subscription', id: source.id, enabled, interval, remove });
  return <div className="dns-filter-subscription">
    <h3>{source.name}</h3>
    <p className="suite-note">{!source.enabled ? '已停用' : live ? '已生效' : '已配置'} · {source.parse.valid.toLocaleString()} 条有效 · {source.parse.invalid + source.parse.unsupported} 条跳过</p>
    <details><summary>订阅详情</summary><p className="suite-note">{source.host} · 更新于 {when(source.updated)} · 无效 {source.parse.invalid} · 不支持 {source.parse.unsupported} · 重复 {source.parse.duplicates}</p></details>
    <div className="dns-filter-inline">
      <label><input type="checkbox" checked={enabled} disabled={disabled} onChange={e => setEnabled(e.target.checked)} /> 已启用</label>
      <Interval value={interval} disabled={disabled} onChange={setInterval} />
      <button className="btn btn-secondary btn-sm" disabled={disabled || enabled === source.enabled && interval === source.interval}
        onClick={() => save(false)}>保存订阅设置</button>
      <button className="btn btn-secondary btn-sm" disabled={disabled}
        onClick={() => void apply({ action: 'refresh', id: source.id })}>刷新订阅</button>
      <button className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => setConfirm(!confirm)}>移除</button>
    </div>
    {confirm && <Notice tone="warning">移除此订阅会重新计算域名拦截。
      <button className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => save(true)}>确认移除</button>
    </Notice>}
  </div>;
}
