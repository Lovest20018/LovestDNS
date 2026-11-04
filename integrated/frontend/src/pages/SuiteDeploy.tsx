import { Copy, ExternalLink, Network, Server, ShieldCheck, TerminalSquare } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { Section, Empty, Notice } from '../components/SuiteUI';
import './SuitePage.css';

export default function SuiteDeploy() {
  const { caps } = useSuite();
  if (!caps) return <Empty title="等待 DNS 服务响应">连接后可查看服务版本和主机维护信息。</Empty>;
  const native = caps.deployment === 'native';
  const commands = native
    ? 'sudo lovestdnsctl status\nsudo lovestdnsctl health\nsudo lovestdnsctl restart\n# 升级时使用已验证的安装包与 SHA-256\nsudo lovestdnsctl upgrade /path/to/lovestdns.tar.gz --sha256 <SHA-256>'
    : 'export WEB_UI_TOKEN=$(openssl rand -hex 32)\nexport DNS_HOST_PORT=1053\nexport WEB_HOST_PORT=18080\ndocker compose -f docker-compose.integrated.yaml build\ndocker compose -f docker-compose.integrated.yaml up -d';
  return <div className="suite-page">
    <div className="suite-deploy-hero"><div><span className="suite-kicker">{native ? 'DEBIAN / SYSTEMD' : 'DOCKER / COMPOSE'}</span><h2>LovestDNS</h2><p>DNS 核心、采集器、维护服务和本地配置台，可独立于 DNS 管理 运行。</p></div><Server size={46} /></div>
    <Notice tone="info">{native ? '日常 DNS 参数与规则可直接在网页修改、保存和应用。此页用于查看版本及主机维护命令；软件升级与监听端口变更由主机处理。' : '当前为 Docker 集成版。正式升级、端口切换和数据迁移由宿主机 Compose 执行。'}</Notice>
    <div className="suite-dashboard-grid">
      <Section title="当前构建" subtitle="来自当前实例" icon={ShieldCheck}><dl className="suite-kpi-list">
        <div><dt>LovestDNS 版本</dt><dd>{caps.version}</dd></div><div><dt>API 版本</dt><dd>v{caps.api_version}</dd></div>
        <div><dt>上游核心来源</dt><dd><code>{caps.core.image}</code></dd></div><div><dt>上游提交</dt><dd><code>{caps.core.source_commit}</code></dd></div>
      </dl></Section>
      <Section title="运行方式" subtitle="端口与目录以实际安装配置为准" icon={Network}><dl className="suite-kpi-list">
        <div><dt>服务管理</dt><dd>{native ? 'systemd' : 'Docker Compose'}</dd></div><div><dt>DNS 查询端口</dt><dd>{caps.dns_defaults.port} / UDP + TCP</dd></div>
        <div><dt>{native ? '默认数据目录' : '数据目录'}</dt><dd><code>{native ? '/var/lib/lovestdns/data' : 'suite-dns-data → /data'}</code></dd></div>
        <div><dt>{native ? '默认状态目录' : '面板状态'}</dt><dd><code>{native ? '/var/lib/lovestdns/state' : 'suite-state'}</code></dd></div>
      </dl></Section>
    </div>
    <Section title={native ? '宿主机维护命令' : '开发验证命令'} subtitle={native ? '默认实例使用 lovestdnsctl；命名实例使用安装时返回的控制命令' : '1053/18080 适合本机验证'} icon={TerminalSquare}
      actions={<button className="btn btn-secondary btn-sm" onClick={() => void navigator.clipboard?.writeText(commands)}><Copy size={14} />复制</button>}>
      <pre className="suite-log">{commands}</pre><p className="suite-muted">{native ? '原生部署、更新与回退流程见项目 docs/lovestdns-native-deployment.md。' : 'Docker 部署与旧数据迁移见项目 docs/lovestdns-pve-deployment.md。'}核心升级需使用适配并验收过的 LovestDNS 版本；规则库按配置的更新周期处理。</p>
    </Section>
    <a className="suite-doc-link" href="https://github.com/Lovest20018/PaoPaoDNS-WebUI" target="_blank" rel="noreferrer"><ExternalLink size={15} />查看项目文档</a>
  </div>;
}
