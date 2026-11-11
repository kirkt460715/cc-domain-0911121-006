from corpus_gate.errors import integer, shape, fail, digest
from corpus_gate.text.normalize import tokens

DEFAULT = {'max_tokens': 256, 'overlap_tokens': 16, 'minimum_tokens': 1}


def settings(value):
    value = {} if value is None else value
    shape(value, set(), set(DEFAULT))
    result = {**DEFAULT, **value}
    integer(result['max_tokens'], 'max_tokens', 1, 10000)
    integer(result['overlap_tokens'], 'overlap_tokens', 0, 9999)
    integer(result['minimum_tokens'], 'minimum_tokens', 1, 10000)
    if result['overlap_tokens'] >= result['max_tokens']:
        fail('chunk_overlap', 'Overlap must be smaller than chunk size')
    if result['minimum_tokens'] > result['max_tokens']:
        fail('chunk_minimum', 'Minimum tokens exceeds chunk size')
    return result


def chunks(value, options=None):
    options = settings(options)
    terms = tokens(value)
    output = []
    step = options['max_tokens'] - options['overlap_tokens']
    for begin in range(0, len(terms), step):
        end = min(begin + options['max_tokens'], len(terms))
        if end - begin < options['minimum_tokens']:
            break
        start_offset = terms[begin]['start']
        end_offset = terms[end - 1]['end']
        body = value[start_offset:end_offset]
        output.append({'ordinal': len(output), 'start_offset': start_offset, 'end_offset': end_offset, 'text': body, 'digest': digest(body), 'tokens': end - begin})
        if end == len(terms):
            break
    return output


def verify(value, records):
    problems = []
    previous_start = -1
    for index, record in enumerate(records):
        if record['ordinal'] != index:
            problems.append({'code': 'chunk_ordinal', 'ordinal': index})
        start = record['start_offset']
        end = record['end_offset']
        if not 0 <= start < end <= len(value):
            problems.append({'code': 'chunk_offset', 'ordinal': index})
            continue
        if start <= previous_start:
            problems.append({'code': 'chunk_order', 'ordinal': index})
        previous_start = start
        if value[start:end] != record['text']:
            problems.append({'code': 'chunk_content', 'ordinal': index})
        if digest(record['text']) != record['digest']:
            problems.append({'code': 'chunk_digest', 'ordinal': index})
        if len(tokens(record['text'])) != record['tokens']:
            problems.append({'code': 'chunk_tokens', 'ordinal': index})
    return {'ok': not problems, 'problems': problems, 'chunks': len(records)}
