import { useCallback, useEffect, useState } from 'react';
import { errorText, softwareUpdateNetwork } from '../suiteApi';
import type { UpdateNetwork } from '../suiteApi';
import { Notice } from './SuiteUI';
import { useUnsavedChanges } from '../unsaved';

type Draft = { mode: UpdateNetwork['mode']; host: string; port: string; username: string; password: string; clear_password: boolean };
const draftOf = (value: UpdateNetwork): Draft => ({ mode: value.mode, host: value.host, username: value.username,
  port: value.port?.toString() ?? '', password: '', clear_password: false });

export default function SoftwareUpdateNetwork({ busy, onDraftChange, onSaved }: {
  busy: boolean; onDraftChange: (dirty: boolean) => void; onSaved: () => Promise<void>;
}) {
  const [saved, setSaved] = useState<UpdateNetwork | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const dirty = !!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(draftOf(saved));
  useUnsavedChanges(dirty);
  useEffect(() => { onDraftChange(dirty || saving); }, [dirty, saving, onDraftChange]);
  const load = useCallback((signal?: AbortSignal) => {
    return softwareUpdateNetwork(undefined, signal).then(value => {
      if (!signal?.aborted) { setSaved(value); setDraft(draftOf(value)); setError(''); }
    }).catch(e => { if (!signal?.aborted) setError(errorText(e)); });
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    void load(abort.signal);
    return () => abort.abort();
  }, [load]);
  const save = async () => {
    if (!draft || busy || saving) return;
    setSaving(true); setError('');
    try {
      const value = await softwareUpdateNetwork(draft.mode === 'direct'
        ? { mode: 'direct', host: '', port: null, username: '', clear_password: true }
        : { ...draft, port: Number(draft.port) });
      setSaved(value); setDraft(draftOf(value));
      await onSaved();
    } catch (e) { setError(errorText(e)); }
    finally { setSaving(false); }
  };
  const edit = (value: Partial<Draft>) => setDraft(current => current && { ...current, ...value });
  return <details className="suite-update-network suite-debug">
    <summary>更新网络 · {saved ? saved.mode === 'direct' ? '直连' : saved.mode === 'http' ? 'HTTP 代理' : 'SOCKS5 代理' : '正在读取'}</summary>
    <p className="suite-muted">仅软件版本检查、校验文件和安装包下载使用此设置。SOCKS5 由代理解析 GitHub 域名。保存后会检查官方发行版，连接失败会保留错误结果。</p>
    {draft && <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy || saving}>
        <label>连接方式<select value={draft.mode} onChange={event => edit({ mode: event.target.value as Draft['mode'] })}>
          <option value="direct">直连</option><option value="http">HTTP 代理（CONNECT）</option><option value="socks5">SOCKS5 代理</option>
        </select></label>
        {draft.mode !== 'direct' && <div className="suite-update-proxy-fields">
          <label>代理 IP<input required autoComplete="off" value={draft.host} placeholder="例如 192.0.2.10" onChange={event => edit({ host: event.target.value })} /></label>
          <label>端口<input required type="number" min="1" max="65535" step="1" value={draft.port} onChange={event => edit({ port: event.target.value })} /></label>
          <label>账号（可选）<input autoComplete="off" maxLength={255} value={draft.username} onChange={event => edit({ username: event.target.value })} /></label>
          <label>密码（可选）<input type="password" autoComplete="new-password" maxLength={255} value={draft.password}
            placeholder={saved?.password_configured ? '已保存；同一地址和账号下留空保留' : '未设置'} onChange={event => edit({ password: event.target.value, clear_password: false })} /></label>
          {saved?.password_configured && <label className="suite-update-clear-password"><input type="checkbox" checked={draft.clear_password}
            onChange={event => edit({ clear_password: event.target.checked, password: '' })} />清除已保存的认证信息</label>}
        </div>}
        {dirty && <p className="suite-muted">更新网络有未保存的修改。请保存或还原后再检查、安装更新；切换地址或账号时需重新填写密码。</p>}
        <div className="suite-update-network-actions"><button className="btn btn-secondary" type="submit">{saving ? '正在保存…' : '保存并检查更新'}</button>
          {dirty && <button className="btn btn-secondary" type="button" onClick={() => saved && setDraft(draftOf(saved))}>还原修改</button>}</div>
      </fieldset>
    </form>}
    {error && <Notice tone="error">{error}{!draft && <button className="btn btn-secondary" onClick={() => void load()}>重新读取设置</button>}</Notice>}
  </details>;
}
