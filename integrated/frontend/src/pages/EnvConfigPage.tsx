import { useState } from 'react';
import { RefreshCw, Save, Settings2 } from 'lucide-react';
import { useFileEditor } from '../useFileEditor';
import { ENV_VARS, GROUP_LABELS } from '../types';
import { useSuiteConfig } from '../suiteContext';
import { errorText } from '../suiteApi';
import { envOverrides, editOverride } from '../envEditor';
import { useUnsavedChanges } from '../unsaved';
import ConfigFeedback from '../components/ConfigFeedback';
import { Notice, Section } from '../components/SuiteUI';

export default function EnvConfigPage() {
  const { caps, runningEnv, statusError, error: connectionError } = useSuiteConfig();
  const running = runningEnv ?? {};
  const [search, setSearch] = useState('');
  const editor = useFileEditor('custom_env.ini');
  const { content, setContent, loaded, busy, dirty } = editor;
  const saved = editor.baseline ?? '';
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const load = () => editor.load(true);
  useUnsavedChanges(dirty);
  const values = envOverrides(content);
  const savedValues = envOverrides(saved);
  const native = caps?.deployment === 'native';
  const variables = ENV_VARS.filter((v) => !['DNSPORT', 'HTTP_FILE', 'USE_HOSTS'].includes(v.key));
  const save = async () => {
    setError(''); setMessage('');
    try {
      for (const v of variables) {
        if (!Object.hasOwn(values, v.key)) continue;
        const value = values[v.key];
        if (!value) throw new Error(`${v.label}不能为空；如需使用启动设置，请关闭“覆盖”。`);
        if (v.type === 'number' && (!/^\d+$/.test(value) || Number(value) < (v.min ?? 0) || Number(value) > (v.max ?? Number.MAX_SAFE_INTEGER))) throw new Error(`${v.label}超出有效范围。`);
      }
      const result = await editor.save();
      if (result?.success) setMessage(result.msg);
    } catch (e) { setError(errorText(e)); }
  };
  return <div className="suite-page">
    <Notice>基础设置与高级配置共用 custom_env.ini 草稿。<a href="#/advanced?file=custom_env.ini">编辑源文件</a>。保存后在本页展开“应用与查询验证”。启用“覆盖”时使用此处的值；关闭后使用服务启动设置。自定义变量和注释会保留。</Notice>
    <div className="suite-editor-toolbar"><span>{dirty ? '有未保存修改' : loaded ? '配置已读取' : '等待读取配置'}</span><div><button className="btn btn-secondary" aria-label="重新读取配置" title="重新读取配置" disabled={busy} onClick={() => void load()}><RefreshCw size={15} />重新读取</button><button className="btn btn-primary" disabled={busy || !loaded || !dirty || !!connectionError} onClick={() => void save()}><Save size={15} />{busy ? '处理中…' : '保存配置'}</button></div></div>
    {(error || editor.error || editor.result?.success === false) && <Notice tone="error">{error || editor.error || editor.result?.msg}</Notice>}{message && <Notice>{message}</Notice>}
    <ConfigFeedback file="custom_env.ini" saved={!!message} dirty={dirty} result={editor.result?.result} />
    <div className="suite-settings-find"><label htmlFor="settings-search">查找设置<input id="settings-search" className="form-input" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索名称、变量或说明" /></label>{search && <button className="btn btn-secondary btn-sm" onClick={() => setSearch('')}>清除搜索</button>}<nav aria-label="设置分组">{Object.entries(GROUP_LABELS).map(([group, info]) => <a key={group} href={`#settings-${group}`} onClick={(e) => { e.preventDefault(); setSearch(''); setTimeout(() => document.getElementById(`settings-${group}`)?.scrollIntoView({ behavior: 'smooth' }), 0); }}>{info.label}</a>)}</nav></div>
    {statusError && <Notice tone="warning">运行值读取失败，当前不展示旧值：{statusError}</Notice>}
    {!variables.some((v) => `${v.label} ${v.key} ${v.description}`.toLowerCase().includes(search.trim().toLowerCase())) && <Notice>没有匹配设置。<button className="btn btn-secondary btn-sm" onClick={() => setSearch('')}>显示全部设置</button></Notice>}
    {Object.entries(GROUP_LABELS).filter(([group]) => variables.some((v) => v.group === group && `${v.label} ${v.key} ${v.description}`.toLowerCase().includes(search.trim().toLowerCase()))).map(([group, info]) => <div id={`settings-${group}`} key={group} className="suite-settings-group"><Section title={info.label} icon={Settings2}>
      <div className="suite-settings-grid">{variables.filter((v) => v.group === group && `${v.label} ${v.key} ${v.description}`.toLowerCase().includes(search.trim().toLowerCase())).map((v) => {
        const enabled = Object.hasOwn(values, v.key);
        const runtime = running[v.key];
        const itemDirty = enabled !== Object.hasOwn(savedValues, v.key) || values[v.key] !== savedValues[v.key];
        const value = values[v.key] ?? runtime ?? v.defaultValue;
        const cnOnly = v.requiresCNAUTO && (values.CNAUTO ?? running.CNAUTO) !== 'yes';
        const description = native && v.key === 'UPDATE' ? '按每日、7 天或 30 天间隔检查规则和 GEOIP 更新；关闭后不执行定时更新。'
          : native && v.key === 'SERVER_IP' ? '向客户端公布的 DNS 服务器地址，并用于 paopao.dns 记录。不会更改主机监听地址。'
          : v.description.replace(/Docker 启动时的|容器/g, '服务');
        return <div className={`suite-setting ${cnOnly ? 'is-inactive' : ''}`} key={v.key}>
          <label className="suite-setting-title" htmlFor={`setting-${v.key}`}><strong>{v.label}</strong><code>{v.key}</code></label><p>{description}</p>
          <label className="suite-override"><input type="checkbox" checked={enabled} disabled={busy || !loaded} onChange={(e) => { setMessage(''); setContent(editOverride(content, v.key, e.target.checked ? value : null)); }} />覆盖服务启动设置</label>
          {v.type === 'select' ? <select id={`setting-${v.key}`} className="form-select" value={value} disabled={!enabled || busy} onChange={(e) => { setMessage(''); setContent(editOverride(content, v.key, e.target.value)); }}>{!v.options?.some((o) => o.value === value) && <option value={value}>{value || '未设置'}</option>}{v.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
            : <input id={`setting-${v.key}`} className="form-input" type={v.type === 'number' ? 'number' : 'text'} min={v.min} max={v.max} value={value} placeholder={v.placeholder} disabled={!enabled || busy} onChange={(e) => { setMessage(''); setContent(editOverride(content, v.key, e.target.value)); }} />}
          <small>运行值：{runtime ?? '未采集'}{itemDirty ? (enabled ? ' · 草稿：覆盖值未保存' : ' · 草稿：改用服务启动设置，未保存') : enabled && runtime !== undefined && value !== runtime ? ' · 已保存目标与运行值不同，待应用确认' : ''}{cnOnly ? ' · 当前目标模式未开启智能分流' : ''}</small>
        </div>;
      })}</div>
    </Section></div>)}
    <Notice>DNS 监听地址和端口、Web 端口、目录与资源限制由主机服务配置管理。{native ? '修改这些参数后使用实例控制命令重启服务。' : '修改后通过主机重新创建容器。'} <a href="#/deploy">查看服务信息</a></Notice>
  </div>;
}
