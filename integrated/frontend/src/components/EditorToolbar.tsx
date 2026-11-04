import { Save } from 'lucide-react';

export default function EditorToolbar({ file, dirty, busy, blocked, message, failed, onSave }: {
  file: string; dirty: boolean; busy: boolean; blocked: boolean; message?: string; failed?: boolean; onSave: () => void;
}) {
  return <div className="suite-file-toolbar-wrap">
    <div className="suite-editor-toolbar suite-file-toolbar"><span><code>{file}</code><small>{dirty ? '有未保存修改' : '仅保存当前文件'}</small></span><button className="btn btn-primary" disabled={busy || blocked} onClick={onSave}><Save size={15} />{busy ? '保存中…' : '保存配置'}</button></div>
    {message && <p className={`suite-editor-result ${failed ? 'is-error' : ''}`} role={failed ? 'alert' : 'status'}>{message}</p>}
  </div>;
}
