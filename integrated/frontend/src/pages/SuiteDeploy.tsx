import { ExternalLink, KeyRound, Network, Server, ShieldCheck, TerminalSquare } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useSuite } from '../suiteContext';
import { errorText, externalApiSetting } from '../suiteApi';
import { Section, Empty, Notice } from '../components/SuiteUI';
import { confirmDiscard } from '../unsaved';
import GatewaySettings from '../components/GatewaySettings';
import { nativeMaintenanceCommands } from '../settingsIdentity';
import CopyableText from '../components/CopyableText';
import SoftwareUpdate from '../components/SoftwareUpdate';
import './SuitePage.css';

export default function SuiteDeploy() {
  const { caps } = useSuite();
  const [gatewayTab, setGatewayTab] = useState(() => new URLSearchParams(window.location.hash.split('?')[1]).get('tab') === 'gateway');
  useEffect(() => {
    const sync = () => setGatewayTab(new URLSearchParams(window.location.hash.split('?')[1]).get('tab') === 'gateway');
    window.addEventListener('hashchange', sync);return () => window.removeEventListener('hashchange', sync);
  }, []);
  const [apiEnabled, setApiEnabled] = useState<boolean | null>(null);
  const [savingApi, setSavingApi] = useState(false);
  const [apiError, setApiError] = useState('');
  useEffect(() => {
    let active = true;
    externalApiSetting().then((value) => { if (active) setApiEnabled(value); })
      .catch((error) => { if (active) setApiError(errorText(error)); });
    return () => { active = false; };
  }, []);
  const changeApi = async () => {
    if (savingApi) return;
    setSavingApi(true); setApiError('');
    try { setApiEnabled(await externalApiSetting(apiEnabled === null ? undefined : !apiEnabled)); }
    catch (error) { setApiError(errorText(error)); }
    finally { setSavingApi(false); }
  };
  if (!caps) return <Empty title="等待 DNS 服务响应">连接后可查看服务版本和主机维护信息。</Empty>;
  const native = caps.deployment === 'native';
  const apiBase = `${window.location.origin}/api/v1/`;
  const installed = caps.installation;
  const provenance = caps.provenance;
  const value = (text?: string | null) => <code>{text && text !== 'unknown' ? text : '未记录'}</code>;
  const commands = native
    ? nativeMaintenanceCommands(installed)
    : 'export WEB_UI_TOKEN=$(openssl rand -hex 32)\nexport DNS_HOST_PORT=1053\nexport WEB_HOST_PORT=18080\ndocker compose -f docker-compose.integrated.yaml build\ndocker compose -f docker-compose.integrated.yaml up -d';
  return <div className="suite-page">
    <div className="suite-segment"><button className={!gatewayTab ? 'is-active' : ''} onClick={() => { if (confirmDiscard()) { setGatewayTab(false);window.history.replaceState(null, '', '#/settings'); } }}>服务信息</button><button className={gatewayTab ? 'is-active' : ''} onClick={() => { if (confirmDiscard()) { setGatewayTab(true);window.history.replaceState(null, '', '#/settings?tab=gateway'); } }}>网关联动</button></div>
    {gatewayTab ? <GatewaySettings /> : <>
    <div className="suite-deploy-hero"><div><span className="suite-kicker">{native ? 'DEBIAN / SYSTEMD' : 'DOCKER / COMPOSE'}</span><h2>LovestDNS</h2><p>管理本机 DNS 解析、缓存与规则，查看服务状态并排查问题。</p></div><Server size={46} /></div>
    <Notice tone="info">{native ? '日常 DNS 参数与规则可直接在网页修改、保存和应用。软件更新可在下方检查并执行，监听端口变更由主机处理。' : '当前为 Docker 集成版。正式升级、端口切换和数据迁移由宿主机 Compose 执行。'}</Notice>
    {native && <SoftwareUpdate loadedVersion={caps.version} />}
    <div className="suite-dashboard-grid suite-settings-grid">
      <Section title="当前实例" subtitle="实例身份与原生安装信息" icon={Network}><dl className="suite-kpi-list suite-build-list">
        <div><dt>实例名称</dt><dd>{caps.name || '未记录'}</dd></div><div><dt>持久实例 ID</dt><dd>{value(caps.instance_id)}</dd></div>
        <div><dt>服务管理</dt><dd>{native ? 'systemd' : 'Docker Compose'}</dd></div><div><dt>DNS 配置端口</dt><dd>{caps.dns_defaults.port} / UDP + TCP</dd></div>
        {native ? <><div><dt>服务单元</dt><dd>{value(installed?.service_name)}</dd></div><div><dt>当前发布目录</dt><dd>{value(installed?.release_path)}</dd></div>
          <div><dt>数据目录</dt><dd>{value(installed?.data_dir)}</dd></div><div><dt>状态目录</dt><dd>{value(installed?.state_dir)}</dd></div></>
          : <><div><dt>数据卷</dt><dd><code>suite-dns-data → /data</code></dd></div><div><dt>状态卷</dt><dd><code>suite-state</code></dd></div></>}
      </dl></Section>
      <Section title="应用与前端" subtitle="安装文件中记录的构建来源" icon={ShieldCheck}><dl className="suite-kpi-list suite-build-list">
        <div><dt>LovestDNS 版本</dt><dd>{caps.version}</dd></div><div><dt>API 版本</dt><dd>v{caps.api_version}</dd></div>
        <div><dt>应用源码提交</dt><dd>{value(provenance?.application.source_commit)}</dd></div><div><dt>前端源码提交</dt><dd>{value(provenance?.frontend.source_commit)}</dd></div>
        <div><dt>前端依赖安装</dt><dd>{value(provenance?.frontend.dependency_mode)}{provenance?.frontend.working_tree_modified === true && ' · 构建时有未提交修改'}</dd></div>
        <div><dt>安装包 SHA-256</dt><dd>{value(installed?.receipt.status === 'recorded' ? installed.receipt.archive_sha256 : null)}</dd></div>
      </dl></Section>
    </div>
    {installed?.receipt.status === 'mismatch' && <Notice tone="warning">安装收据与当前发布不一致，归档摘要暂不可确认。</Notice>}
    <Section title="组件与基础包" subtitle="包内声明；当前页面未核对实际进程文件" icon={ShieldCheck}><dl className="suite-kpi-list suite-build-list">
      <div><dt>上游核心来源</dt><dd>{value(caps.core.image)}</dd></div><div><dt>上游核心提交</dt><dd>{value(caps.core.source_commit)}</dd></div>
      <div><dt>继承基础包版本</dt><dd>{value(provenance?.base_package.version)}</dd></div><div><dt>基础包 SHA-256</dt><dd>{value(provenance?.base_package.archive_sha256)}</dd></div>
      <div><dt>独立组件版本声明</dt><dd>{caps.core.components ? Object.entries(caps.core.components.versions).map(([name, version]) => <span key={name}><code>{name}: {version}</code>{' '}</span>) : '未记录'}</dd></div>
      <div><dt>组件清单 SHA-256</dt><dd>{value(caps.core.components?.lock_sha256)}</dd></div>
    </dl><details className="suite-debug"><summary>查看来源摘要与安装记录</summary><dl className="suite-kpi-list suite-build-list">
      <div><dt>应用源码摘要</dt><dd>{value(provenance?.application.source_sha256)}</dd></div><div><dt>前端源码摘要</dt><dd>{value(provenance?.frontend.source_sha256)}</dd></div>
      <div><dt>前端构建清单摘要</dt><dd>{value(provenance?.frontend.manifest_sha256)}</dd></div><div><dt>当前安装清单摘要</dt><dd>{value(installed?.receipt.manifest_sha256)}</dd></div>
      <div><dt>安装时间</dt><dd>{installed?.receipt.status === 'recorded' && installed.receipt.installed_at !== null ? new Date(installed.receipt.installed_at * 1000).toLocaleString() : '未记录'}</dd></div>
      <div><dt>来源记录</dt><dd>{value(provenance?.application.method)}</dd></div>
    </dl></details></Section>
    <Section title="API 与 Token" subtitle="供外部程序接入；网页登录使用独立的管理员密码" icon={KeyRound}>
      <div className="suite-api-setting"><strong>外部 API：{apiEnabled === null ? apiError ? '读取失败' : '正在读取' : apiEnabled ? '已开启' : '已关闭'}</strong>
        <button className="btn btn-secondary" aria-pressed={apiEnabled ?? false} disabled={(apiEnabled === null && !apiError) || savingApi} onClick={() => void changeApi()}>{savingApi ? '正在保存…' : apiEnabled === null ? '重新读取' : apiEnabled ? '关闭外部 API' : '开启外部 API'}</button></div>
      {apiError && <Notice tone="error">{apiError}</Notice>}
      <p className="suite-muted">默认关闭。关闭后拒绝外部 Token 调用，网页管理和 DNS 解析照常使用。设置立即生效并保留，服务重启后无需重新设置。</p>
      <dl className="suite-kpi-list">
        <div><dt>API 地址</dt><dd><code>{apiBase}</code></dd></div>
        <div><dt>接口文档</dt><dd><a href="/api/v1/openapi.json" target="_blank" rel="noreferrer">查看 OpenAPI 文档</a>（登录后可查看）</dd></div>
        <div><dt>只读 Token</dt><dd><code>SUITE_READ_TOKEN</code>：读取状态、指标与执行诊断</dd></div>
        <div><dt>管理 Token</dt><dd><code>WEB_UI_TOKEN</code>：修改配置、执行维护任务</dd></div>
        <div><dt>配置位置</dt><dd>{native ? value(installed?.config_file) : '宿主机 Compose 使用的私有环境文件'}</dd></div>
        <div><dt>调用方式</dt><dd><code>Authorization: Bearer &lt;Token&gt;</code></dd></div>
      </dl>
      <p className="suite-muted">Token 在主机配置，修改后重启服务加载。两个 Token 应分别设置；当前网页不显示或修改 Token。</p>
    </Section>
    <Section title={native ? '宿主机维护命令' : '开发验证命令'} subtitle={native ? '对应当前实例；在主机终端执行' : '1053/18080 适合本机验证'} icon={TerminalSquare}>
      {commands ? <CopyableText text={commands} fileName="lovestdns-maintenance.txt" /> : <Notice tone="info">当前实例未提供可核对的维护命令，请使用安装时返回的控制命令。</Notice>}<p className="suite-muted">{native ? '原生部署、更新与回退流程见项目 docs/lovestdns-native-deployment.md。' : 'Docker 部署与旧数据迁移见项目 docs/lovestdns-pve-deployment.md。'}核心升级需使用适配并验收过的 LovestDNS 版本；规则库按配置的更新周期处理。</p>
    </Section>
    <a className="suite-doc-link" href="https://github.com/Lovest20018/LovestDNS" target="_blank" rel="noreferrer"><ExternalLink size={15} />查看项目文档</a>
    </>}
  </div>;
}
