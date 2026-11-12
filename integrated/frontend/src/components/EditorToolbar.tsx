import { Save } from 'lucide-react';
import ConfigFeedback from './ConfigFeedback';
import type { SaveResult } from '../api';

export default function EditorToolbar({ file, dirty, busy, blocked, message, failed, result, onSave }: {
  file: string; dirty: boolean; busy: boolean; blocked: boolean; message?: string; failed?: boolean; result?: SaveResult; onSave: () => void;
}) {
  return <div className="suite-file-toolbar-wrap">
    <div className="suite-editor-toolbar suite-file-toolbar"><span><code>{file}</code><small>{dirty ? '有未保存修改' : '无未保存修改'}</small></span><button className="btn btn-primary" disabled={busy || blocked || !dirty} onClick={onSave}><Save size={15} />{busy ? '保存中…' : '保存配置'}</button></div>
    {message && <p className={`suite-editor-result ${failed ? 'is-error' : ''}`} role={failed ? 'alert' : 'status'}>{message}</p>}
    <ConfigFeedback key={file} file={file} dirty={dirty} saved={!!message && !failed} result={result} />
  </div>;
}
