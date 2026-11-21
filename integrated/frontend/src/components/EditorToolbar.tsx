import { useSuite } from '../suiteContext';
import { Save } from 'lucide-react';
import ConfigFeedback, { ConfigNextStep } from './ConfigFeedback';
import type { SaveResult } from '../api';

export default function EditorToolbar({ file, dirty, busy, blocked, message, failed, result, change, onSave }: {
  file: string; dirty: boolean; busy: boolean; blocked: boolean; message?: string; failed?: boolean; result?: SaveResult;
  change?: { summary: string; domain?: string }; onSave: () => void;
}) {
  const { caps } = useSuite();
  return <div className="suite-file-toolbar-wrap">
    <div className="suite-editor-toolbar suite-file-toolbar suite-editor-toolbar-feedback"><span><code>{file}</code><small>{dirty ? '有未保存修改' : '无未保存修改'}</small></span><button className="btn btn-primary" disabled={busy || blocked || !dirty} onClick={onSave}><Save size={15} />{busy ? '保存中…' : '保存配置'}</button>
      {caps?.permissions.maintain && <a href={`#/system?tab=history&file=${encodeURIComponent(file)}`}>查看此文件历史</a>}
      <ConfigNextStep message={message} failed={failed} />
    </div>
    <ConfigFeedback key={file} file={file} dirty={dirty} saved={!!message && !failed} result={result} change={change} />
  </div>;
}
