import type { SaveResult } from './api';

export function saveFeedback(result: SaveResult) {
  if (result.requires_restart) return '已保存；重启 DNS 核心的处理状态见本页“应用与查询验证”。';
  if (result.requires_reload) return '已保存；预览与应用状态见本页“应用与查询验证”。';
  if (result.watched_now) return '已保存；保存时运行配置满足文件监听条件，请查询目标域名确认生效。';
  return `已保存；${result.condition || '请检查运行配置并验证解析。'}`;
}
