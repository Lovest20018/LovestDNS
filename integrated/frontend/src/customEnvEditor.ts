export interface CustomEnvRow { key: string; value: string; enabled: boolean; sourceLine: number | null }

function documentLines(content: string) {
  const bom = content.startsWith('\uFEFF') ? '\uFEFF' : '';
  const parts = content.slice(bom.length).split(/(\r\n|\n|\r)/);
  const lines = [] as { text: string; ending: string }[];
  for (let i = 0; i < parts.length; i += 2) lines.push({ text: parts[i], ending: parts[i + 1] ?? '' });
  return { bom, lines };
}

export function parseCustomEnvDocument(content: string) {
  const entries: CustomEnvRow[] = [];
  let unrecognizedLines = 0;
  documentLines(content).lines.forEach(({ text }, sourceLine) => {
    // Keep incomplete rows visible while a user clears or types a field.
    // Validation, rather than parsing, rejects incomplete names and quotes.
    const active = text.match(/^([^\s="\r\n]*)="([^\r\n]*)"$/);
    const disabled = text.match(/^\s*#+\s*([^\s="\r\n]*)="([^\r\n]*)"\s*$/);
    const match = active ?? disabled;
    if (match) entries.push({ key: match[1], value: match[2], enabled: !!active, sourceLine });
    else if (text.trim() && !text.trimStart().startsWith('#')) unrecognizedLines++;
  });
  return { entries, unrecognizedLines };
}

export function serializeCustomEnvDocument(content: string, entries: CustomEnvRow[]) {
  const { bom, lines } = documentLines(content);
  const original = new Map(parseCustomEnvDocument(content).entries.map((entry) => [entry.sourceLine, entry]));
  const rows = new Map(entries.filter((entry) => entry.sourceLine !== null).map((entry) => [entry.sourceLine, entry]));
  const assignment = (entry: CustomEnvRow) => `${entry.enabled ? '' : '# '}${entry.key}="${entry.value}"`;
  let output = bom + lines.map(({ text, ending }, line) => {
    const before = original.get(line);
    if (!before) return text + ending;
    const entry = rows.get(line);
    if (!entry) return '';
    return (entry.key === before.key && entry.value === before.value && entry.enabled === before.enabled ? text : assignment(entry)) + ending;
  }).join('');
  const newline = content.match(/\r\n|\n|\r/)?.[0] ?? '\n';
  for (const entry of entries.filter((item) => item.sourceLine === null)) {
    if (output && output !== bom && !/[\r\n]$/.test(output)) output += newline;
    output += assignment(entry) + newline;
  }
  return output;
}

export function validateCustomEnvRows(entries: CustomEnvRow[]) {
  entries.forEach((entry, index) => {
    if (!/^[_a-zA-Z0-9]+$/.test(entry.key)) throw new Error(`第 ${index + 1} 个变量需要填写名称，只能包含字母、数字和下划线`);
    if (/["\r\n]/.test(entry.value) || entry.value.includes(String.fromCharCode(0))) throw new Error(`变量 ${entry.key} 的值不能包含双引号、换行或空字符`);
  });
}
