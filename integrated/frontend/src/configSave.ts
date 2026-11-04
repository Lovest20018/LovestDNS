// Compare the editor baseline, then send the same read version to the locked server write.
export async function preflightSave<T>(file: string, baseline: string | null, content: string,
  read: (file: string) => Promise<{ content: string; revision?: string }>, write: (file: string, content: string, expectedRevision?: string) => Promise<T>): Promise<T> {
  let current: string | null;
  let revision: string | undefined;
  try { const value = await read(file); current = value.content; revision = value.revision; }
  catch (e) {
    if (typeof e === 'object' && e !== null && 'status' in e && e.status === 404) { current = null; revision = 'missing'; }
    else throw e;
  }
  if (current !== baseline) throw new Error(`${file} 已被其他页面或工具修改，已阻止覆盖，当前草稿仍保留。请先复制或下载草稿，再重新读取并合并。`);
  return write(file, content, revision);
}
