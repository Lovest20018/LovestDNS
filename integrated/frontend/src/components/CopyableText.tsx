import { useRef, useState } from 'react';
import { Copy, Download } from 'lucide-react';
import { copyText } from '../copyText';

export default function CopyableText({ text, fileName }: { text: string; fileName: string }) {
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'manual'>('idle');
  const selection = useRef<HTMLTextAreaElement>(null);
  const copy = async () => {
    setState('copying');
    setState(await copyText(text) ? 'copied' : 'manual');
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = fileName; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <div className="suite-copy-text">
    <div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={state === 'copying'} onClick={() => void copy()}><Copy size={14} />{state === 'copying' ? '复制中…' : '复制'}</button><button className="btn btn-secondary btn-sm" onClick={download}><Download size={14} />下载文本</button></div>
    <p role="status" aria-live="polite" className="suite-note">{state === 'copied' ? '已复制到剪贴板。' : state === 'manual' ? '浏览器无法自动复制。请选中下方全部文本，使用系统复制操作，或下载文本。' : ''}</p>
    {state === 'manual' ? <><button className="btn btn-secondary btn-sm" onClick={() => { selection.current?.focus(); selection.current?.select(); }}>选择全部文本</button><textarea ref={selection} aria-label="手动复制文本" className="textarea-code suite-copy-selection" readOnly value={text} onFocus={event => event.target.select()} rows={8} /></> : <pre className="suite-log">{text}</pre>}
  </div>;
}
