import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, History, RotateCcw } from 'lucide-react';
import { suiteFetch, errorText, when, bytes, ApiError } from '../suiteApi';
import type { Revision } from '../suiteApi';
import type { FileContent, SaveResult } from '../api';
import { configDiff } from '../configDiff';
import { Empty, Notice, Section } from './SuiteUI';

const FILES = ['custom_env.ini', 'custom_mod.yaml', 'unbound_custom.conf', 'force_forward_list.txt',
  'force_dnscrypt_list.txt', 'force_recurse_list.txt', 'force_ttl_rules.txt', 'custom_cn_mark.txt', 'trackerslist.txt'];

export default function SuiteHistory() {
  const [file, setFile] = useState(FILES[0]);
  return <Section title="配置历史与恢复" subtitle="每个文件保留最多 20 个版本。恢复后仍需根据生效条件应用和验证。" icon={History}>
    <label className="suite-field">配置文件<select aria-label="历史配置文件" className="form-select" value={file} onChange={(e) => setFile(e.target.value)}>{FILES.map((name) => <option key={name}>{name}</option>)}</select></label>
    <FileHistory key={file} file={file} />
  </Section>;
}

function FileHistory({ file }: { file: string }) {
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [current, setCurrent] = useState('');
  const [currentRevision, setCurrentRevision] = useState<string | undefined>();
  const [preview, setPreview] = useState<{ revision: number; content: string } | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirm, setConfirm] = useState(false);
  const controller = useRef<AbortController | null>(null);

  const load = useCallback(async (signal: AbortSignal) => {
    const [history, actual] = await Promise.all([
      suiteFetch<{ revisions: Revision[] }>(`/api/v1/history/${file}`, { signal }),
      suiteFetch<FileContent>(`/api/v1/file/${file}`, { signal }).catch((e) => { if (e instanceof ApiError && e.status === 404) return { content: '', revision: 'missing' }; throw e; }),
    ]);
    if (!signal.aborted) { setRevisions(history.revisions); setCurrent(actual.content); setCurrentRevision(actual.revision); }
  }, [file]);
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    const timer = setTimeout(() => { void load(abort.signal)
      .catch((e) => { if (!abort.signal.aborted) setError(errorText(e)); })
      .finally(() => { if (!abort.signal.aborted) setBusy(false); }); }, 0);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [load]);

  const read = async (revision: number) => {
    const signal = controller.current!.signal;
    setBusy(true); setError(''); setNotice(''); setPreview(null); setConfirm(false);
    try { const value = await suiteFetch<{ revision: number; content: string }>(`/api/v1/history/${file}/${revision}`, { signal }); if (!signal.aborted) setPreview(value); }
    catch (e) { if (!signal.aborted) setError(errorText(e)); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const restore = async () => {
    if (!preview || !confirm) return;
    const signal = controller.current!.signal;
    setBusy(true); setError(''); setConfirm(false);
    try {
      const result = await suiteFetch<SaveResult>(`/api/v1/history/${file}/${preview.revision}/restore`, { method: 'POST', signal, body: JSON.stringify({ expected_revision: currentRevision }) });
      if (signal.aborted) return;
      setNotice(`${result.message}。${result.condition}。尚未验证每条规则已生效。`);
      setPreview(null); await load(signal);
    } catch (e) { if (!signal.aborted) setError(errorText(e)); }
    finally { if (!signal.aborted) setBusy(false); }
  };
  const diff = preview ? configDiff(current, preview.content) : null;
  return <div className="suite-history">
    {error && <Notice tone="error">{error}</Notice>}{notice && <Notice>{notice}</Notice>}
    {revisions.length ? <div className="suite-revision-list">{revisions.map((item) => <button className={preview?.revision === item.id ? 'is-selected' : ''} key={item.id} disabled={busy} onClick={() => void read(item.id)}><span><strong>{when(item.created_at)}</strong><small>{item.reason === 'saved' ? '保存版本' : '保存前版本'} · {bytes(item.bytes)}</small></span><code>#{item.id} · {item.sha256.slice(0, 10)}</code></button>)}</div>
      : <Empty title="暂无配置历史">从集成版首次保存配置开始记录；已有文件不会自动生成过去的版本。</Empty>}
    {preview && <div className="suite-history-preview"><div className="suite-toolbar"><strong>版本 #{preview.revision} → {file}</strong><button className="btn btn-secondary btn-sm" onClick={() => { const url = URL.createObjectURL(new Blob([preview.content], { type: 'text/plain;charset=utf-8' })); const link = document.createElement('a'); link.href = url; link.download = `${file}.revision-${preview.revision}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }}><Download size={14} />下载此版本</button></div>
      <p className="suite-muted">下方比较当前磁盘文件与将要恢复的版本：红色行会移除，绿色行会恢复。</p>
      {diff ? <pre className="suite-diff">{diff.map((line, i) => <span className={`is-${line.kind}`} key={i}>{line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : ' '} {line.text || ' '}{'\n'}</span>)}</pre>
        : <Notice tone="warning">文件较大，省略逐行差异。请下载历史版本，并在展开原文后核对。</Notice>}
      <details><summary>查看此历史版本原文</summary><pre className="suite-log">{preview.content}</pre></details>
      <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirm(true)}><RotateCcw size={14} />恢复此版本</button>
      {confirm && <div className="suite-confirm"><p>将覆盖磁盘上的 {file}，当前内容会记录到历史。文件监听可能触发加载，恢复后仍需检查应用状态和目标域名。</p><div className="suite-button-row"><button className="btn btn-primary" disabled={busy} onClick={() => void restore()}>确认恢复版本 #{preview.revision}</button><button className="btn btn-secondary" onClick={() => setConfirm(false)}>取消</button></div></div>}
    </div>}
  </div>;
}
