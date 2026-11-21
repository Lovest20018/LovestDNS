import { LIST_FILE_INFO } from './types';

export const DOMAIN_LISTS = [
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

export type DomainList = (typeof DOMAIN_LISTS)[number];

export function domainListForFile(file: string | null) {
  return DOMAIN_LISTS.find((list) => list.filename === file);
}

export function domainListLabel(list: DomainList) {
  return list.title.replace('强制 ', '').replace('域名列表', '');
}
