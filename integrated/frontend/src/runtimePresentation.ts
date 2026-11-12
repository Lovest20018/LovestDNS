import type { RuntimeSnapshot } from './suiteApi';

type Process = RuntimeSnapshot['components'][string];
type Tone = 'good' | 'warning' | 'error' | 'neutral';
const COMPONENTS: Record<string, [string, string]> = {
  mosdns: ['分流', '接收设备的 DNS 查询，按规则选择解析方式'],
  'unbound-raw': ['递归', '向 DNS 系统逐级查找域名，并缓存结果'],
  'unbound-forward': ['转发', '把指定查询交给配置的上游 DNS，并缓存结果'],
  redis: ['共享缓存', '保存已查到的结果，供后续查询重复使用'],
  dnscrypt: ['加密上游', '通过加密连接向上游 DNS 查询'],
  'dnscrypt-socks': ['代理通道', '按配置通过代理连接加密 DNS；未运行不一定是故障'],
  watcher: ['规则自动加载', '关注规则文件变化，触发对应的加载操作'],
  observer: ['状态采集', '读取组件、查询量和缓存指标，供本页展示'],
  manager: ['配置任务', '执行网页提交的应用配置、重启和缓存保存任务'],
  web: ['管理网页', '提供当前的网页和管理接口'],
  cron: ['定时更新', '按设定周期安排规则数据更新'],
  updater: ['本次数据更新', '本次规则数据下载任务；失败时可查看维护日志'],
  bootstrap: ['启动准备', '生成运行配置并启动 DNS 组件，完成后会退出'],
};

export function componentSummary(name: string, process: Process): { label: string; description: string; status: string; tone: Tone } {
  const [description] = COMPONENTS[name] ?? [name];
  const label = name === 'redis' ? 'Redis' : name;
  const state = process.statename;
  if (state === 'RUNNING') return { label, description, status: name === 'updater' ? '正在检查更新' : '运行中', tone: 'good' };
  if (state === 'EXITED' && process.exitstatus === 0 && ['bootstrap', 'updater'].includes(name)) return { label, description, status: name === 'bootstrap' ? '准备完成' : '本次更新已完成', tone: 'good' };
  if (state === 'EXITED') return { label, description, status: name === 'updater' ? '本次更新未完成' : process.exitstatus ? '异常退出' : '已结束', tone: process.exitstatus ? 'warning' : 'neutral' };
  if (state === 'STOPPED') return { label, description, status: name === 'updater' ? '当前无更新任务' : '已停止', tone: 'neutral' };
  if (['STARTING', 'BACKOFF'].includes(state)) return { label, description, status: state === 'STARTING' ? '启动中' : '启动重试中', tone: 'warning' };
  if (state === 'STOPPING') return { label, description, status: '正在停止', tone: 'neutral' };
  return { label, description, status: ['FATAL', 'UNKNOWN'].includes(state) ? '需要检查' : state, tone: 'error' };
}

export function cacheSaveSummary(redis?: RuntimeSnapshot['redis']): { value: string; detail: string } {
  if (!redis?.available) return { value: '暂未读取', detail: '等待缓存服务返回数据' };
  const info = redis.info ?? {};
  if (info.rdb_bgsave_in_progress === 1) return { value: '正在保存', detail: '后台写入磁盘，查询仍可继续' };
  if (info.rdb_last_bgsave_status === 'err') return { value: '上次保存失败', detail: '请到维护与日志查看磁盘或保存错误' };
  return { value: info.rdb_last_bgsave_status === 'ok' ? '上次保存成功' : '暂无保存结果', detail: `上次保存后 ${String(info.rdb_changes_since_last_save ?? '—')} 次数据变更；当前内存可能含未落盘内容` };
}
