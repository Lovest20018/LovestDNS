import { parseCustomEnvDocument, serializeCustomEnvDocument, validateCustomEnvRows } from '../customEnvEditor';
import type { CustomEnvRow } from '../customEnvEditor';
import EditorToolbar from '../components/EditorToolbar';
import { useSuiteConfig } from '../suiteContext';
import { useFileEditor } from '../useFileEditor';
import { useUnsavedChanges } from '../unsaved';
import { useState } from 'react';
import { useToast } from '../hooks';
import { copyToClipboard, downloadFile } from '../generators';
import { Plus, X, Copy, Download, FileCode, FileText, Save, CheckCircle, AlertCircle, RefreshCw, AlertTriangle } from 'lucide-react';
import type { CustomModZone, CustomModSwap, CustomModHost } from '../types';
import YAML from 'yaml';

type SubTab = 'custom_env' | 'custom_mod' | 'unbound_custom';
type CustomEnvEditMode = 'text' | 'visual';

interface CustomModParseResult {
  zones: CustomModZone[];
  swaps: CustomModSwap[];
  hosts: CustomModHost[];
  visualSafe: boolean;
}

const CUSTOM_MOD_KEYS = new Set(['Zones', 'Swaps', 'Hosts']);

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function parseCustomModYaml(content: string): CustomModParseResult {
  const emptyResult: CustomModParseResult = { zones: [], swaps: [], hosts: [], visualSafe: true };
  if (!content.trim()) return emptyResult;

  try {
    const parsed = YAML.parse(content);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return emptyResult;

    const keys = Object.keys(parsed as Record<string, unknown>);
    const allowed: Record<string, string[]> = { Zones: ['zone', 'dns', 'ttl', 'seq', 'socks5'], Swaps: ['env_key', 'cidr_file'], Hosts: ['env_key', 'zone'] };
    const visualSafe = keys.every((key) => CUSTOM_MOD_KEYS.has(key) && Array.isArray(parsed[key]) && parsed[key].every((item: unknown) => item && typeof item === 'object' && !Array.isArray(item) && Object.entries(item).every(([name, value]) => allowed[key].includes(name) && (name === 'ttl' ? typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 : typeof value === 'string'))));
    if (!visualSafe) return { ...emptyResult, visualSafe: false };

    const data = parsed as Record<string, unknown>;
    const zones = Array.isArray(data.Zones)
      ? data.Zones.map((item: unknown): CustomModZone => {
          const record = asRecord(item);
          return {
            zone: String(record.zone ?? ''),
            dns: String(record.dns ?? ''),
            ttl: Number(record.ttl ?? 0) || 0,
            seq: record.seq === 'top6' || record.seq === 'list' ? record.seq : 'top',
            socks5: record.socks5 === 'yes' ? 'yes' : 'no',
          };
        })
      : [];
    const swaps = Array.isArray(data.Swaps)
      ? data.Swaps.map((item: unknown): CustomModSwap => {
          const record = asRecord(item);
          return {
            env_key: String(record.env_key ?? ''),
            cidr_file: String(record.cidr_file ?? ''),
          };
        })
      : [];
    const hosts = Array.isArray(data.Hosts)
      ? data.Hosts.map((item: unknown): CustomModHost => {
          const record = asRecord(item);
          return {
            env_key: String(record.env_key ?? ''),
            zone: String(record.zone ?? ''),
          };
        })
      : [];

    return { zones, swaps, hosts, visualSafe: true };
  } catch {
    return { ...emptyResult, visualSafe: false };
  }
}

export default function AdvancedConfigPage() {
  const { error: connectionError } = useSuiteConfig();
  const { showToast, ToastComponent } = useToast();
  const [activeSubTab, setActiveSubTab] = useState<SubTab>(() => { const file = new URLSearchParams(window.location.hash.split('?')[1]).get('file'); return file === 'custom_mod.yaml' ? 'custom_mod' : file === 'unbound_custom.conf' ? 'unbound_custom' : 'custom_env'; });
  const envEditor = useFileEditor('custom_env.ini', '', activeSubTab === 'custom_env');
  const modEditor = useFileEditor('custom_mod.yaml', '', activeSubTab === 'custom_mod');
  const unboundEditor = useFileEditor('unbound_custom.conf', '', activeSubTab === 'unbound_custom');
  const editors = { custom_env: envEditor, custom_mod: modEditor, unbound_custom: unboundEditor };
  const activeEditor = editors[activeSubTab];
  const customEnvContent = envEditor.content;
  const setCustomEnvContent = envEditor.setContent;
  const unboundContent = unboundEditor.content;
  const setUnboundContent = unboundEditor.setContent;
  const customModContent = modEditor.content;
  const customEnvEntries = parseCustomEnvDocument(customEnvContent).entries;
  const setCustomEnvEntries = (update: CustomEnvRow[] | ((rows: CustomEnvRow[]) => CustomEnvRow[])) => {
    const rows = typeof update === 'function' ? update(customEnvEntries) : update;
    setCustomEnvContent(serializeCustomEnvDocument(customEnvContent, rows));
  };
  const [customEnvEditMode, setCustomEnvEditMode] = useState<CustomEnvEditMode>('text');
  const parsedMod = parseCustomModYaml(customModContent);
  const { zones: customModZones, swaps: customModSwaps, hosts: customModHosts, visualSafe: customModVisualSafe } = parsedMod;
  const [validationError, setValidationError] = useState('');
  const saveResult = validationError ? { success: false, msg: validationError, result: undefined } : activeEditor.result;
  const saving = activeEditor.busy;
  const loading = !activeEditor.loaded && !activeEditor.error;
  const loadFailed = !activeEditor.loaded || !!activeEditor.error;
  const loadFile = (tab: SubTab) => editors[tab].load(true);
  const getCustomModOutput = () => customModContent;
  const getCustomEnvOutput = () => customEnvContent;
  const switchCustomEnvEditMode = (mode: CustomEnvEditMode) => setCustomEnvEditMode(mode);
  const addEnvEntry = () => setCustomEnvEntries((rows) => [...rows, { key: '', value: '', enabled: true, sourceLine: null }]);
  const updateEnvEntry = (index: number, entry: CustomEnvRow) => {
    if (/[\s="]/.test(entry.key)) { showToast('变量名不能包含空格、等号或引号'); return; }
    setCustomEnvEntries((rows) => rows.map((row, i) => i === index ? entry : row));
  };
  const removeEnvEntry = (index: number) => setCustomEnvEntries((rows) => rows.filter((_, i) => i !== index));
  const updateCustomEnvText = setCustomEnvContent;
  const editMod = (section: string, index: number | null, value: Record<string, unknown> | null) => {
    if (!customModVisualSafe) return;
    try {
      const document = YAML.parseDocument(customModContent || '{}');
      if (!document.has(section)) document.set(section, []);
      if (index === null) document.addIn([section], value);
      else if (value === null) document.deleteIn([section, index]);
      else for (const [key, item] of Object.entries(value)) document.setIn([section, index, key], item);
      modEditor.setContent(document.toString());
    } catch { showToast('此 YAML 结构无法安全编辑，请使用文本模式'); }
  };
  const addZone = () => editMod('Zones', null, { zone: '', dns: '', ttl: 0, seq: 'top', socks5: 'no' });
  const updateZone = (i: number, zone: CustomModZone) => editMod('Zones', i, { ...zone });
  const removeZone = (i: number) => editMod('Zones', i, null);
  const addSwap = () => editMod('Swaps', null, { env_key: '', cidr_file: '' });
  const updateSwap = (i: number, swap: CustomModSwap) => editMod('Swaps', i, { ...swap });
  const removeSwap = (i: number) => editMod('Swaps', i, null);
  const addHost = () => editMod('Hosts', null, { env_key: '', zone: '' });
  const updateHost = (i: number, host: CustomModHost) => editMod('Hosts', i, { ...host });
  const removeHost = (i: number) => editMod('Hosts', i, null);
  const getActiveOutput = () => ({ filename: { custom_env: 'custom_env.ini', custom_mod: 'custom_mod.yaml', unbound_custom: 'unbound_custom.conf' }[activeSubTab], content: activeEditor.content });
  const tabDirty = { custom_env: envEditor.dirty, custom_mod: modEditor.dirty, unbound_custom: unboundEditor.dirty };
  const dirty = Object.values(tabDirty).some(Boolean);
  useUnsavedChanges(Object.values(tabDirty).some(Boolean));
  const handleSave = async () => {
    setValidationError('');
    try {
      if (activeSubTab === 'custom_env' && customEnvEditMode === 'visual') validateCustomEnvRows(customEnvEntries);
      const result = await activeEditor.save();
      if (result?.success) showToast('配置已保存；请按生效提示继续');
    } catch (e) { setValidationError(getErrorMessage(e)); }
  };

  const handleCopyOutput = async () => {
    const { content } = getActiveOutput();
    await copyToClipboard(content);
    showToast('已复制到剪贴板');
  };

  const handleDownloadOutput = () => {
    const { filename, content } = getActiveOutput();
    downloadFile(content, filename);
    showToast('文件已下载');
  };

  return (
    <div>
      <fieldset className="suite-editor-fieldset" disabled={saving} aria-busy={saving}>

      <div className="page-header">
        <h2>高级配置</h2>
        <p>
          编辑 custom_env.ini（运行时覆盖变量）、custom_mod.yaml（自定义 Zones/Swaps/Hosts）
          和 unbound_custom.conf（Unbound 自定义配置）。保存后写入 /data 目录。
        </p>
      </div>

      <div className="stack-actions" style={{ marginBottom: 16 }}>
        <span className="summary-chip">ENV {customEnvEntries.length}</span>
        <span className="summary-chip">Zones {customModZones.length}</span>
        <span className="summary-chip">Swaps {customModSwaps.length}</span>
        <span className="summary-chip">Hosts {customModHosts.length}</span>
      </div>

      <div className="tab-bar suite-file-tabs">
        <button className={`tab-item ${activeSubTab === 'custom_env' ? 'active' : ''}`} onClick={() => { setValidationError(''); setActiveSubTab('custom_env'); }}>
          <FileText size={14} style={{ display: 'inline', verticalAlign: 'middle' }} /> custom_env.ini
        {tabDirty.custom_env && <span className="suite-draft-dot"> · 未保存</span>}</button>
        <button className={`tab-item ${activeSubTab === 'custom_mod' ? 'active' : ''}`} onClick={() => { setValidationError(''); setActiveSubTab('custom_mod'); }}>
          <FileCode size={14} style={{ display: 'inline', verticalAlign: 'middle' }} /> custom_mod.yaml
        {tabDirty.custom_mod && <span className="suite-draft-dot"> · 未保存</span>}</button>
        <button className={`tab-item ${activeSubTab === 'unbound_custom' ? 'active' : ''}`} onClick={() => { setValidationError(''); setActiveSubTab('unbound_custom'); }}>
          <FileCode size={14} style={{ display: 'inline', verticalAlign: 'middle' }} /> unbound_custom.conf
        {tabDirty.unbound_custom && <span className="suite-draft-dot"> · 未保存</span>}</button>
      </div>

      <EditorToolbar file={getActiveOutput().filename} dirty={tabDirty[activeSubTab]} busy={saving} blocked={loading || loadFailed || !!connectionError} message={saveResult?.msg} failed={saveResult?.success === false} result={saveResult?.result} onSave={() => void handleSave()} />
      {activeEditor.error && <p className="suite-editor-result is-error" role="alert">{activeEditor.error}</p>}

      {loading && (
        <div className="loading-panel">
          <div className="loading-card" style={{ color: 'var(--text-muted)' }}>
            <RefreshCw size={16} style={{ animation: 'spin 1s linear infinite' }} /> 加载中...
            <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
          </div>
        </div>
      )}

      {activeSubTab === 'custom_env' && (
        <div className="form-panel">
          <div className="card-title">
            custom_env.ini — 运行时变量覆盖
            <span className="badge badge-green" style={{ marginLeft: 8 }}>按生效提示处理</span>
          </div>
          <div className="card-desc">
            变量覆盖按核心返回的生效条件处理。初始化参数需重启服务；规则变量可在配置中心预览并应用。格式：key="value"
          </div>

          <div className="tab-bar" style={{ marginBottom: 16 }}>
            <button className={`tab-item ${customEnvEditMode === 'text' ? 'active' : ''}`} onClick={() => switchCustomEnvEditMode('text')}>文本编辑</button>
            <button
              className={`tab-item ${customEnvEditMode === 'visual' ? 'active' : ''}`}
              onClick={() => switchCustomEnvEditMode('visual')}
            >
              可视化编辑
            </button>
          </div>

          <p className="card-desc" style={{ marginBottom: 16 }}>可视化编辑仅修改对应变量，原有注释和空行会保留。{parseCustomEnvDocument(customEnvContent).unrecognizedLines > 0 && '有无法识别的行已原样保留，请在文本模式检查。'}</p>

          {customEnvEditMode === 'visual' ? (
            <>
              {customEnvEntries.map((entry, i) => (
                <div className="domain-entry" key={i} style={{ marginBottom: 8 }}>
                  <input className="form-input" style={{ width: 180, flexShrink: 0 }} type="text" value={entry.key} aria-label={`变量 ${i + 1} 名称`} placeholder="VARIABLE_NAME" onChange={(e) => updateEnvEntry(i, { ...entry, key: e.target.value })} />
                  <input className="form-input" type="text" value={entry.value} aria-label={`变量 ${i + 1} 值`} placeholder="value" onChange={(e) => updateEnvEntry(i, { ...entry, value: e.target.value })} />
                  <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12, color: 'var(--text-muted)', whiteSpace: 'nowrap', cursor: 'pointer' }}>
                    <input aria-label={`启用变量 ${i + 1}`} type="checkbox" checked={entry.enabled} onChange={(e) => updateEnvEntry(i, { ...entry, enabled: e.target.checked })} />启用
                  </label>
                  <button className="domain-remove-btn" aria-label={`删除变量 ${i + 1}`} onClick={() => removeEnvEntry(i)}><X size={16} /></button>
                </div>
              ))}

              <div className="inline-actions">
                <button className="btn btn-secondary btn-sm" onClick={addEnvEntry}><Plus size={14} /> 添加变量</button>
              </div>
            </>
          ) : (
            <textarea disabled={!envEditor.loaded} aria-label="custom_env.ini 文件内容" className="textarea-code" value={customEnvContent} onChange={(e) => updateCustomEnvText(e.target.value)} rows={15} />
          )}

          <div style={{ marginTop: 20, borderTop: '1px solid var(--border-color)', paddingTop: 16 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: 'var(--text-secondary)' }}>生成预览</div>
            <div className="preview-shell command-box"><div className="code-content" style={{ padding: 12, fontSize: 12 }}>{getCustomEnvOutput()}</div></div>
          </div>
        </div>
      )}

      {activeSubTab === 'custom_mod' && (
        <>
          <details className="form-panel" open={!customModVisualSafe}><summary>编辑 YAML 原文</summary><textarea disabled={!modEditor.loaded} aria-label="custom_mod.yaml 文件内容" className="textarea-code" rows={12} value={customModContent} onChange={(event) => modEditor.setContent(event.target.value)} /></details>
          <div className="form-panel" style={{ borderColor: 'var(--accent-amber)' }}>
            <div className="card-desc"><AlertTriangle size={16} style={{ display: 'inline', verticalAlign: 'middle' }} /> custom_mod.yaml 保存后不会自动生效。请到配置中心预览并应用，再查询目标域名验证规则；纯递归模式不支持 custom_mod。</div>
            <a className="btn btn-secondary" href="#/config" style={{ marginTop: 12 }}>前往配置中心预览并应用</a>
          </div>

          <div className="form-panel">
            <div className="card-title">Zones — 自定义域名区域转发</div>
            <div className="card-desc">定义域名区域转发到指定 DNS 服务器，seq=top 最高优先级，seq=list 在列表之后匹配。</div>
            {!customModVisualSafe && (
              <div className="card-desc" style={{ marginBottom: 12, color: 'var(--accent-amber)' }}>
                当前 custom_mod.yaml 含有暂不支持的 YAML 结构。保存/复制/下载会保留原文，下面的可视化表单不会写回文件。
              </div>
            )}
            {customModZones.map((zone, i) => (
              <div key={i} style={{ marginBottom: 12, padding: 12, background: 'var(--bg-input)', borderRadius: 'var(--radius-md)', border: '1px solid var(--border-color)' }}>
                <div className="form-row">
                  <div className="form-group"><label className="form-label">域名/区域</label><input className="form-input" type="text" value={zone.zone} placeholder="company.local" onChange={(e) => updateZone(i, { ...zone, zone: e.target.value })} /></div>
                  <div className="form-group"><label className="form-label">DNS 服务器</label><input className="form-input" type="text" value={zone.dns} placeholder="udp://10.10.10.3:53" onChange={(e) => updateZone(i, { ...zone, dns: e.target.value })} /></div>
                </div>
                <div className="form-row">
                  <div className="form-group"><label className="form-label">TTL</label><input className="form-input" type="number" value={zone.ttl} min={0} onChange={(e) => updateZone(i, { ...zone, ttl: parseInt(e.target.value) || 0 })} /></div>
                  <div className="form-group"><label className="form-label">优先级 (seq)</label><select className="form-select" value={zone.seq} onChange={(e) => updateZone(i, { ...zone, seq: e.target.value as CustomModZone['seq'] })}><option value="top">top</option><option value="top6">top6</option><option value="list">list</option></select></div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 13, color: 'var(--text-secondary)', cursor: 'pointer' }}>
                    <input type="checkbox" checked={zone.socks5 === 'yes'} onChange={(e) => updateZone(i, { ...zone, socks5: e.target.checked ? 'yes' : 'no' })} />走 SOCKS5
                  </label>
                  <button className="btn btn-danger btn-sm" onClick={() => removeZone(i)}><X size={14} /> 删除</button>
                </div>
              </div>
            ))}
            <button className="btn btn-secondary btn-sm" disabled={!customModVisualSafe} onClick={addZone}><Plus size={14} /> 添加 Zone</button>
          </div>

          <div className="form-panel">
            <div className="card-title">Swaps — IP/CIDR 段替换</div>
            {customModSwaps.map((swap, i) => (
              <div className="domain-entry" key={i} style={{ marginBottom: 8 }}>
                <input className="form-input" style={{ width: 180, flexShrink: 0 }} type="text" value={swap.env_key} placeholder="env_key" onChange={(e) => updateSwap(i, { ...swap, env_key: e.target.value })} />
                <input className="form-input" type="text" value={swap.cidr_file} placeholder="/data/test_cidr.txt" onChange={(e) => updateSwap(i, { ...swap, cidr_file: e.target.value })} />
                <button className="domain-remove-btn" onClick={() => removeSwap(i)}><X size={16} /></button>
              </div>
            ))}
            <button className="btn btn-secondary btn-sm" disabled={!customModVisualSafe} onClick={addSwap}><Plus size={14} /> 添加 Swap</button>
          </div>

          <div className="form-panel">
            <div className="card-title">Hosts — 域名直接映射</div>
            {customModHosts.map((host, i) => (
              <div className="domain-entry" key={i} style={{ marginBottom: 8 }}>
                <input className="form-input" style={{ width: 180, flexShrink: 0 }} type="text" value={host.env_key} placeholder="env_key" onChange={(e) => updateHost(i, { ...host, env_key: e.target.value })} />
                <input className="form-input" type="text" value={host.zone} placeholder="a.com domain:b.com" onChange={(e) => updateHost(i, { ...host, zone: e.target.value })} />
                <button className="domain-remove-btn" onClick={() => removeHost(i)}><X size={16} /></button>
              </div>
            ))}
            <button className="btn btn-secondary btn-sm" disabled={!customModVisualSafe} onClick={addHost}><Plus size={14} /> 添加 Host</button>
          </div>

          <div className="form-panel">
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: 'var(--text-secondary)' }}>生成预览</div>
            <div className="preview-shell command-box"><div className="code-content" style={{ padding: 12, fontSize: 12 }}>{getCustomModOutput()}</div></div>
          </div>
        </>
      )}

      {activeSubTab === 'unbound_custom' && (
        <div className="form-panel">
          <div className="card-title">
            unbound_custom.conf — Unbound 自定义配置
            <span className="badge badge-amber" style={{ marginLeft: 8 }}>需重启服务</span>
          </div>
          <div className="card-desc">添加 SRV 记录等高级配置。修改后需重启 DNS 服务生效。</div>
          <textarea disabled={!unboundEditor.loaded} aria-label="Unbound 文件内容" className="textarea-code" value={unboundContent} onChange={(e) => setUnboundContent(e.target.value)} rows={12} />
        </div>
      )}

      <p className="suite-save-scope">当前文件：<code>{getActiveOutput().filename}</code> · 仅保存当前文件{dirty && !tabDirty[activeSubTab] ? "；其他标签仍有未保存修改" : ""}</p>
      <div className="stack-actions suite-editor-actions" style={{ marginTop: 18 }}>
        <button className="btn btn-primary" disabled={saving || loading || loadFailed || !!connectionError} onClick={handleSave}>
          <Save size={16} /> {saving ? '保存中...' : '保存配置'}
        </button>
        <button className="btn btn-secondary btn-sm" onClick={handleCopyOutput}><Copy size={14} /> 复制</button>
        <button className="btn btn-secondary btn-sm" onClick={handleDownloadOutput}><Download size={14} /> 下载</button>
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

      {loadFailed && !loading && <p role="alert">配置尚未成功读取，保存已停用。<button className="btn btn-secondary btn-sm" onClick={() => void loadFile(activeSubTab)}>重新读取</button></p>}
      {ToastComponent}
      </fieldset>
    </div>
  );
}
