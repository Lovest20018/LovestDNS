import { parseListContent, serializeListContent } from '../domainEditor';
import type { DomainEntry } from '../domainEditor';
import EditorToolbar from '../components/EditorToolbar';
import { useSuiteConfig } from '../suiteContext';
import { useUnsavedChanges } from '../unsaved';
import { useFileEditor } from '../useFileEditor';
import { useState } from 'react';
import { LIST_FILE_INFO } from '../types';
import { Plus, X, FileText, Download, Copy, Save, CheckCircle, AlertCircle, ArrowUp, ArrowDown } from 'lucide-react';
import { useToast } from '../hooks';

const PREFIXES = ['domain', 'full', 'regexp', 'keyword'];

const createEntryId = () => Date.now() + Math.random();

const createDomainEntry = (): DomainEntry => ({
  id: createEntryId(),
  prefix: 'domain',
  value: '',
  comment: false,
});

const createCommentEntry = (): DomainEntry => ({
  id: createEntryId(),
  prefix: '__comment__',
  value: '# ',
  comment: true,
});

const ALL_LISTS = [
  ...LIST_FILE_INFO,
  {
    key: 'custom_cn_mark',
    filename: 'custom_cn_mark.txt',
    title: '自定义 CN 域名标记',
    description: '额外定义标记为 CN 的域名。当域名被 USE_MARK_DATA 或 IP 库认定为非 CN 但你希望当成 CN 处理时使用。格式同 force_*_list。',
    requiresForward: false,
    defaultContent: `# 自定义 CN 域名标记\n# 当域名被误判为非 CN 时可在此添加\n`,
  },
  {
    key: 'trackerslist',
    filename: 'trackerslist.txt',
    title: 'BT Tracker 列表',
    description: 'BT tracker 域名列表，CN_TRACKER=yes 时这些域名强制走 dnscrypt。每行一个 tracker URL，会自动增量更新合并。',
    requiresForward: false,
    defaultContent: `# BT Tracker 列表\n# 每行一个 tracker URL\n# 自动增量更新合并\n`,
  },
];

export default function DomainListPage() {
  const [activeTab, setActiveTab] = useState(() => ALL_LISTS.find((list) => list.filename === new URLSearchParams(window.location.hash.split('?')[1]).get('file'))?.key ?? 'force_dnscrypt_list');
  return <DomainFileEditor key={activeTab} activeTab={activeTab} onSelect={setActiveTab} />;
}

function DomainFileEditor({ activeTab, onSelect }: { activeTab: string; onSelect: (key: string) => void }) {
  const { error: connectionError } = useSuiteConfig();
  const { showToast, ToastComponent } = useToast();
  const [editMode, setEditMode] = useState<'visual' | 'text'>('text');
  const activeList = ALL_LISTS.find((l) => l.key === activeTab)!;
  const editor = useFileEditor(activeList.filename, activeList.defaultContent);
  const { content: rawContent, setContent: setRawContent, busy: saving, result: saveResult } = editor;
  const loading = !editor.loaded && !editor.error;
  const loadFailed = !editor.loaded || !!editor.error;
  const loadFile = () => editor.load(true);
  const isTrackerList = activeTab === 'trackerslist';
  const entries = isTrackerList ? [] : parseListContent(rawContent);
  const setEntries = (update: DomainEntry[] | ((old: DomainEntry[]) => DomainEntry[])) => setRawContent(serializeListContent(typeof update === 'function' ? update(entries) : update));
  const ruleCount = entries.filter((entry) => entry.prefix !== '__comment__').length;
  const commentCount = entries.length - ruleCount;
  const disabledRuleCount = entries.filter((entry) => entry.prefix !== '__comment__' && entry.comment).length;
  const getCurrentContent = () => rawContent;
  const hasUnsavedChanges = editor.dirty;
  useUnsavedChanges(hasUnsavedChanges);
  const switchTab = (key: string) => onSelect(key);
  const switchEditMode = (mode: 'visual' | 'text') => { if (!isTrackerList || mode === 'text') setEditMode(mode); };

  const updateEntry = (id: number, field: keyof DomainEntry, val: string | boolean) => {
    setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, [field]: val } : e)));
  };

  const addEntry = () => {
    setEntries((prev) => [
      ...prev,
      createDomainEntry(),
    ]);
  };

  const addCommentEntry = () => {
    setEntries((prev) => [
      ...prev,
      createCommentEntry(),
    ]);
  };

  const insertEntryAfter = (id: number, entry: DomainEntry) => {
    setEntries((prev) => {
      const index = prev.findIndex((e) => e.id === id);
      if (index === -1) return [...prev, entry];
      const next = [...prev];
      next.splice(index + 1, 0, entry);
      return next;
    });
  };

  const insertDomainAfter = (id: number) => {
    insertEntryAfter(id, createDomainEntry());
  };

  const insertCommentAfter = (id: number) => {
    insertEntryAfter(id, createCommentEntry());
  };

  const removeEntry = (id: number) => {
    setEntries((prev) => prev.filter((e) => e.id !== id));
  };

  const moveEntry = (id: number, direction: -1 | 1) => {
    setEntries((prev) => {
      const index = prev.findIndex((e) => e.id === id);
      const nextIndex = index + direction;
      if (index === -1 || nextIndex < 0 || nextIndex >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };

  const handleSave = async () => {
    const result = await editor.save();
    if (result?.success) showToast('配置已保存；请按生效提示继续');
  };

  const handleCopy = async () => {
    const content = getCurrentContent();
    await navigator.clipboard.writeText(content);
    showToast('已复制到剪贴板');
  };

  const handleDownload = () => {
    const content = getCurrentContent();
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = activeList.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <fieldset className="suite-editor-fieldset" disabled={saving} aria-busy={saving}>

      <div className="page-header">
        <h2>域名列表管理</h2>
        <p>管理域名分流列表文件。保存后按当前运行参数判断监听条件，再使用查询测试验证。</p>
      </div>

      <div className="tab-bar suite-file-tabs suite-list-tabs">
        {ALL_LISTS.map((list) => (
          <button
            key={list.key}
            className={`tab-item ${activeTab === list.key ? 'active' : ''}`}
            onClick={() => switchTab(list.key)}
          >
            {list.title.replace('强制 ', '').replace('域名列表', '')}
          </button>
        ))}
      </div>

      <EditorToolbar file={activeList.filename} dirty={hasUnsavedChanges} busy={saving} blocked={loading || loadFailed || !!connectionError} message={saveResult?.msg} failed={saveResult?.success === false} result={saveResult?.result} onSave={() => void handleSave()} />
      {editor.error && <p className="suite-editor-result is-error" role="alert">{editor.error}</p>}

      <div className="card">
        <div className="card-title">{activeList.title}</div>
        <div className="card-desc">{activeList.description}</div>

        <div className="tab-bar" style={{ marginBottom: 16 }}>
          <button className={`tab-item ${editMode === 'text' ? 'active' : ''}`} onClick={() => switchEditMode('text')}>文本编辑</button>
          <button
            className={`tab-item ${editMode === 'visual' ? 'active' : ''}`}
            onClick={() => switchEditMode('visual')}
            disabled={isTrackerList}
            title={isTrackerList ? 'Tracker URL 列表仅支持文本编辑' : undefined}
          >
            可视化编辑
          </button>
        </div>

        {isTrackerList && (
          <div className="card-desc" style={{ marginBottom: 16, color: 'var(--accent-amber)' }}>
            trackerslist.txt 每行是 Tracker URL，不是 domain/full/regexp 规则。此文件已禁用可视化编辑，避免保存时误加 domain: 前缀。
          </div>
        )}

        {loading ? (
          <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>加载中...</div>
        ) : editMode === 'visual' && !isTrackerList ? (
          <>
            <div className="domain-editor-toolbar">
              <div className="stack-actions">
                <span className="summary-chip">总行 {entries.length}</span>
                <span className="summary-chip">规则 {ruleCount}</span>
                <span className="summary-chip">注释 {commentCount}</span>
                <span className="summary-chip">已禁用 {disabledRuleCount}</span>
              </div>
              <div className="form-hint">每行右侧的“域名 / 注释”会插入到当前行下面；底部按钮用于追加到末尾。</div>
            </div>
            <div className="domain-list-editor">
              {entries.map((entry, index) => (
                <div className={`domain-entry ${entry.prefix === '__comment__' ? 'is-comment' : ''}`} key={entry.id}>
                  <span className="domain-entry-index">{index + 1}</span>
                  <span className={`domain-entry-type ${entry.prefix === '__comment__' ? 'comment' : entry.comment ? 'disabled' : 'rule'}`}>
                    {entry.prefix === '__comment__' ? '注释' : entry.comment ? '禁用' : '规则'}
                  </span>
                  {entry.prefix === '__comment__' ? (
                    <input className="form-input" type="text" value={entry.value} placeholder="# 注释说明" onChange={(e) => updateEntry(entry.id, 'value', e.target.value)} style={{ flex: 1 }} />
                  ) : (
                    <>
                      <select className="form-select domain-prefix-select" value={entry.prefix} onChange={(e) => updateEntry(entry.id, 'prefix', e.target.value)}>
                        {PREFIXES.map((p) => (<option key={p} value={p}>{p}:</option>))}
                      </select>
                      <input className="form-input" type="text" value={entry.value} placeholder="example.com" onChange={(e) => updateEntry(entry.id, 'value', e.target.value)} />
                      <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, color: 'var(--text-muted)', whiteSpace: 'nowrap', cursor: 'pointer' }}>
                        <input type="checkbox" checked={entry.comment} onChange={(e) => updateEntry(entry.id, 'comment', e.target.checked)} />注释
                      </label>
                    </>
                  )}
                  <div className="domain-row-actions">
                    {!entry.comment && ['domain', 'full'].includes(entry.prefix) && /^[a-zA-Z0-9][a-zA-Z0-9.-]*$/.test(entry.value.trim()) && <a className="btn btn-secondary btn-sm" href={`#/diagnostics?domain=${encodeURIComponent(entry.value.trim())}`} title="查询当前实例；未保存规则需先保存并按提示应用">查询</a>}
                    <button className="btn btn-secondary btn-sm" onClick={() => moveEntry(entry.id, -1)} disabled={index === 0} title="上移此行"><ArrowUp size={14} /></button>
                    <button className="btn btn-secondary btn-sm" onClick={() => moveEntry(entry.id, 1)} disabled={index === entries.length - 1} title="下移此行"><ArrowDown size={14} /></button>
                    <button className="btn btn-secondary btn-sm" onClick={() => insertDomainAfter(entry.id)} title="在此行后插入域名"><Plus size={14} /> 域名</button>
                    <button className="btn btn-secondary btn-sm" onClick={() => insertCommentAfter(entry.id)} title="在此行后插入注释"><Plus size={14} /> 注释</button>
                    <button className="domain-remove-btn" onClick={() => removeEntry(entry.id)} title="删除此行"><X size={16} /></button>
                  </div>
                </div>
              ))}
            </div>
            <div className="inline-actions">
              <button className="btn btn-secondary btn-sm" onClick={addEntry}><Plus size={14} /> 添加域名</button>
              <button className="btn btn-secondary btn-sm" onClick={addCommentEntry}><Plus size={14} /> 添加注释</button>
            </div>
          </>
        ) : (
          <textarea disabled={!editor.loaded} aria-label={activeList.filename + " 文件内容"} className="textarea-code" value={rawContent} onChange={(e) => setRawContent(e.target.value)} rows={15} />
        )}

        <div className="suite-editor-actions" style={{ marginTop: 16, borderTop: '1px solid var(--border-color)', paddingTop: 16, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={saving || loading || loadFailed || !!connectionError || !hasUnsavedChanges} onClick={handleSave}>
            <Save size={16} /> {saving ? '保存中...' : '保存配置'}
          </button>
          <button className="btn btn-secondary btn-sm" onClick={handleCopy}><Copy size={14} /> 复制</button>
          <button className="btn btn-secondary btn-sm" onClick={handleDownload}><Download size={14} /> 下载</button>
          {saveResult && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13 }}>
              {saveResult.success ? (
                <><CheckCircle size={14} style={{ color: 'var(--accent-green)' }} /> <span style={{ color: 'var(--accent-green)' }}>{saveResult.msg}</span></>
              ) : (
                <><AlertCircle size={14} style={{ color: 'var(--accent-red)' }} /> <span style={{ color: 'var(--accent-red)' }}>{saveResult.msg}</span></>
              )}
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">域名匹配语法</div>
        <table className="info-table">
          <thead><tr><th>前缀</th><th>说明</th><th>示例</th></tr></thead>
          <tbody>
            <tr><td><code style={{ color: 'var(--accent-cyan)' }}>domain:</code></td><td>匹配域名及子域名</td><td>domain:03k.org</td></tr>
            <tr><td><code style={{ color: 'var(--accent-cyan)' }}>full:</code></td><td>精确匹配，优先级最高</td><td>full:03k.org</td></tr>
            <tr><td><code style={{ color: 'var(--accent-cyan)' }}>regexp:</code></td><td>Go 标准正则</td><td>regexp:.+\.03k\.org$</td></tr>
            <tr><td><code style={{ color: 'var(--accent-cyan)' }}>keyword:</code></td><td>关键字匹配</td><td>keyword:03k.org</td></tr>
          </tbody>
        </table>
      </div>

      <details className="card suite-editor-help"><summary><FileText size={16} /> 匹配优先级说明</summary>
        <div className="card-title"><FileText size={18} /> 匹配优先级</div>
        <div className="priority-flow">
          <span className="priority-item" style={{ background: 'rgba(139,92,246,0.15)', color: '#8b5cf6' }}>custom_mod (top)</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(59,130,246,0.15)', color: '#3b82f6' }}>force_forward_list</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(16,185,129,0.15)', color: '#10b981' }}>force_dnscrypt_list</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b' }}>force_recurse_list</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(6,182,212,0.15)', color: '#06b6d4' }}>force_ttl_rules</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(139,92,246,0.15)', color: '#8b5cf6' }}>custom_mod (list)</span>
          <span className="priority-arrow">&gt;</span>
          <span className="priority-item" style={{ background: 'rgba(107,114,128,0.15)', color: '#6b7280' }}>自动分流</span>
        </div>
      </details>

      {loadFailed && !loading && <p role="alert">配置尚未成功读取，保存已停用。<button className="btn btn-secondary btn-sm" onClick={() => void loadFile()}>重新读取</button></p>}
      {ToastComponent}
      </fieldset>
    </div>
  );
}
