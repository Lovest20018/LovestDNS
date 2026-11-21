import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ListFilter, Plus, RefreshCw, Shield } from 'lucide-react';
import { checkFilter, filterLabels, filterPreviewHref } from '../filterApi';
import type { FilterAction, FilterCheck, FilterSource, FilterSourceType, FilterState } from '../filterApi';
import { errorText, when } from '../suiteApi';
import { Empty, Notice, Section, Status } from '../components/SuiteUI';
import BlockedQueries from '../components/BlockedQueries';
import { filterStatus, useFilterView } from '../filterContext';
import './SuitePage.css';
import './SuiteFiltering.css';

const intervals = [[0, '仅手动'], [21600, '每 6 小时'], [86400, '每天'], [604800, '每周']] as const;
const LOCAL_RULES_BYTES = 16 * 1024;
const reasons: Record<string, string> = {
  pattern: '复杂匹配表达式', cosmetic: '元素隐藏规则', modifier: '扩展修饰符',
  hosts_rewrite: '指定 IP 的重写', invalid_domain: '域名格式无效', line_too_long: '规则行超长',
  exception_or_badfilter: '无法保留的例外或禁用规则', preprocessing_directive: '预处理指令',
};

function readRoute() {
  const params = new URLSearchParams(window.location.hash.split('?')[1]);
  const value = params.get('tab');
  const tab = value === 'rules' || value === 'allow' ? value : 'records';
  return { tab, check: params.get('check') ?? '', preview: params.get('preview') };
}

export default function SuiteFiltering() {
  const { state, error: readError, refresh, apply, task, acknowledgeTask } = useFilterView();
  const [{ tab, check, preview }, setRoute] = useState(readRoute);
  const [sourceType, setSourceType] = useState<FilterSourceType | 'all'>('all');
  const busy = task?.phase === 'pending' || !!state?.jobs.some(job => job.phase === 'pending');
  const [error, setError] = useState('');

  useEffect(() => {
    const sync = () => setRoute(readRoute());
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);

  const perform = async (action: FilterAction) => {
    setError('');
    try {
      return (await apply(action)).job.phase === 'succeeded';
    } catch (e) {
      setError(errorText(e));
      return false;
    }
  };
  const draft = state?.draft;
  const showPreview = !!draft && tab !== 'records';
  const actionStatus = useId();
  const blockedReason = task?.phase === 'uncertain' ? '上一次过滤任务结果待核对，请核对后恢复操作。'
    : busy ? '当前有过滤任务执行中，完成后可继续操作。'
      : !state?.available ? '当前 DNS 入口不支持过滤或连接暂不可用。' : '';
  const disabledBy = blockedReason ? actionStatus : undefined;
  const live = !readError && !!state?.available && state.live_enabled && state.live_revision === state.revision;
  const sources = state?.sources ?? [];
  const allowCount = sources.filter(source => source.type === 'allow').length;
  const sourceOptions = [
    ['all', '全部', sources.length], ['block', '拦截', sources.length - allowCount], ['allow', '白名单', allowCount],
  ] as const;

  return <div className="suite-page dns-filter-page">
    <nav className="suite-page-tabs" aria-label="广告过滤分区">
      {([['records', '拦截记录'], ['rules', '订阅与规则'], ['allow', '放行列表']] as const).map(([key, label]) =>
        <a key={key} href={key === 'records' ? '#/filter' : '#/filter?tab=' + key}
          aria-current={tab === key ? 'page' : undefined}>{label}</a>)}
    </nav>
    {(readError || !task && error) && <Notice tone="error">{readError || error} <button onClick={() => void refresh()}>重新读取</button></Notice>}
    {task && <div aria-live="polite"><Notice tone={task.phase === 'failed' ? 'error' : task.phase === 'uncertain' ? 'warning' : undefined}>
      {task.message}{task.id && <small> · 任务 <code>{task.id}</code></small>}
      {task.phase === 'uncertain' && <>
        <button onClick={() => void refresh()}>重新读取</button>
        <button disabled={busy || !!readError} onClick={() => { acknowledgeTask(); setError(''); }}>已核对最近任务，恢复操作</button>
      </>}
    </Notice></div>}
    {busy && !task && <p aria-live="polite" className="suite-note">正在处理过滤任务…</p>}
    {!state ? <Empty title={readError ? 'DNS 过滤管理暂不可用' : '正在读取 DNS 过滤配置'} />
      : <>
        <div className="dns-filter-status"><div className="dns-filter-statusbar">
          <Status value={!readError && state.live_enabled && state.available && state.live_revision === state.revision ? 'good' : 'neutral'} label={(readError ? '上次读取 · ' : '') + filterStatus(state)} />
          {state.live_revision && <span className="dns-filter-count"><strong>{state.rules.toLocaleString()}</strong> 条匹配规则</span>}
          {state.live_revision && tab === 'rules' && <small>活动版本 <code>{state.live_revision.slice(0, 12)}</code></small>}
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}
            disabled={busy}><RefreshCw size={14} />刷新过滤状态</button>
        </div>
          {state.mode === 'recursive' && <Notice>当前使用纯递归入口。切换到 MosDNS 分流模式后可使用过滤。</Notice>}
          {state.mode === 'unavailable' && <Notice tone="warning">尚未连接支持过滤的 MosDNS 入口。</Notice>}
          {state.available && state.live_revision !== state.revision && <Notice tone="warning">解析核心与已保存的过滤版本不同，服务正在核对。</Notice>}
        </div>
        {draft && showPreview && <div className="dns-filter-review">
          <p>订阅待复核 · <a href={filterPreviewHref(draft, tab)}>查看{draft.name}的预览</a></p>
          <Preview key={draft.id} draft={draft} focus={preview === draft.id} disabledBy={disabledBy} apply={perform} />
        </div>}
        {preview && (!draft || preview !== draft.id) && <Notice tone="warning">该订阅预览已失效或已处理。</Notice>}
        {tab === 'records' && task?.phase === 'uncertain' && <FilterJobs state={state} expanded />}
        {tab === 'rules' && <div className="dns-filter-rules">
        <Section title="过滤设置" icon={Shield}>
          <FilterEditor state={state} blockedReason={blockedReason} statusId={actionStatus} apply={perform} />
        </Section>
        <Section title="过滤订阅" icon={ListFilter}>
          <div className="suite-segment" role="group" aria-label="筛选订阅类型">
            {sourceOptions.map(([type, label, count]) => <button key={type} type="button" aria-pressed={sourceType === type}
              onClick={() => setSourceType(type)}>{label} ({count})</button>)}
          </div>
          {sources.filter(item => sourceType === 'all' || (item.type ?? 'block') === sourceType)
            .map(item => <Subscription key={item.id + item.interval + item.enabled}
              source={item} live={live} disabledBy={disabledBy} apply={perform} />)}
          {sourceType !== 'all' && !sourceOptions.find(([type]) => type === sourceType)?.[2]
            && <p className="suite-note">暂无这类订阅。</p>}
          <NewSubscription initiallyOpen={!sources.length && !draft} disabledBy={disabledBy} apply={perform} />
          <FilterJobs state={state} expanded={task?.phase === 'uncertain'} />
        </Section>
        </div>}
        {tab === 'allow' && <>
          <Section title="精确放行" icon={Shield}>
            <ManualAllow state={state} blockedReason={blockedReason} statusId={actionStatus} apply={perform} />
          </Section>
          <Section title="白名单订阅" icon={ListFilter}>
            {sources.filter(item => item.type === 'allow').map(item => <Subscription
              key={item.id + item.interval + item.enabled} source={item} live={live} disabledBy={disabledBy} apply={perform} />)}
            <NewSubscription type="allow" initiallyOpen={false} disabledBy={disabledBy} apply={perform} />
            <FilterJobs state={state} expanded={task?.phase === 'uncertain'} />
          </Section>
        </>}
      </>}
    {tab === 'records' && state && <details key={check} className="dns-filter-domain-tool" open={!!check}>
      <summary>检查当前规则</summary>
      <DomainCheck initialDomain={check} available={state.available} revision={state.live_revision} />
    </details>}
    {tab === 'records' && <BlockedQueries />}
  </div>;
}

type Apply = (action: FilterAction) => Promise<boolean>;

function FilterJobs({ state, expanded }: { state: FilterState; expanded: boolean }) {
  if (!state.jobs.length) return null;
  return <details className="dns-filter-jobs" open={expanded || undefined}>
    <summary>最近过滤任务</summary><ul>{state.jobs.map(job =>
      <li key={job.id}>{job.phase === 'failed' ? '失败' : job.phase === 'pending' ? '处理中' : '完成'} · {job.message || job.action} · <code>{job.id}</code></li>)}</ul>
  </details>;
}

function ManualAllow({ state, blockedReason, statusId, apply }: {
  state: FilterState; blockedReason: string; statusId: string; apply: Apply;
}) {
  const [domain, setDomain] = useState('');
  const [mode, setMode] = useState<'temporary' | 'permanent'>('temporary');
  const [search, setSearch] = useState('');
  const disabled = !!blockedReason;
  const entries = Object.entries(state.allow).sort(([a], [b]) => a.localeCompare(b));
  const visible = entries.filter(([name]) => name.toLowerCase().includes(search.trim().toLowerCase()));
  return <>
    <p className="suite-note">精确匹配此域名，作用于整个实例。如需覆盖子域名，可在<a href="#/filter?tab=rules">本地规则</a>中添加域名例外。</p>
    <form className="dns-filter-inline" onSubmit={async event => {
      event.preventDefault();
      if (!disabled && domain.trim() && await apply({ action: 'allow', domain: domain.trim(), mode })) setDomain('');
    }}>
      <label>放行域名<input className="form-input" value={domain} maxLength={254} placeholder="example.test"
        onChange={event => setDomain(event.target.value)} disabled={disabled} required /></label>
      <label>时效<select className="form-select" value={mode} disabled={disabled}
        onChange={event => setMode(event.target.value as typeof mode)}>
        <option value="temporary">10 分钟</option><option value="permanent">永久</option>
      </select></label>
      <button className="btn btn-primary" disabled={disabled || !domain.trim()} aria-describedby={statusId}>添加放行</button>
    </form>
    <p className="suite-note" id={statusId} role="status">{blockedReason || (!domain.trim() ? '输入域名后可添加放行。' : '')}</p>
    {(entries.length > 5 || search) && <div className="dns-filter-allow-search">
      <label>搜索放行域名<input className="form-input" type="search" value={search}
        onChange={event => setSearch(event.target.value)} /></label>
      {search && <button type="button" className="btn btn-secondary btn-sm" onClick={() => setSearch('')}>清除搜索</button>}
    </div>}
    {visible.length ? <ul className="dns-filter-allow">{visible.map(([name, until]) =>
      <li key={name}>
        <div><code>{name}</code><small>{until === null ? '永久' : '到期 ' + when(until)}</small></div>
        <div className="suite-button-row">
          {until !== null && <button type="button" className="btn btn-secondary btn-sm" disabled={disabled}
            aria-describedby={disabled ? statusId : undefined}
            aria-label={`将 ${name} 转为永久`}
            onClick={() => void apply({ action: 'allow', domain: name, mode: 'permanent' })}>转为永久</button>}
          <button type="button" className="btn btn-secondary btn-sm" disabled={disabled} aria-describedby={disabled ? statusId : undefined}
            onClick={() => void apply({ action: 'allow', domain: name, mode: 'remove' })}>移除</button>
        </div>
      </li>)}</ul>
      : <p className="suite-note">{entries.length ? '没有匹配的放行域名。' : '暂无精确放行'}</p>}
  </>;
}

function FilterEditor({ state, blockedReason, statusId, apply }: {
  state: FilterState; blockedReason: string; statusId: string; apply: Apply;
}) {
  const { draft, setDraft } = useFilterView();
  const { enabled, local, revision } = draft ?? { enabled: state.enabled, local: state.local_rules, revision: state.revision };
  const conflict = revision !== state.revision;
  const ruleLines = local.split('\n').filter(line => line.trim() && !/^\s*[#!]/.test(line)).length;
  const characters = Array.from(local).length;
  const bytes = new TextEncoder().encode(local).length;
  const tooLarge = bytes > LOCAL_RULES_BYTES;
  const reason = blockedReason || (conflict ? '请先核对活动版本，再应用草稿。' : tooLarge ? '本地规则已超出容量，请删减后应用。'
    : !draft ? '无待应用修改' : '');
  function edit(nextEnabled: boolean, nextLocal: string) {
    setDraft(nextEnabled === state.enabled && nextLocal === state.local_rules ? null
      : { enabled: nextEnabled, local: nextLocal, revision });
  }
  return <form className="dns-filter-editor" onSubmit={e => { e.preventDefault(); if (!reason) void apply({ action: 'configure', enabled, local_rules: local }); }}>
    <label className="dns-filter-switch">启用 DNS 去广告<input type="checkbox" checked={enabled} disabled={!!blockedReason} onChange={e => edit(e.target.checked, local)} /></label>
    <label>本地规则<textarea className="form-input" value={local} rows={5} maxLength={LOCAL_RULES_BYTES}
      spellCheck={false} disabled={!!blockedReason} onChange={e => edit(enabled, e.target.value)}
      placeholder={'ads.example.test\n||tracker.example.test^\n@@||safe.tracker.example.test^'} /></label>
    <details className="dns-filter-help"><summary>支持的规则语法</summary><p className="suite-note">支持精确域名、域名及子域、两端带星号的固定域名片段、域名例外和阻断 hosts 行。命中时返回 REFUSED。</p></details>
    {conflict && draft && <Notice tone="warning">编辑期间活动配置已变化。当前保存的开关为{state.enabled ? '开启' : '关闭'}。
      <pre>{state.local_rules || '当前没有本地规则'}</pre>
      <button type="button" onClick={() => setDraft({ ...draft, revision: state.revision })}>已核对，保留草稿</button>
      <button type="button" onClick={() => setDraft(null)}>使用当前配置</button></Notice>}
    <div className="dns-filter-editor-footer">
      <div className="dns-filter-rule-stats" data-over-limit={tooLarge}>
        <span>规则行 {ruleLines.toLocaleString()} 条 · {characters.toLocaleString()} 字符</span>
        <label>UTF-8 容量 {bytes.toLocaleString()} / {LOCAL_RULES_BYTES.toLocaleString()} 字节
          <progress value={Math.min(bytes, LOCAL_RULES_BYTES)} max={LOCAL_RULES_BYTES} />
        </label>
      </div>
      <span id={statusId} role="status">{reason || '有未应用的修改'}</span>
      <button className="btn btn-primary btn-sm" disabled={!!reason} aria-describedby={statusId}>应用过滤设置</button>
    </div>
  </form>;
}

function DomainCheck({ initialDomain, available, revision }: { initialDomain: string; available: boolean; revision: string | null }) {
  const [domain, setDomain] = useState(initialDomain);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FilterCheck | null>(null);
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const check = useCallback(async (name: string) => {
    const request = new AbortController();
    controller.current?.abort();
    controller.current = request;
    setBusy(true);
    setError('');
    setResult(null);
    try {
      const value = await checkFilter(name.trim(), request.signal);
      if (!request.signal.aborted) setResult(value);
    }
    catch (e) { if (!request.signal.aborted) setError(errorText(e)); }
    finally { if (!request.signal.aborted) setBusy(false); }
  }, []);
  useEffect(() => {
    if (!initialDomain || !available) return;
    const start = window.setTimeout(() => {
      heading.current?.focus();
      void check(initialDomain);
    }, 0);
    return () => window.clearTimeout(start);
  }, [available, initialDomain, check]);
  return <div className="dns-filter-check">
    {initialDomain && <h3 ref={heading} tabIndex={-1}>检查 {initialDomain} 的当前规则</h3>}
    <form className="dns-filter-inline" onSubmit={e => { e.preventDefault(); void check(domain); }}>
      <label>查询名<input className="form-input" value={domain} maxLength={254} placeholder="example.test"
        onChange={e => { setDomain(e.target.value); setResult(null); }} disabled={busy || !available} /></label>
      <button className="btn btn-secondary" disabled={busy || !available || !domain.trim()}>按当前规则测试</button>
    </form>
    {error && <Notice tone="error">{error}</Notice>}
    {result && <Notice>{result.revision === revision ? '当前规则判定' : '此前规则判定'} · {filterLabels[result.action]}
      {result.rule && <> · <code>{result.rule}</code> · {result.source}</>}
      {result.source_id && <> · 订阅 <code>{result.source_id}</code></>}
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

function NewSubscription({ type, initiallyOpen, disabledBy, apply }: {
  type?: FilterSourceType; initiallyOpen: boolean; disabledBy?: string; apply: Apply;
}) {
  const { subscriptionDraft, setSubscriptionDraft } = useFilterView();
  const draft = subscriptionDraft ?? { name: '', url: '', interval: 86400, type: type ?? 'block' };
  const { name, url, interval } = draft;
  const [open, setOpen] = useState(initiallyOpen || !!subscriptionDraft && (!type || draft.type === type));
  const status = useId();
  const disabled = !!disabledBy;
  const incomplete = !name.trim() || !url.trim();
  return <div className="dns-filter-add"><button className="btn btn-secondary btn-sm" type="button" aria-expanded={open}
    onClick={() => {
      if (!open && type) setSubscriptionDraft({ ...draft, type });
      setOpen(!open);
    }}><Plus size={14} />{open ? '收起添加表单' : type === 'allow' ? '添加白名单订阅' : '添加订阅'}</button>
    <form hidden={!open} className="dns-filter-inline dns-filter-new" onSubmit={async e => {
    e.preventDefault();
    if (!disabled && !incomplete && await apply({ action: 'preview', name: name.trim(), url: url.trim(), interval, type: draft.type })) setOpen(false);
  }}>
    <label>订阅类型<select className="form-select" value={draft.type} disabled={disabled}
      onChange={e => setSubscriptionDraft({ ...draft, type: e.target.value as FilterSourceType })}>
      <option value="block">拦截订阅</option><option value="allow">白名单订阅</option>
    </select></label>
    <label>订阅名称<input className="form-input" value={name} placeholder="我的 DNS 列表" maxLength={64} onChange={e => setSubscriptionDraft({ ...draft, name: e.target.value })} disabled={disabled} required /></label>
    <label>订阅地址<input className="form-input" type="url" value={url} placeholder="https://…" maxLength={2048} autoComplete="off" onChange={e => setSubscriptionDraft({ ...draft, url: e.target.value })} disabled={disabled} required /></label>
    <Interval value={interval} disabled={disabled} onChange={interval => setSubscriptionDraft({ ...draft, interval })} />
    {incomplete && !disabled && <p className="suite-note" id={status}>输入订阅名称和 HTTPS 地址后可获取预览。</p>}
    <button className="btn btn-secondary" disabled={disabled || incomplete} aria-describedby={disabledBy ?? (incomplete ? status : undefined)}>获取预览</button>
    {subscriptionDraft && <button type="button" className="btn btn-secondary" disabled={disabled} aria-describedby={disabledBy} onClick={() => {
      setSubscriptionDraft(null);
      setOpen(false);
    }}>清空草稿</button>}
  </form></div>;
}

function Preview({ draft, focus, disabledBy, apply }: {
  draft: NonNullable<FilterState['draft']>; focus: boolean; disabledBy?: string; apply: Apply;
}) {
  const [ack, setAck] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (focus) heading.current?.focus(); }, [focus]);
  const counts = draft.parse;
  const partial = !!(counts.invalid || counts.unsupported);
  const blocked = !!counts.protected || !counts.valid;
  const status = useId();
  const needsAck = partial && !ack;
  return <div className="dns-filter-preview">
    <div className="dns-source-heading"><h3 ref={heading} tabIndex={-1}>{draft.name}</h3><strong className="dns-review-label">待复核</strong></div>
    <small>{draft.type === 'allow' ? '白名单订阅' : '拦截订阅'} · {draft.host}</small>
    {draft.type === 'allow' && <p>精确域名 {counts.scopes?.allow_exact ?? 0} · 域名及子域 {counts.scopes?.allow_suffix ?? 0}</p>}
    <p>有效 {counts.valid} · 重复 {counts.duplicates} · 注释或空行 {counts.ignored} · 无效 {counts.invalid} · 不支持 {counts.unsupported} · 阻止导入 {counts.protected}</p>
    {counts.issues.length > 0 && <details open><summary>查看规则行及原因（最多 100 条）</summary>
      <ul>{counts.issues.map(issue => <li key={issue.line}>第 {issue.line} 行 · {reasons[issue.reason] ?? issue.reason}
        <code>{issue.rule}{issue.truncated ? '…' : ''}</code></li>)}</ul>
    </details>}
    {blocked && <div id={status}><Notice tone="warning">该版本无法启用。请检查例外语义和有效规则数量。</Notice></div>}
    {partial && !blocked && <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} /> 接受部分导入有效规则</label>}
    {needsAck && !blocked && !disabledBy && <p id={status} className="suite-note">确认部分导入后可应用订阅。</p>}
    <button className="btn btn-primary" disabled={!!disabledBy || blocked || needsAck}
      aria-describedby={disabledBy ?? (blocked || needsAck ? status : undefined)}
      onClick={() => void apply({ action: 'apply_preview', preview: draft.id, partial: ack })}>应用订阅</button>
  </div>;
}

function Subscription({ source, live, disabledBy, apply }: {
  source: FilterSource; live: boolean; disabledBy?: string; apply: Apply;
}) {
  const [interval, setInterval] = useState(source.interval);
  const [enabled, setEnabled] = useState(source.enabled);
  const [confirm, setConfirm] = useState(false);
  const status = useId();
  const disabled = !!disabledBy;
  const unchanged = enabled === source.enabled && interval === source.interval;
  const save = (remove: boolean) => void apply({ action: 'subscription', id: source.id, enabled, interval, remove });
  return <div className="dns-filter-subscription">
    <div className="dns-source-heading"><h3>{source.name}</h3>
      <Status value={source.enabled && live ? 'good' : 'neutral'} label={!source.enabled ? '已停用' : live ? '已生效' : '已配置'} /></div>
    <p className="dns-source-host">{source.type === 'allow' ? '白名单订阅' : '拦截订阅'} · {source.host}</p>
    <div className="dns-source-stats"><span><b>{source.parse.valid.toLocaleString()}</b> 条有效</span>
      <span><b>{(source.parse.invalid + source.parse.unsupported).toLocaleString()}</b> 条跳过</span>
      <span>{intervals.find(([value]) => value === source.interval)?.[1] ?? source.interval + ' 秒'}</span></div>
    <div className="dns-source-tools"><details><summary>订阅详情与设置</summary>
    <p className="suite-note">更新于 {when(source.updated)} · 无效 {source.parse.invalid} · 不支持 {source.parse.unsupported} · 重复 {source.parse.duplicates}</p>
    <div className="dns-filter-inline dns-source-settings">
      <label><input type="checkbox" checked={enabled} disabled={disabled} onChange={e => setEnabled(e.target.checked)} /> 已启用</label>
      <Interval value={interval} disabled={disabled} onChange={setInterval} />
      <button className="btn btn-secondary btn-sm" disabled={disabled || unchanged} aria-describedby={disabledBy ?? (unchanged ? status : undefined)}
        onClick={() => save(false)}>保存订阅设置</button>
      <button className="btn btn-secondary btn-sm" disabled={disabled} aria-describedby={disabledBy} onClick={() => setConfirm(!confirm)}>移除</button>
    </div>
    {unchanged && !disabled && <p className="suite-note" id={status}>订阅设置暂无修改。</p>}
    {confirm && <Notice tone="warning">移除此订阅会重新计算域名规则。
      <button className="btn btn-secondary btn-sm" disabled={disabled} aria-describedby={disabledBy} onClick={() => save(true)}>确认移除</button>
    </Notice>}</details>
    <button className="btn btn-secondary btn-sm" disabled={disabled} aria-describedby={disabledBy}
      onClick={() => void apply({ action: 'refresh', id: source.id })}><RefreshCw size={13} />刷新订阅</button></div>
  </div>;
}
