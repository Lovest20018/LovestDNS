export interface DiffLine { kind: 'same' | 'add' | 'remove'; text: string }
export function configDiff(current: string, revision: string): DiffLine[] | null {
  const split = (value: string) => value.replace(/\r\n/g, '\n').split('\n');
  const before = split(current), after = split(revision);
  // Bounded LCS avoids blocking the browser for large list files. Original
  // texts are always available in the adjacent raw preview.
  if (before.length > 600 || after.length > 600 || current.length + revision.length > 128 * 1024) return null;
  const width = after.length + 1;
  const matrix = new Uint16Array((before.length + 1) * width);
  for (let i = before.length - 1; i >= 0; i--) for (let j = after.length - 1; j >= 0; j--) {
    matrix[i * width + j] = before[i] === after[j] ? matrix[(i + 1) * width + j + 1] + 1
      : Math.max(matrix[(i + 1) * width + j], matrix[i * width + j + 1]);
  }
  const output: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < before.length || j < after.length) {
    if (i < before.length && j < after.length && before[i] === after[j]) { output.push({ kind: 'same', text: before[i++] }); j++; }
    else if (j < after.length && (i === before.length || matrix[i * width + j + 1] > matrix[(i + 1) * width + j])) output.push({ kind: 'add', text: after[j++] });
    else output.push({ kind: 'remove', text: before[i++] });
  }
  return output;
}
