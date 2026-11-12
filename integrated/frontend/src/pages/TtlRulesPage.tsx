import { parseTtlRules, serializeTtlRules } from '../ttlEditor';
import type { TtlRuleEntry } from '../ttlEditor';
import EditorToolbar from '../components/EditorToolbar';
import { useSuiteConfig } from '../suiteContext';
import { useUnsavedChanges } from '../unsaved';
import { useFileEditor } from '../useFileEditor';
import { useState } from 'react';
import { useToast } from '../hooks';
import { Plus, X, Clock, Save, CheckCircle, AlertCircle, RefreshCw } from 'lucide-react';

export default function TtlRulesPage() {
  const { error: connectionError, runningEnv } = useSuiteConfig();
  const { showToast, ToastComponent } = useToast();
  const rulesTtl = parseInt(runningEnv?.RULES_TTL || '0');
  const rulesEnabled = rulesTtl > 0;
  const cnAutoEnabled = runningEnv?.CNAUTO === 'yes';
  const editor = useFileEditor('force_ttl_rules.txt');
  const { content: rawContent, setContent: setRawContent, busy: saving, result: saveResult, dirty } = editor;
  const loading = !editor.loaded && !editor.error;
  const loadFailed = !editor.loaded || !!editor.error;
  const loadFile = () => editor.load(true);
  const parsed = parseTtlRules(rawContent);
  const entries = parsed.entries;
  const visualSafe = parsed.visualSafe;
  const setEntries = (update: TtlRuleEntry[] | ((old: TtlRuleEntry[]) => TtlRuleEntry[])) => setRawContent(serializeTtlRules(typeof update === 'function' ? update(entries) : update, rawContent));
  const [editMode, setEditMode] = useState<'visual' | 'text'>('text');
  useUnsavedChanges(dirty);

  const addEntry = () => {
    setEntries((prev) => [
      ...prev,
      { id: Date.now(), domain: '', server: '', type: 'forward' },
    ]);
  };

  const switchEditMode = (mode: 'visual' | 'text') => {
    if (mode === 'visual' && !visualSafe) { showToast('当前文件含有未支持的语法，请使用文本编辑保留原文'); return; }
    setEditMode(mode);
  };

  const updateEntry = (id: number, updates: Partial<TtlRuleEntry>) => {
    setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, ...updates } : e)));
  };

  const updateRawContent = (content: string) => {
    setRawContent(content);
  };

  const removeEntry = (id: number) => {
    setEntries((prev) => prev.filter((e) => e.id !== id));
  };

  const handleSave = async () => {
    const result = await editor.save();
    if (result?.success) showToast('配置已保存；请按生效提示继续');
  };

  return (
    <div>
      <fieldset className="suite-editor-fieldset" disabled={saving} aria-busy={saving}>

      <div className="page-header">
        <h2>TTL 规则配置</h2>
        <p>
          配置 force_ttl_rules.txt，将指定域名转发到指定 DNS 服务器并修改 TTL 值。
          适用于 DDNS 域名实时更新、指定域名走特定权威 DNS 等场景。
        </p>
      </div>

      {runningEnv && !cnAutoEnabled && (
        <div className="card" style={{ borderColor: 'var(--accent-amber)' }}>
          <div className="card-desc" style={{ color: 'var(--accent-amber)' }}>
            CNAUTO 未开启，TTL 规则功能不会工作。请在基础设置中开启智能分流 并重启 DNS 服务。
          </div>
        </div>
      )}

      {!rulesEnabled && cnAutoEnabled && (
        <div className="card" style={{ borderColor: 'var(--accent-amber)' }}>
          <div className="card-desc" style={{ color: 'var(--accent-amber)' }}>
            RULES_TTL 当前为 0，TTL 规则不会生效。请在基础设置中将 RULES_TTL 设为大于 0 的值并重启 DNS 核心。
          </div>
        </div>
      )}

      <EditorToolbar file={"force_ttl_rules.txt"} dirty={dirty} busy={saving} blocked={loading || loadFailed || !!connectionError} message={saveResult?.msg} failed={saveResult?.success === false} result={saveResult?.result} onSave={() => void handleSave()} />
      {editor.error && <p className="suite-editor-result is-error" role="alert">{editor.error}</p>}

      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div className="card-title">规则编辑</div>
          <button className="btn btn-secondary btn-sm" onClick={() => void loadFile()} disabled={loading || saving}>
            <RefreshCw size={14} /> 刷新
          </button>
        </div>

        <div className="tab-bar" style={{ marginBottom: 16 }}>
          <button
            className={`tab-item ${editMode === 'visual' ? 'active' : ''}`}
            onClick={() => switchEditMode('visual')}
            disabled={!visualSafe}
            title={!visualSafe ? '当前文件含有可视化编辑无法保留的注释或语法' : undefined}
          >
            可视化编辑
          </button>
          <button
            className={`tab-item ${editMode === 'text' ? 'active' : ''}`}
            onClick={() => switchEditMode('text')}
          >
            文本编辑
          </button>
        </div>

        {!visualSafe && (
          <div className="card-desc" style={{ marginBottom: 16, color: 'var(--accent-amber)' }}>
            当前 force_ttl_rules.txt 含有可视化编辑无法无损保留的注释或语法，已使用文本编辑以避免保存时重写原文件。
          </div>
        )}

        {loading ? (
          <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>加载中...</div>
        ) : editMode === 'visual' ? (
          <>
            {entries.map((entry) => (
              <div
                key={entry.id}
                style={{
                  marginBottom: 10,
                  padding: 12,
                  background: 'var(--bg-input)',
                  borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border-color)',
                }}
              >
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">域名</label>
                    <input
                      className="form-input"
                      type="text"
                      value={entry.domain}
                      placeholder="cncheck.03k.org"
                      onChange={(e) => updateEntry(entry.id, { domain: e.target.value })}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">规则类型</label>
                    <select
                      className="form-select"
                      value={entry.type === 'cname' ? 'record' : entry.type}
                      onChange={(e) => {
                        const type = e.target.value as TtlRuleEntry['type'];
                        updateEntry(entry.id, { type, matchMode: type === 'forward' ? undefined : entry.matchMode || 'subdomain' });
                      }}
                    >
                      <option value="forward">转发到 DNS 服务器 (@)</option>
                      <option value="record">指定 IP 或 CNAME (@@/@@@)</option>
                    </select>
                  </div>
                </div>
                {entry.type === 'forward' ? (
                  <div className="form-group">
                    <label className="form-label">DNS 服务器</label>
                    <input
                      className="form-input"
                      type="text"
                      value={entry.server}
                      placeholder="129.211.176.224:53 或 129.211.176.224,112.80.181.45"
                      onChange={(e) => updateEntry(entry.id, { server: e.target.value })}
                    />
                    <div className="form-hint">
                      支持 IP:PORT 格式，多服务器用逗号分隔。对应域名 whois 中的 NS 服务器 IP。
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="form-row">
                      <div className="form-group">
                        <label className="form-label">
                          {entry.type === 'record' ? 'A/AAAA 记录值' : 'CNAME 目标域名'}
                        </label>
                        <input
                          className="form-input"
                          type="text"
                          value={entry.record || ''}
                          placeholder={entry.type === 'record' ? '1.2.3.4 或 2404:6800:4008:c06::99' : 'target.example.com'}
                          onChange={(e) => updateEntry(entry.id, { record: e.target.value })}
                        />
                      </div>
                      <div className="form-group">
                        <label className="form-label">匹配方式</label>
                        <select
                          className="form-select"
                          value={entry.matchMode || 'subdomain'}
                          onChange={(e) => updateEntry(entry.id, { matchMode: e.target.value as 'subdomain' | 'exact' })}
                        >
                          <option value="subdomain">子域名匹配 (@@)</option>
                          <option value="exact">精确匹配 (@@@)</option>
                        </select>
                      </div>
                    </div>
                    {entry.type === 'record' && (
                      <div className="form-hint">
                        可指定多条记录，每行一个。支持 IPv4 和 IPv6 地址。
                      </div>
                    )}
                  </>
                )}
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <button className="btn btn-danger btn-sm" onClick={() => removeEntry(entry.id)}>
                    <X size={14} /> 删除规则
                  </button>
                </div>
              </div>
            ))}

            <div className="inline-actions">
              <button className="btn btn-secondary btn-sm" onClick={addEntry}>
                <Plus size={14} /> 添加规则
              </button>
            </div>
          </>
        ) : (
          <textarea
            className="textarea-code"
            value={rawContent}
            onChange={(e) => updateRawContent(e.target.value)}
            rows={15}
          />
        )}

        <div className="suite-editor-actions" style={{ marginTop: 16, borderTop: '1px solid var(--border-color)', paddingTop: 16, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={saving || loading || loadFailed || !!connectionError || !dirty} onClick={handleSave}>
            <Save size={16} /> {saving ? '保存中...' : '保存配置'}
          </button>
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

      <details className="card suite-editor-help"><summary><Clock size={16} /> TTL 语法和生效说明</summary>
        <div className="card-title">
          <Clock size={18} />
          TTL 规则说明
        </div>
        <div className="card-desc">
          RULES_TTL 值大于 0 时生效。规则仅对 A 记录和 AAAA 记录生效。
          保存后按后端返回的监听条件处理，并查询目标域名验证。升级会保留此配置文件。
        </div>
        <table className="info-table">
          <thead>
            <tr><th>格式</th><th>说明</th><th>示例</th></tr>
          </thead>
          <tbody>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@server</code></td>
              <td>转发到指定 DNS 服务器（子域名匹配）</td>
              <td>cncheck.03k.org@129.211.176.224</td>
            </tr>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@server:port</code></td>
              <td>转发到指定 DNS 服务器含端口</td>
              <td>cncheck.03k.org@129.211.176.224:53</td>
            </tr>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@s1,s2</code></td>
              <td>多服务器轮询</td>
              <td>cncheck.03k.org@129.211.176.224,112.80.181.45</td>
            </tr>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@@IP</code></td>
              <td>直接指定 A/AAAA 记录（子域名匹配）</td>
              <td>www.qq.com@@1.2.3.4</td>
            </tr>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@@@IP</code></td>
              <td>直接指定记录（精确匹配）</td>
              <td>www.qq.com@@@1.2.3.4</td>
            </tr>
            <tr>
              <td><code style={{ color: 'var(--accent-cyan)' }}>domain@@CNAME</code></td>
              <td>CNAME 到另一域名（子域名匹配）</td>
              <td>www.qq.com@@qq.03k.org</td>
            </tr>
          </tbody>
        </table>
      </details>

      {loadFailed && !loading && <p role="alert">配置尚未成功读取，保存已停用。<button className="btn btn-secondary btn-sm" onClick={() => void loadFile()}>重新读取</button></p>}
      {ToastComponent}
      </fieldset>
    </div>
  );
}
