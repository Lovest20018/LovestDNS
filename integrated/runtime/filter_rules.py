"""Parse the supported AdGuard DNS QNAME rules without weakening unsupported rules."""
import hashlib
import json
import re

MAX_LIST_BYTES = 8 * 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024 * 1024
MAX_RULES = 250_000
MAX_FRAGMENTS = 128
FRAGMENT = re.compile(r'\*([a-z0-9-]+(?:\.[a-z0-9-]+)+)\*', re.ASCII | re.IGNORECASE)
MAX_LINE = 512
BLOCK_HOSTS = {'0.0.0.0', '127.0.0.1', '::'}
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.ASCII)
SUFFIX = re.compile(r'(@@)?\|\|([^*|^/$\s]+)\^', re.ASCII)
COSMETIC = re.compile(r'#(?:@?(?:#|\?|%|\$)|[?%$]#)')


def normalized_domain(value):
    if not isinstance(value, str) or not value.isascii():
        raise ValueError('请输入 ASCII 域名或已经编码的 IDN 域名')
    value = value.lower().removesuffix('.')
    if not 1 <= len(value) <= 253 or not all(LABEL.fullmatch(label) for label in value.split('.')):
        raise ValueError('域名格式无效')
    # DNS filter syntax interprets IP literals separately from domain rules.
    if all(label.isdigit() for label in value.split('.')):
        raise ValueError('请输入域名')
    return value


def parse(data, *, accept_fragments=True, source_type='block'):
    if len(data.encode('utf-8')) > MAX_LIST_BYTES:
        raise ValueError('规则文本超过大小上限')
    result, seen, types = [], set(), set()
    fragments = 0
    counts = dict(valid=0, duplicates=0, ignored=0, unsupported=0, invalid=0, protected=0)
    issues = []
    scopes = {}

    def issue(kind, reason, raw, line):
        counts[kind] += 1
        if len(issues) < 100:
            issues.append({'line': line, 'rule': raw[:MAX_LINE], 'kind': kind, 'reason': reason,
                           'truncated': len(raw) > MAX_LINE})

    for number, original in enumerate(data.removeprefix('\ufeff').splitlines(), 1):
        line = original.strip()
        if line.lower().startswith(('!#', '[adblock', '[adguard')):
            if line.lower().startswith(('[adblock', '[adguard')) and line.endswith(']'):
                counts['ignored'] += 1
            else:
                issue('protected', 'preprocessing_directive', line, number)
            continue
        if not line or line.startswith(('!', '#')) and not COSMETIC.match(line):
            counts['ignored'] += 1
            continue
        if len(line) > MAX_LINE:
            issue('protected', 'line_too_long', line, number)
            continue
        head, _, _ = line.partition('#')
        expression = head.rstrip()
        match = SUFFIX.fullmatch(line)
        fragment = FRAGMENT.fullmatch(line) if accept_fragments and source_type == 'block' else None
        if not match and (line.startswith('@@') or re.search(r'\$[^\s]*\bbadfilter\b', expression, re.IGNORECASE)):
            issue('protected', 'exception_or_badfilter', line, number)
            continue
        # Browser selectors can contain a domain and '#'. Recognize them before
        # stripping the inline comments allowed by hosts and bare domain rules.
        if COSMETIC.match(line, len(head)):
            types.add('cosmetic')
            issue('unsupported', 'cosmetic', line, number)
            continue
        tokens = expression.split()
        kind = 'exact'
        try:
            if match:
                kind = 'except' if match[1] else 'suffix'
                names = [normalized_domain(match[2])]
            elif fragment and len(fragment[1]) <= 253:
                kind = 'fragment'
                names = [fragment[1].lower()]
                types.add(kind)
            elif '$' in expression or any(char in expression for char in '/*^|~'):
                category = 'modifier' if '$' in expression else 'pattern'
                if category == 'modifier':
                    modifiers = re.findall(r'(?:\$|,)([a-z][a-z0-9_-]{0,39})(?=[=,]|$)', expression.lower())
                    types.update(f'modifier:{name}' for name in modifiers[:16])
                    if not modifiers:
                        types.add('modifier')
                else:
                    types.add(category)
                issue('unsupported', category, line, number)
                continue
            elif len(tokens) > 1:
                if source_type == 'allow' or tokens[0] not in BLOCK_HOSTS:
                    types.add('hosts_rewrite')
                    issue('unsupported', 'hosts_rewrite', line, number)
                    continue
                names = [normalized_domain(value) for value in tokens[1:]]
            else:
                names = [normalized_domain(tokens[0] if tokens else '')]
        except ValueError:
            issue('protected' if line.startswith('@@') else 'invalid', 'invalid_domain', line, number)
            continue
        for domain in names:
            if source_type == 'allow':
                kind = 'allow_exact' if kind == 'exact' else 'allow_suffix'
            key = kind, domain
            if key in seen:
                counts['duplicates'] += 1
                continue
            seen.add(key)
            if kind == 'fragment':
                fragments += 1
                if fragments > MAX_FRAGMENTS:
                    raise ValueError(f'一份规则最多支持 {MAX_FRAGMENTS} 条字面片段通配规则')
            result.append((kind, domain, line, number))
            scopes[kind] = scopes.get(kind, 0) + 1
            counts['valid'] += 1
            if counts['valid'] > MAX_RULES:
                raise ValueError('单份订阅的有效规则数超过上限')
    return result, {**counts, 'types': sorted(types), 'issues': issues, 'scopes': scopes}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compile_policy(model, get_list, now):
    result = {name: {} for name in ('exact', 'suffix', 'except', 'allow')}
    enabled = [item for item in model['sources'] if item['enabled']]
    fragments = {}
    lists = [('local', '本地规则', model['local_rules'], True, 'block')]
    lists.extend((item['id'], item['name'], get_list(item['content']), 'fragment' in item.get('accepted', ()),
                  item.get('type', 'block')) for item in enabled)
    count = 0
    for source_id, source, text, accept_fragments, source_type in lists:
        terms, _ = parse(text, accept_fragments=accept_fragments, source_type=source_type)
        for kind, domain, raw, line in terms:
            target = fragments if kind == 'fragment' else result.setdefault(kind, {})
            if domain not in target:
                rule_id = digest(encoded((source_id, kind, domain, raw, line)))[:24]
                target[domain] = {'id': rule_id, 'source': source, 'rule': raw}
                if source_id != 'local':
                    target[domain]['source_id'] = source_id
                count += 1
                if count > MAX_RULES:
                    raise ValueError('已启用的总规则数超过上限')
    if len(fragments) > MAX_FRAGMENTS:
        raise ValueError(f'已启用的字面片段通配规则总数超过 {MAX_FRAGMENTS}')
    if fragments:
        result['fragments'] = [dict(text=text, **fragments[text]) for text in sorted(fragments, key=lambda s: (-len(s), s))]
    version = 3 if 'allow_exact' in result or 'allow_suffix' in result else 2 if fragments else 1
    result.update(version=version, enabled=model['enabled'])
    return encode_policy(result, model, now), count


def encode_policy(compiled, model, now):
    compiled['allow'] = {
        domain: {'id': digest(encoded(('allow', domain)))[:24], 'source': '本地精确放行', 'rule': domain, 'until': until or 0}
        for domain, until in model['allow'].items() if until is None or until > now
    }
    compiled['model_sha256'] = digest(encoded(model))
    payload = encoded(compiled)
    if len(payload) > MAX_POLICY_BYTES:
        raise ValueError('编译后的过滤规则超过大小上限')
    return payload
