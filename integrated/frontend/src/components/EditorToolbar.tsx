import { useId } from 'react';
import { useSuiteConfig } from '../suiteContext';
import { RefreshCw, Save } from 'lucide-react';
import ConfigFeedback, { ConfigNextStep } from './ConfigFeedback';
import type { SaveResult } from '../api';

export default function EditorToolbar({ file, dirty, busy, loaded, readError, message, failed, result, change, onSave, onReload }: {
  file: string; dirty: boolean; busy: boolean; loaded: boolean; readError: string;
  message?: string; failed?: boolean; result?: SaveResult;
  change?: { summary: string; domain?: string }; onSave: () => void; onReload?: () => void;
}) {
  const { caps, error } = useSuiteConfig();
  const status = useId();
  const reason = busy ? loaded ? '正在处理配置' : '正在读取配置'
    : error ? '连接中断，恢复连接后可保存'
      : readError ? '请核对文件并重新读取后保存'
        : !loaded ? '等待读取配置' : !dirty ? '无未保存修改' : '';
  return <div className="suite-file-toolbar-wrap">
    <div className="suite-editor-toolbar suite-file-toolbar">
      <span><code>{file}</code><small id={status} role="status">{reason || '有未保存修改'}</small></span>
      <div className="suite-file-toolbar-actions">
        {onReload && <button className="btn btn-secondary" type="button" aria-label="重新读取配置" disabled={busy} onClick={onReload}>
          <RefreshCw size={15} />重新读取
        </button>}
        <button className="btn btn-primary" type="button" disabled={!!reason} aria-describedby={status} onClick={onSave}>
          <Save size={15} />{busy ? '处理中…' : '保存配置'}
        </button>
        {caps?.permissions.maintain && <a href={`#/system?tab=history&file=${encodeURIComponent(file)}`}>查看此文件历史</a>}
      </div>
      <ConfigNextStep message={message} failed={failed} />
    </div>
    <ConfigFeedback key={file} file={file} dirty={dirty} saved={!!message && !failed} result={result} change={change} />
  </div>;
}
