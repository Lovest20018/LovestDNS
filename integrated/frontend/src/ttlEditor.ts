export interface TtlRuleEntry {
  id: number;
  source?: string;
  signature?: string;
  domain: string;
  server: string;
  type: 'forward' | 'record' | 'cname';
  matchMode?: 'subdomain' | 'exact';
  record?: string;
}

export interface TtlRuleParseResult {
  entries: TtlRuleEntry[];
  visualSafe: boolean;
}

export function parseTtlRules(content: string): TtlRuleParseResult {
  const entries: TtlRuleEntry[] = [];
  let visualSafe = true;
  const ipv4Like = (s: string) => /^\d{1,3}(\.\d{1,3}){1,3}$/.test(s);
  const ipv6Like = (s: string) => /^[0-9a-fA-F:]+$/.test(s) && s.includes(':');
  for (const [lineId, line] of content.split('\n').entries()) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed.startsWith('#')) continue;
    const id = lineId;
    if (!/^[^@]*@{1,3}[^@]*$/.test(trimmed)) { visualSafe = false; continue; }
    if (trimmed.includes('@@@')) {
      const [domain, record] = trimmed.split('@@@');
      const r = record.trim();
      const isIp = ipv4Like(r) || ipv6Like(r);
      const isDomain = r.includes('.') && !isIp;
      if (!isIp && !isDomain) {
        visualSafe = false;
      }
      entries.push({ id, domain: domain.trim(), server: '', type: isDomain ? 'cname' : 'record', matchMode: 'exact', record: r });
    } else if (trimmed.includes('@@')) {
      const [domain, record] = trimmed.split('@@');
      const r = record.trim();
      const isIp = ipv4Like(r) || ipv6Like(r);
      const isDomain = r.includes('.') && !isIp;
      if (!isIp && !isDomain) {
        visualSafe = false;
      }
      entries.push({ id, domain: domain.trim(), server: '', type: isDomain ? 'cname' : 'record', matchMode: 'subdomain', record: r });
    } else if (trimmed.includes('@')) {
      const [domain, serverPart] = trimmed.split('@');
      entries.push({ id, domain: domain.trim(), server: serverPart?.trim() || '', type: 'forward' });
    } else {
      visualSafe = false;
    }
  }
  return { entries: entries.map((entry) => ({ ...entry, source: content.split('\n')[entry.id], signature: JSON.stringify([entry.domain, entry.server, entry.type, entry.matchMode, entry.record]) })), visualSafe };
}

export function serializeTtlRules(entries: TtlRuleEntry[], original: string): string {
  const render = (e: TtlRuleEntry) => {
    if (e.source !== undefined && e.signature === JSON.stringify([e.domain, e.server, e.type, e.matchMode, e.record])) return e.source;
    return e.type === 'forward' ? `${e.domain}@${e.server}` : `${e.domain}${e.matchMode === 'exact' ? '@@@' : '@@'}${e.record || ''}`;
  };
  const old = new Set(parseTtlRules(original).entries.map((e) => e.id));
  const byLine = new Map(entries.filter((e) => e.source !== undefined).map((e) => [e.id, e]));
  const lines = original.split('\n').flatMap((line, i) => old.has(i) ? byLine.has(i) ? [render(byLine.get(i)!)] : [] : [line]);
  const added = entries.filter((e) => e.source === undefined).map(render);
  if (added.length && lines.at(-1) === '') lines.pop();
  return [...lines, ...added].join('\n');
}
