export type DiagnosticTab = 'records' | 'query' | 'compare';
export interface DiagnosticRoute {
  tab: DiagnosticTab;
  source: string;
  filterDomain: string;
  qtype: string;
  before: number | null;
  automatic: boolean;
  domain: string;
  record: string;
}

export function parseDiagnosticRoute(hash: string, remembered = '', canRecord = true): DiagnosticRoute {
  const search = hash.split('?')[1] ?? '';
  const params = new URLSearchParams(search || remembered.split('?')[1] || '');
  const requested = params.get('tab');
  // A shortcut to an active query should keep the user's record-filter context.
  // Explicit record-list links remain self-contained and override remembered state.
  if (search && (requested === 'query' || requested === 'compare' || (!requested && params.has('domain')))) {
    const previous = new URLSearchParams(remembered.split('?')[1] ?? '');
    for (const key of ['source', 'filter_domain', 'qtype', 'before', 'auto']) {
      if (!params.has(key) && previous.has(key)) params.set(key, previous.get(key)!);
    }
  }
  const tab = requested === 'compare' || requested === 'query' ? requested
    : requested === 'records' && canRecord ? 'records'
      : params.has('domain') || !canRecord ? 'query' : 'records';
  const cursor = params.get('before') ?? '';
  const before = /^[1-9]\d*$/.test(cursor) && Number.isSafeInteger(Number(cursor)) ? Number(cursor) : null;
  const qtype = params.get('qtype') ?? '';
  return {
    tab, source: (params.get('source') ?? '').slice(0, 45),
    filterDomain: (params.get('filter_domain') ?? '').slice(0, 253),
    qtype: /^[1-9]\d*$/.test(qtype) && Number(qtype) <= 65535 ? qtype : '',
    before, automatic: params.get('auto') === '1',
    domain: (params.get('domain') ?? 'www.baidu.com').slice(0, 253),
    record: (params.get('record') ?? 'A').toUpperCase(),
  };
}

export function diagnosticHash(route: DiagnosticRoute): string {
  const params = new URLSearchParams({ tab: route.tab });
  if (route.source) params.set('source', route.source);
  if (route.filterDomain) params.set('filter_domain', route.filterDomain);
  if (route.qtype) params.set('qtype', route.qtype);
  if (route.before !== null) params.set('before', String(route.before));
  if (route.automatic) params.set('auto', '1');
  if (route.domain !== 'www.baidu.com') params.set('domain', route.domain);
  if (route.record !== 'A') params.set('record', route.record);
  return `#/diagnostics?${params}`;
}

export function queryRecordParams(route: DiagnosticRoute): URLSearchParams {
  const params = new URLSearchParams({ limit: '50' });
  if (route.source) params.set('source', route.source);
  if (route.filterDomain) params.set('domain', route.filterDomain);
  if (route.qtype) params.set('qtype', route.qtype);
  if (route.before !== null) params.set('before', String(route.before));
  return params;
}
