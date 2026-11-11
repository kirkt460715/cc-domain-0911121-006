import collections
import re
import unicodedata
from corpus_gate.errors import integer, shape, fail
from corpus_gate.text.normalize import tokens, language_hint

DEFAULT = {'minimum_tokens': 5, 'maximum_tokens': 100000, 'minimum_unique_percent': 20, 'maximum_symbol_percent': 60, 'maximum_repeated_line_percent': 60, 'detect_email': True, 'detect_phone': True, 'blocked_terms': [], 'allowed_languages': []}
EMAIL = re.compile(r'(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)')
PHONE = re.compile(r'(?<!\d)(?:\+?\d[\d ()-]{7,18}\d)(?!\d)')


def settings(value):
    value = {} if value is None else value
    shape(value, set(), set(DEFAULT))
    result = {**DEFAULT, **value}
    integer(result['minimum_tokens'], 'minimum_tokens', 0, 100000)
    integer(result['maximum_tokens'], 'maximum_tokens', 1, 2000000)
    if result['minimum_tokens'] > result['maximum_tokens']:
        fail('quality_token_range', 'Minimum tokens exceeds maximum')
    for key in ('minimum_unique_percent', 'maximum_symbol_percent', 'maximum_repeated_line_percent'):
        integer(result[key], key, 0, 100)
    for key in ('detect_email', 'detect_phone'):
        if type(result[key]) is not bool:
            fail('quality_boolean', 'Detector flags must be booleans')
    terms = result['blocked_terms']
    if not isinstance(terms, list) or len(terms) > 100 or any(not isinstance(term, str) or not term or len(term) > 100 for term in terms):
        fail('quality_terms', 'Blocked terms must be a bounded text list')
    result['blocked_terms'] = sorted(set(term.casefold() for term in terms))
    languages = result['allowed_languages']
    if not isinstance(languages, list) or set(languages) - {'zh', 'latin', 'mixed', 'und'}:
        fail('quality_languages', 'Unknown language hint')
    result['allowed_languages'] = sorted(set(languages))
    return result


def metrics(value):
    terms = tokens(value)
    words = [term['text'].casefold() for term in terms]
    counts = collections.Counter(words)
    nonspace = [char for char in value if not char.isspace()]
    symbols = sum(unicodedata.category(char)[0] in {'P', 'S'} for char in nonspace)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    repeated = len(lines) - len(set(lines))
    return {'characters': len(value), 'tokens': len(words), 'unique_tokens': len(counts), 'nonspace_characters': len(nonspace), 'symbol_characters': symbols, 'lines': len(lines), 'repeated_lines': repeated, 'longest_token': max((len(word) for word in words), default=0), 'language': language_hint(value), 'most_frequent_count': max(counts.values(), default=0)}


def inspect(value, options=None):
    options = settings(options)
    measured = metrics(value)
    findings = []
    def add(code, severity, evidence, overrideable=True):
        findings.append({'code': code, 'severity': severity, 'evidence': evidence, 'overrideable': overrideable})
    if not value:
        add('empty_text', 'reject', {}, False)
    if measured['tokens'] < options['minimum_tokens']:
        add('too_short', 'quarantine', {'actual': measured['tokens'], 'minimum': options['minimum_tokens']})
    if measured['tokens'] > options['maximum_tokens']:
        add('too_long', 'reject', {'actual': measured['tokens'], 'maximum': options['maximum_tokens']}, False)
    if measured['tokens'] and measured['unique_tokens'] * 100 < options['minimum_unique_percent'] * measured['tokens']:
        add('low_diversity', 'quarantine', {'unique': measured['unique_tokens'], 'total': measured['tokens']})
    if measured['nonspace_characters'] and measured['symbol_characters'] * 100 > options['maximum_symbol_percent'] * measured['nonspace_characters']:
        add('symbol_heavy', 'quarantine', {'symbols': measured['symbol_characters'], 'total': measured['nonspace_characters']})
    if measured['lines'] and measured['repeated_lines'] * 100 > options['maximum_repeated_line_percent'] * measured['lines']:
        add('repeated_lines', 'quarantine', {'repeated': measured['repeated_lines'], 'total': measured['lines']})
    if options['allowed_languages'] and measured['language'] not in options['allowed_languages']:
        add('language', 'quarantine', {'language': measured['language']})
    folded = value.casefold()
    for term in options['blocked_terms']:
        if term in folded:
            add('blocked_term', 'reject', {'term': term}, False)
    for enabled, pattern, code in [(options['detect_email'], EMAIL, 'email_like'), (options['detect_phone'], PHONE, 'phone_like')]:
        if enabled:
            spans = [{'start': match.start(), 'end': match.end()} for match in pattern.finditer(value)]
            if spans:
                add(code, 'quarantine', {'spans': spans})
    state = 'rejected' if any(row['severity'] == 'reject' for row in findings) else 'quarantined' if findings else 'accepted'
    return {'state': state, 'metrics': measured, 'findings': findings}


def compare(before, after):
    left = {finding['code'] for finding in before['findings']}
    right = {finding['code'] for finding in after['findings']}
    return {'before_state': before['state'], 'after_state': after['state'], 'resolved': sorted(left - right), 'introduced': sorted(right - left), 'retained': sorted(left & right), 'token_change': after['metrics']['tokens'] - before['metrics']['tokens']}
