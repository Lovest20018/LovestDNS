export interface DomainEntry {
  id: number;
  prefix: string;
  value: string;
  comment: boolean;
  source?: string;
  signature?: string;
}

export function parseListContent(content: string): DomainEntry[] {
  const entries: DomainEntry[] = [];
  let id = 0;
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) { entries.push({ id: id++, prefix: '__comment__', value: line, comment: true, source: line, signature: JSON.stringify(['__comment__', line, true]) }); continue; }
    if (trimmed.startsWith('#')) {
      const uncommented = trimmed.replace(/^#+\s*/, '');
      const match = uncommented.match(/^(domain|full|regexp|keyword):(.*)$/);
      if (match) {
        entries.push({ id: id++, prefix: match[1], value: match[2], comment: true });
      } else {
        entries.push({ id: id++, prefix: '__comment__', value: trimmed, comment: true });
      }
      continue;
    }
    const match = trimmed.match(/^(domain|full|regexp|keyword):(.*)$/);
    if (match) {
      entries.push({ id: id++, prefix: match[1], value: match[2], comment: false });
    } else {
      entries.push({ id: id++, prefix: 'domain', value: trimmed, comment: false });
    }
  }
  const lines = content.split('\n');
  return entries.map((entry, index) => ({ ...entry, source: lines[index], signature: JSON.stringify([entry.prefix, entry.value, entry.comment]) }));
}

export function serializeListContent(entries: DomainEntry[]): string {
  const lines: string[] = [];
  for (const e of entries) {
    if (e.source !== undefined && e.signature === JSON.stringify([e.prefix, e.value, e.comment])) { lines.push(e.source); continue; }
    if (e.prefix === '__comment__') {
      const text = e.value.trimStart();
      lines.push(text.startsWith('#') ? e.value : `# ${e.value}`);
    } else {
      const line = `${e.prefix}:${e.value}`;
      lines.push(e.comment ? `# ${line}` : line);
    }
  }
  return lines.join('\n');
}
