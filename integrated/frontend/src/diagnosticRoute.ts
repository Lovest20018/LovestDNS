export type DiagnosticTab = 'records' | 'query' | 'compare' | 'device';
export interface DiagnosticRoute {
  tab: DiagnosticTab;
  source: string;
  filterDomain: string;
  exactDomain: string;
  qtype: string;
  response: string;
  decision: string;
  start: number | null;
  end: number | null;
  snapshot: number | null;
  before: number | null;
  automatic: boolean;
  domain: string;
  record: string;
}

function unsigned(value: string | null): number | null {
  return value !== null && /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null;
}

export function parseDiagnosticRoute(hash: string, remembered = '', canRecord = true): DiagnosticRoute {
  const search = hash.split('?')[1] ?? '';
  const params = new URLSearchParams(search || remembered.split('?')[1] || '');
  const requested = params.get('tab');
  // A shortcut to an active query should keep the user's record-filter context.
  // Explicit record-list links remain self-contained and override remembered state.
  if (search && (requested === 'query' || requested === 'compare' || (!requested && params.has('domain')))) {
    const previous = new URLSearchParams(remembered.split('?')[1] ?? '');
    for (const key of ['source', 'filter_domain', 'exact', 'qtype', 'response', 'decision', 'start', 'end', 'snapshot', 'before', 'auto']) {
      if (!params.has(key) && previous.has(key)) params.set(key, previous.get(key)!);
    }
  }
  const tab = requested === 'compare' || requested === 'query' || requested === 'device' ? requested
    : requested === 'records' && canRecord ? 'records'
      : params.has('domain') || !canRecord ? 'query' : 'records';
  const cursor = params.get('before') ?? '';
  const before = /^[1-9]\d*$/.test(cursor) && Number.isSafeInteger(Number(cursor)) ? Number(cursor) : null;
  const qtype = params.get('qtype') ?? '';
  const start = unsigned(params.get('start'));
  const end = unsigned(params.get('end'));
  const window = start !== null && end !== null && end > start && end - start <= 86400 && end <= 253402300800;
  const response = params.get('response') ?? '';
  const decision = params.get('decision') ?? '';
  return {
    tab, source: canRecord ? (params.get('source') ?? '').slice(0, 45) : '',
    filterDomain: canRecord ? (params.get('filter_domain') ?? '').slice(0, 253) : '',
    exactDomain: canRecord ? (params.get('exact') ?? '').slice(0, 1021) : '',
    qtype: canRecord && /^[1-9]\d*$/.test(qtype) && Number(qtype) <= 65535 ? qtype : '',
    response: canRecord && /^(no_response_observed|[A-Z0-9_]{1,20})$/.test(response) ? response : '',
    decision: canRecord && ['block', 'allow', 'pass'].includes(decision) ? decision : '',
    start: canRecord && window ? start : null,
    end: canRecord && window ? end : null,
    snapshot: canRecord && window ? unsigned(params.get('snapshot')) : null,
    before: canRecord ? before : null, automatic: canRecord && params.get('auto') === '1',
    domain: (params.get('domain') ?? 'www.baidu.com').slice(0, 253),
    record: (params.get('record') ?? 'A').toUpperCase(),
  };
}

export function diagnosticHash(route: DiagnosticRoute): string {
  const params = new URLSearchParams({ tab: route.tab });
  const filters = queryRecordParams(route);
  filters.delete('limit');
  filters.delete('domain');
  filters.forEach((value, key) => params.set(key, value));
  if (route.filterDomain) params.set('filter_domain', route.filterDomain);
  if (route.automatic) params.set('auto', '1');
  if (route.domain !== 'www.baidu.com') params.set('domain', route.domain);
  if (route.record !== 'A') params.set('record', route.record);
  return `#/diagnostics?${params}`;
}

export function queryRecordParams(route: DiagnosticRoute): URLSearchParams {
  const params = new URLSearchParams({ limit: '50' });
  if (route.source) params.set('source', route.source);
  if (route.filterDomain) params.set('domain', route.filterDomain);
  if (route.exactDomain) params.set('exact', route.exactDomain);
  if (route.qtype) params.set('qtype', route.qtype);
  if (route.response) params.set('response', route.response);
  if (route.decision) params.set('decision', route.decision);
  if (route.start !== null && route.end !== null) {
    params.set('start', String(route.start));
    params.set('end', String(route.end));
    if (route.snapshot !== null) params.set('snapshot', String(route.snapshot));
  }
  if (route.before !== null) params.set('before', String(route.before));
  return params;
}
