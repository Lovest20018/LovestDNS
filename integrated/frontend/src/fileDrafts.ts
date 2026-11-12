// Memory-only, instance-scoped editor documents. Auth recovery and route changes
// retain these; explicit logout clears them. Configuration never enters storage.
export interface FileDraft {
  content: string; baseline: string | null; revision: string; loaded: boolean;
}
let instance = '';
let epoch = 0;
const documents = new Map<string, FileDraft>();
const baselineVersions = new Map<string, number>();
const listeners = new Map<string, Set<() => void>>();
function notify(file?: string) {
  for (const [key, subscribers] of listeners) if (!file || key === file || key === '*') for (const subscriber of subscribers) subscriber();
}
export function subscribeFileDraft(file: string, listener: () => void) {
  let subscribers = listeners.get(file);
  if (!subscribers) { subscribers = new Set(); listeners.set(file, subscribers); }
  subscribers.add(listener);
  return () => { subscribers.delete(listener); if (!subscribers.size) listeners.delete(file); };
}
export function setDraftInstance(value: string) {
  if (instance && instance !== value) { documents.clear(); baselineVersions.clear(); epoch++; notify(); }
  instance = value;
}
export function clearFileDrafts() { documents.clear(); baselineVersions.clear(); instance = ''; epoch++; notify(); }
export function draftEpoch() { return epoch; }
export function fileDraft(file: string) { return documents.get(file); }
export function fileBaselineVersion(file: string) { return baselineVersions.get(file) ?? 0; }
function advanceBaseline(file: string) { baselineVersions.set(file, fileBaselineVersion(file) + 1); }
export function rememberFileDraft(file: string, value: FileDraft, expectedEpoch = epoch) {
  if (expectedEpoch !== epoch) return false;
  const previous = documents.get(file);
  if (!previous || previous.baseline !== value.baseline || previous.revision !== value.revision || previous.loaded !== value.loaded) advanceBaseline(file);
  documents.set(file, value); notify(file); return true;
}
export function applyFileSave(file: string, expectedVersion: number, content: string, revision: string, expectedEpoch = epoch) {
  const current = documents.get(file);
  if (!current || expectedEpoch !== epoch || expectedVersion !== fileBaselineVersion(file)) return false;
  // Local typing keeps the baseline version; accepted disk reads and saves
  // advance it, so a delayed response cannot undo a newer disk baseline.
  return rememberFileDraft(file, { ...current, baseline: content, revision, loaded: true }, expectedEpoch);
}
export function dirtyFileDrafts() { return [...documents.values()].some((value) => value.content !== (value.baseline ?? '')); }
export function invalidateCleanDraft(file: string) {
  const value = documents.get(file);
  if (value && value.content === (value.baseline ?? '')) { documents.delete(file); advanceBaseline(file); notify(file); }
}
export function reconcileFileDraft(current: FileDraft, remote: FileDraft, replace = false) {
  const dirty = current.loaded && current.content !== (current.baseline ?? '');
  return dirty && !replace ? { document: current, conflict: current.revision !== remote.revision } : { document: remote, conflict: false };
}
