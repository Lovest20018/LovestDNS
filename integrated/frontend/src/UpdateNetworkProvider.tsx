import { useCallback, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { errorText, softwareUpdate, softwareUpdateNetwork } from './suiteApi';
import type { UpdateNetwork } from './suiteApi';
import { UpdateNetworkContext } from './updateNetworkContext';
import type { UpdateNetworkDraft } from './updateNetworkContext';
import { useUnsavedChanges } from './unsaved';

const draftOf = (value: UpdateNetwork): UpdateNetworkDraft => ({
  mode: value.mode, host: value.host, username: value.username, channel: value.channel,
  port: value.port?.toString() ?? '', password: '', clear_password: false,
});
type Editor = { saved: UpdateNetwork; draft: UpdateNetworkDraft };
const isDirty = (editor: Editor | null) => !!editor && JSON.stringify(editor.draft) !== JSON.stringify(draftOf(editor.saved));

export default function UpdateNetworkProvider({ children }: { children: ReactNode }) {
  // The authenticated instance owns this state. Password input stays in memory
  // while the settings and maintenance routes share the same draft.
  const [editor, setEditor] = useState<Editor | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const write = useRef(false);
  const generation = useRef(0);
  const dirty = isDirty(editor);
  useUnsavedChanges(dirty || saving);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (write.current) return;
    const request = ++generation.current;
    try {
      const saved = await softwareUpdateNetwork(undefined, signal);
      if (!signal?.aborted && request === generation.current) {
        setEditor(current => isDirty(current) ? current : { saved, draft: draftOf(saved) });
        setError('');
      }
    } catch (error) {
      if (!signal?.aborted && request === generation.current) setError(errorText(error));
    }
  }, []);

  const edit = (change: Partial<UpdateNetworkDraft>) => setEditor(current =>
    current && { ...current, draft: { ...current.draft, ...change } });
  const restore = () => {
    setEditor(current => current && { ...current, draft: draftOf(current.saved) });
    setError('');
  };
  const save = async () => {
    if (!editor || write.current) return;
    const { draft } = editor;
    write.current = true;
    generation.current++;
    setSaving(true); setError(''); setMessage('');
    try {
      const saved = await softwareUpdateNetwork(draft.mode === 'direct'
        ? { mode: 'direct', host: '', port: null, username: '', clear_password: true, channel: draft.channel }
        : { ...draft, port: Number(draft.port) });
      setEditor({ saved, draft: draftOf(saved) });
      setMessage('更新设置已保存。');
      await softwareUpdate({ action: 'check' });
      setMessage('更新设置已保存，已提交版本检查。');
    } catch (error) {
      setError(errorText(error));
    } finally {
      write.current = false;
      setSaving(false);
    }
  };
  return <UpdateNetworkContext.Provider value={{
    saved: editor?.saved ?? null, draft: editor?.draft ?? null, dirty, saving, message, error, load, edit, restore, save,
  }}>{children}</UpdateNetworkContext.Provider>;
}
