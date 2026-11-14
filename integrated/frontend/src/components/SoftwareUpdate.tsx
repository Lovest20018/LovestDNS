import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Download, ExternalLink, RefreshCw } from 'lucide-react';
import { errorText, softwareUpdate } from '../suiteApi';
import type { SoftwareUpdateStatus } from '../suiteApi';
import { dirtyFileDrafts, subscribeFileDraft } from '../fileDrafts';
import { Notice, Section } from './SuiteUI';

const BUSY = new Set(['queued', 'checking', 'downloading', 'verifying', 'installing']);
const PHASES: Record<string, string> = { queued: '等待执行', checking: '检查更新中', downloading: '下载安装包',
  verifying: '校验安装包', installing: '备份并安装', succeeded: '更新完成', failed: '操作失败' };

export default function SoftwareUpdate({ loadedVersion }: { loadedVersion: string }) {
  const [versionAtMount] = useState(loadedVersion);
  const [status, setStatus] = useState<SoftwareUpdateStatus | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [confirmDigest, setConfirmDigest] = useState('');
  const epoch = useRef(0);
  const reading = useRef(false);
  const pollingActive = useRef(false);
  const unsaved = useSyncExternalStore(useCallback(listener => subscribeFileDraft('*', listener), []), dirtyFileDrafts);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (reading.current) return;
    reading.current = true;
    const requestEpoch = epoch.current;
    try {
      const value = await softwareUpdate(undefined, signal);
      if (!signal?.aborted && requestEpoch === epoch.current) {
        setStatus(value); setError(''); pollingActive.current = BUSY.has(value.phase ?? '');
      }
    } catch (e) { if (!signal?.aborted && requestEpoch === epoch.current) setError(errorText(e)); }
    finally { reading.current = false; }
  }, []);
  const phase = status?.phase ?? 'idle';
  const active = BUSY.has(phase);
  const jobId = status?.job_id;
  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden) await refresh(abort.signal);
      if (!abort.signal.aborted) timer = setTimeout(poll, document.hidden ? 30000 : pollingActive.current ? 2000 : 20000);
    };
    void poll();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [refresh, jobId]);
  const release = status?.release;
  const available = phase === 'ready' && !!release && release.version !== status?.current_version;
  const confirm = available && release?.archive_sha256 === confirmDigest;
  const submit = async (action: 'check' | 'apply') => {
    if (active || submitting || (action === 'apply' && (!available || unsaved))) return;
    epoch.current++;
    setSubmitting(true); setError(''); setConfirmDigest('');
    try {
      const value = await softwareUpdate(action === 'check' ? { action } : {
        action, version: release!.version, archive_sha256: release!.archive_sha256,
      });
      setStatus(value); pollingActive.current = BUSY.has(value.phase ?? '');
    } catch (e) {
      setError(`${errorText(e)}。若响应中断，请先重新读取状态，任务不会因关闭网页而取消。`);
      void refresh();
    } finally { setSubmitting(false); }
  };
  return <Section title="软件更新" subtitle="官方 GitHub 发行版 · Debian amd64" icon={Download}
    actions={<button className="btn btn-secondary" disabled={active || submitting || status?.supported === false}
      onClick={() => void submit('check')}><RefreshCw size={14} />{active && status?.action === 'check' ? '正在检查…' : '检查更新'}</button>}>
    {!status && !error && <p className="suite-muted">正在读取更新状态…</p>}
    {status?.supported === false && <Notice tone="info">{status.reason}</Notice>}
    {status?.supported && <>
      <div className="suite-software-versions"><div><span>当前版本</span><strong>{status.current_version}</strong></div>
        {release && <div><span>最新发布{release.prerelease ? ' · 预发布版' : ''}</span><strong>{release.version}</strong></div>}</div>
      <p className="suite-muted">安装前校验官方安装包并备份现有配置与数据。升级时 DNS 和网页会短暂重启，失败时尝试恢复原版本；关闭网页不会中断任务。</p>
      {(active || status.message) && <Notice tone={phase === 'failed' ? 'error' : 'info'}>
        {active && <strong>{PHASES[phase]} · </strong>}{status.message}
        {active && error && <span> 正在等待服务恢复，页面会继续读取结果。</span>}
      </Notice>}
      {status.checked_at && <p className="suite-muted">上次检查：{new Date(status.checked_at * 1000).toLocaleString()}</p>}
      {release && <details className="suite-debug"><summary>查看版本说明</summary><pre className="suite-update-notes">{release.notes || '发行版未提供补充说明。'}</pre>
        <a href={release.url} target="_blank" rel="noreferrer"><ExternalLink size={13} />打开官方发行页</a></details>}
      {available && <div className="suite-update-actions">
        {unsaved && <Notice tone="warning">有未保存的配置草稿，请先保存或还原，再更新软件。</Notice>}
        {confirm ? <div className="suite-update-confirm" role="group" aria-label="确认软件更新">
          <p>确认更新到 <strong>{release!.version}</strong>？{release!.prerelease && '这是预发布版本。'}升级会短暂重启 DNS 与网页服务。</p>
          <div><button className="btn btn-primary" disabled={submitting || unsaved} onClick={() => void submit('apply')}>确认更新并重启</button>
            <button className="btn btn-secondary" disabled={submitting} onClick={() => setConfirmDigest('')}>取消</button></div>
        </div> : <button className="btn btn-primary" disabled={submitting || unsaved} onClick={() => setConfirmDigest(release!.archive_sha256)}>更新到此版本</button>}
      </div>}
      {phase === 'succeeded' && status.current_version !== versionAtMount && <button className="btn btn-primary" onClick={() => window.location.reload()}>刷新页面使用新版本</button>}
    </>}
    {error && (!active || !status) && <Notice tone="error">{error}<button className="btn btn-secondary" disabled={submitting} onClick={() => void refresh()}>重新读取状态</button></Notice>}
  </Section>;
}
