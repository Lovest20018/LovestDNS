import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { suiteFetch, errorText } from '../suiteApi';

type Row = { filename: string; revision: string; state: 'unknown' | 'recorded' | 'changed'; recorded_at: number | null; action: string | null };
type State = { files: Row[]; scope: string; sampled_at: number; verified: false };
const LABELS = { unknown: '尚无当前进程的确认记录', recorded: '与成功任务记录一致', changed: '文件已改变，待应用确认' };

export default function ConfigState() {
  const [value, setValue] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const read = useCallback(async (signal?: AbortSignal) => {
    setBusy(true);
    try {
      const data = await suiteFetch<State>('/api/v1/config/state', { signal });
      if (!signal?.aborted) { setValue(data); setError(''); }
    } catch (e) { if (!signal?.aborted) { setValue(null); setError(errorText(e)); } }
    finally { if (!signal?.aborted) setBusy(false); }
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await read(abort.signal); if (!abort.signal.aborted) timer = setTimeout(poll, 5000); };
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [read]);
  const rows = (items: Row[]) => items.map((row) => <div className="suite-config-version" key={row.filename}>
    <code>{row.filename}</code><span className={row.state === 'changed' ? 'is-warning' : ''}>{LABELS[row.state]}</span>
    <small>{row.recorded_at ? `${row.action === 'restart_dns_core' ? '重启' : '应用'}记录：${new Date(row.recorded_at * 1000).toLocaleString('zh-CN')}` : '通过配置中心成功应用或重启后建立记录'}</small>
  </div>);
  return <div className="card suite-config-state"><div className="card-title">配置版本与应用记录<button className="btn btn-secondary btn-sm" aria-label="刷新配置版本" disabled={busy} onClick={() => void read()}><RefreshCw size={13} />刷新</button></div>
    <p className="suite-note">状态由服务器记录，其他页面和设备可读取。版本一致不代表每条域名规则已验证；文件监听自动加载仍需查询确认。</p>
    {error && <p role="status">配置版本暂不可用：{error}</p>}
    {!value && !error && <p role="status">读取配置版本…</p>}
    {value && <>{rows(value.files.slice(0, 3))}<details><summary>其他规则文件（{value.files.length - 3}）</summary>{rows(value.files.slice(3))}</details><p className="suite-note">进程更换后旧记录显示待确认；升级、回退和自动重载可能改变进程。<a href="#/diagnostics">查询目标域名验证</a></p></>}
  </div>;
}
