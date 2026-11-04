import { useSuite } from '../suiteContext';
import type { SaveResult } from '../api';

export default function ConfigFeedback({ saved, dirty, file, result }: { saved: boolean; dirty: boolean; file: string; result?: SaveResult }) {
  const { caps } = useSuite();
  const native = caps?.deployment === 'native';
  const restart = result?.requires_restart || ['custom_env.ini', 'unbound_custom.conf'].includes(file);
  const apply = result?.requires_reload || file === 'custom_mod.yaml';
  return <div className="suite-editor-next" role="status">{dirty ? <span>{file} · 有未保存修改</span> : saved ? <><span>{file} · 文件已保存，解析效果尚未验证。</span>{restart ? <a href={native ? "#/" : "#/deploy"}>下一步：{native ? "重启 DNS 核心" : "查看主机应用说明"}</a> : apply ? <a href="#/">下一步：预览并应用自定义规则</a> : <span>按保存结果确认文件监听条件。</span>}<a href="#/diagnostics">查询目标域名验证</a></> : <span>保存配置后，按结果提示应用并查询目标域名验证。</span>}</div>;
}
