import { useEffect } from 'react';
import { useUpdateNetwork } from '../updateNetworkContext';
import type { UpdateNetworkDraft } from '../updateNetworkContext';
import { Notice } from './SuiteUI';

export default function SoftwareUpdateNetwork() {
  const { saved, draft, dirty, saving, message, error, load, edit, restore, save } = useUpdateNetwork();
  useEffect(() => {
    const abort = new AbortController();
    void load(abort.signal);
    return () => abort.abort();
  }, [load]);
  return <div className="suite-update-network">
    <p className="suite-muted">仅软件版本检查、校验文件和安装包下载使用此设置。SOCKS5 由代理解析 GitHub 域名。保存后会检查官方发行版，连接失败会保留错误结果。</p>
    {draft && <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset disabled={saving}>
        <label>更新通道<select value={draft.channel} onChange={event => edit({ channel: event.target.value as UpdateNetworkDraft['channel'] })}>
          <option value="stable">正式版（推荐）</option><option value="preview">包含预览版</option>
        </select></label>
        {draft.channel === 'preview' && <p className="suite-muted">预览通道可能包含尚在测试的新版本，确认升级前请阅读发行说明。</p>}
        <label>连接方式<select value={draft.mode} onChange={event => edit({ mode: event.target.value as UpdateNetworkDraft['mode'] })}>
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
          {dirty && <button className="btn btn-secondary" type="button" onClick={restore}>还原修改</button>}</div>
      </fieldset>
    </form>}
    {message && <p role="status">{message}</p>}
    {error && <Notice tone="error">{error}{!draft && <button className="btn btn-secondary" onClick={() => void load()}>重新读取设置</button>}</Notice>}
  </div>;
}
