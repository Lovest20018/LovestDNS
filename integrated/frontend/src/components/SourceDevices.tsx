import { useState } from 'react';
import { Network } from 'lucide-react';
import { diagnosticHash, parseDiagnosticRoute } from '../diagnosticRoute';
import { useSourceDevices } from '../sourceDevices';
import { errorText } from '../suiteApi';
import { Empty, Notice, Section } from './SuiteUI';
import './SourceDevices.css';

export function SourceAddress({ ip, name }: { ip: string; name?: string }) {
  return <span className="source-address">{name && <span>{name}<span aria-hidden="true"> · </span></span>}<code>{ip}</code></span>;
}

export function SourceDeviceEditor({ address, name = '', onClose }: { address?: string; name?: string; onClose?: () => void }) {
  const { save } = useSourceDevices();
  const [ip, setIp] = useState(address ?? '');
  const [draft, setDraft] = useState(name);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ text: string; error: boolean }>();
  const submit = async (value: string) => {
    setBusy(true); setFeedback(undefined);
    try {
      await save(ip.trim(), value.trim());
      if (onClose) onClose();
      else { setIp(''); setDraft(''); setFeedback({ text: '备注已保存', error: false }); }
    } catch (error) {
      setFeedback({ text: errorText(error), error: true });
    } finally { setBusy(false); }
  };
  return <form className="source-device-editor" onSubmit={event => { event.preventDefault(); if (draft.trim()) void submit(draft); }}>
    {!address && <label>来源 IP<input className="form-input" required maxLength={45} value={ip}
      placeholder="IPv4 或 IPv6" onChange={event => setIp(event.target.value)} /></label>}
    <label>备注名称<input className="form-input" required maxLength={64} value={draft}
      onChange={event => setDraft(event.target.value)} /></label>
    <div className="suite-button-row">
      <button className="btn btn-secondary btn-sm" disabled={busy || !draft.trim()}>保存备注</button>
      {address && name && <button type="button" className="btn btn-secondary btn-sm" disabled={busy}
        onClick={() => void submit('')}>清除备注</button>}
      {onClose && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onClose}>取消</button>}
    </div>
    {feedback && <Notice tone={feedback.error ? 'error' : undefined}>{feedback.text}</Notice>}
  </form>;
}

export default function SourceDevices() {
  const { devices, error, loaded, reload, allowed } = useSourceDevices();
  const [search, setSearch] = useState('');
  const [edit, setEdit] = useState('');
  if (!allowed) return <Notice tone="warning">管理来源设备需要配置权限。</Notice>;
  const term = search.trim().toLocaleLowerCase();
  const addresses = Object.entries(devices).sort(([left], [right]) => left.localeCompare(right))
    .filter(([ip, name]) => ip.includes(term) || name.toLocaleLowerCase().includes(term));
  return <Section title="来源设备" icon={Network}>
    <p className="suite-note">备注按 DNS 入口看到的 IP 保存。路由器代转时，共用地址的设备显示同一备注。</p>
    <SourceDeviceEditor />
    {error && <Notice tone="error">{error} <button type="button" className="btn btn-secondary btn-sm" onClick={reload}>重试</button></Notice>}
    <label className="source-device-search">搜索备注或 IP<input type="search" className="form-input" value={search}
      onChange={event => setSearch(event.target.value)} /></label>
    {addresses.length ? <ul className="source-device-list">{addresses.map(([ip, name]) => <li key={ip}>
      <a href={diagnosticHash({ ...parseDiagnosticRoute('#/diagnostics?tab=records'), source: ip })}><SourceAddress ip={ip} name={name} /></a>
      <button className="btn btn-secondary btn-sm" aria-expanded={edit === ip}
        onClick={() => setEdit(edit === ip ? '' : ip)}>编辑备注</button>
      {edit === ip && <SourceDeviceEditor address={ip} name={name} onClose={() => setEdit('')} />}
    </li>)}</ul> : <Empty title={loaded ? term ? '没有匹配的来源设备' : '尚无来源设备备注' : '正在读取来源设备'} />}
  </Section>;
}
