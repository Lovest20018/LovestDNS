const LABELS: Record<string, string> = {
  'custom_env.ini': '基础设置',
  'unbound_custom.conf': 'Unbound 配置',
  'custom_mod.yaml': '自定义分流',
  'force_forward_list.txt': '强制转发列表',
  'force_dnscrypt_list.txt': '强制 DNSCrypt 列表',
  'force_recurse_list.txt': '强制递归列表',
  'force_ttl_rules.txt': 'TTL 规则',
  'custom_cn_mark.txt': '自定义 CN 域名标记',
  'trackerslist.txt': 'Tracker 列表',
};
export const configFileLabel = (file: string) => LABELS[file] ?? file;
export function configFileLink(file: string) {
  if (file === 'custom_env.ini') return '#/env';
  if (file === 'force_ttl_rules.txt') return '#/ttl';
  const page = file === 'unbound_custom.conf' || file === 'custom_mod.yaml' ? 'advanced' : 'lists';
  return `#/${page}?file=${encodeURIComponent(file)}`;
}
export function configFileAction(file: string, native: boolean) {
  if (file === 'custom_env.ini' || file === 'unbound_custom.conf') return native ? '重启 DNS 加载' : '通过宿主机加载';
  return file === 'custom_mod.yaml' ? '校验并应用分流' : '核对监听条件并验证域名';
}
