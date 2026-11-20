import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dirtyFileNames, subscribeFileDraft } from '../fileDrafts';
import { RefreshCw } from 'lucide-react';
import { suiteFetch, errorText } from '../suiteApi';
import { useSuite } from '../suiteContext';
import { isActiveTask, useMaintenance } from '../maintenanceContext';
import { configFileAction, configFileLabel, configFileLink } from '../configFiles';

type Row = { filename: string; revision: string; state: 'unknown' | 'recorded' | 'changed'; recorded_at: number | null; action: string | null };
type State = { files: Row[]; scope: string; sampled_at: number; verified: false };
const LABELS = { unknown: '本次启动后暂无应用记录', recorded: '与应用记录一致', changed: '已保存修改 · 待处理' };
const subscribeDrafts = (listener: () => void) => subscribeFileDraft('*', listener);

export default function ConfigState({ compact = false }: { compact?: boolean }) {
  const { caps, paused } = useSuite();
  const { tasks } = useMaintenance();
  const applying = tasks.some(task => task.action !== 'save_redis_snapshot' && isActiveTask(task));
  const drafts = useSyncExternalStore(subscribeDrafts, dirtyFileNames);
  const [value, setValue] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const refreshQueued = useRef<(() => void) | null>(null);
  const read = useCallback(async (signal?: AbortSignal) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try {
      const data = await suiteFetch<State>('/api/v1/config/state', { signal });
      if (!signal?.aborted) { setValue(data); setError(''); }
    } catch (e) { if (!signal?.aborted) { setValue(null); setError(errorText(e)); } }
    finally {
      inFlight.current = false;
      if (!signal?.aborted) {
        setBusy(false);
        const next = refreshQueued.current;
        refreshQueued.current = null;
        next?.();
      }
    }
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { if (!document.hidden) await read(abort.signal); if (!abort.signal.aborted) timer = setTimeout(poll, 30000); };
    const invalidate = () => {
      if (inFlight.current) refreshQueued.current = () => void read(abort.signal);
      else void read(abort.signal);
    };
    window.addEventListener('lovestdns-config-changed', invalidate);
    if (!paused) void poll();
    return () => { abort.abort(); refreshQueued.current = null; clearTimeout(timer); window.removeEventListener('lovestdns-config-changed', invalidate); };
  }, [paused, read]);
  const draftNames = drafts.split(',');
  const rows = (items: Row[]) => items.map((row) => <div className="suite-config-version" key={row.filename}>
    <strong title={row.filename}>{configFileLabel(row.filename)}</strong><span className={row.state === 'changed' ? 'is-warning' : ''}>{draftNames.includes(row.filename) ? '有未保存草稿' : LABELS[row.state]}</span>
    <small>{row.state === 'changed' ? configFileAction(row.filename, caps?.deployment === 'native') : row.recorded_at ? `上次任务记录 ${new Date(row.recorded_at * 1000).toLocaleString('zh-CN')}` : '可查询目标域名进行验证'} · <a href={configFileLink(row.filename)}>查看配置</a></small>
  </div>);
  if (compact) {
    let label = '读取配置状态…';
    let href = '#/config';
    if (applying) return null;
    if (drafts) label = '查看未保存草稿';
    else if (error) return <p className="suite-note">暂时无法核对配置记录。<button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void read()}>重新核对</button></p>;
    else if (value) {
      const changed = value.files.filter((row) => row.state === 'changed').length;
      if (!changed) {
        const unknown = value.files.filter((row) => row.state === 'unknown').length;
        const recorded = value.files.length - unknown;
        const summary = !recorded ? '本次启动后暂无配置应用记录。'
          : unknown ? `${recorded} 项配置与应用记录一致，${unknown} 项暂无本次启动的应用记录。` : '配置与应用记录一致。';
        return <p className="suite-note">{summary} <a href="#/config">查看配置记录</a></p>;
      }
      label = `处理 ${changed} 项已保存修改`;
      href = '#/config?tab=changes';
    }
    return <div className="suite-config-summary"><a className="btn btn-secondary btn-sm" href={href}>{label}</a></div>;
  }
  const changed = value?.files.filter(row => row.state === 'changed') ?? [];
  const rest = value?.files.filter(row => row.state !== 'changed') ?? [];
  return <div className="card suite-config-state" id="config-changes" tabIndex={-1}><div className="card-title">配置版本与应用记录<button className="btn btn-secondary btn-sm" aria-label="刷新配置版本" disabled={busy} onClick={() => void read()}><RefreshCw size={13} />刷新</button></div>
    <p className="suite-note">状态由服务器记录，其他页面和设备可读取。版本一致不代表每条域名规则已验证；文件监听自动加载仍需查询确认。</p>
    {applying && <p role="status">任务完成后自动核对配置记录。</p>}
    {error && !applying && <p role="status">暂时无法核对配置版本。{error}</p>}
    {!value && !error && !applying && <p role="status">读取配置版本…</p>}
    {value && <>{!value.files.length && <p className="suite-note">尚无配置版本记录。</p>}{rows(changed)}{rows(rest.slice(0, 3))}{rest.length > 3 && <details><summary>其他配置（{rest.length - 3}）</summary>{rows(rest.slice(3))}</details>}</>}
  </div>;
}
