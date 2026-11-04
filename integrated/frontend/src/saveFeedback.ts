import type { SaveResult } from './api';

export function saveFeedback(result: SaveResult) {
  if (result.requires_restart) return '已保存，待重启 DNS 核心。请到配置中心执行，再验证解析。';
  if (result.requires_reload) return '已保存，待应用。请到配置中心预览并应用，再验证解析。';
  if (result.watched_now) return '已保存；运行配置满足文件监听条件，请查询目标域名确认生效。';
  return `已保存；${result.condition || '请检查运行配置并验证解析。'}`;
}
