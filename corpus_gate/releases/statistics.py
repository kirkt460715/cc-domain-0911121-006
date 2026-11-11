from collections import Counter
from fractions import Fraction
from corpus_gate.errors import integer, fail
from corpus_gate.releases import manifests
from corpus_gate.text.normalize import tokens


def distribution(db, release_id):
    release = manifests.get(db, release_id)
    result = {}
    for member in release['members']:
        split = member['split']
        row = result.setdefault(split, {'documents': 0, 'tokens': 0, 'characters': 0, 'sources': Counter(), 'languages': Counter(), 'clusters': set(), 'lengths': []})
        payload = member['payload']
        row['documents'] += 1
        row['tokens'] += payload['metrics']['tokens']
        row['characters'] += len(payload['text'])
        row['sources'][payload['source_id']] += 1
        row['languages'][payload['metrics']['language']] += 1
        row['clusters'].add(member['cluster_id'])
        row['lengths'].append(payload['metrics']['tokens'])
    output = []
    for split, row in sorted(result.items()):
        lengths = sorted(row.pop('lengths'))
        row['sources'] = dict(sorted(row['sources'].items()))
        row['languages'] = dict(sorted(row['languages'].items()))
        row['clusters'] = len(row['clusters'])
        row['minimum_tokens'] = lengths[0]
        row['median_tokens'] = lengths[(len(lengths) - 1) // 2]
        row['maximum_tokens'] = lengths[-1]
        output.append({'split': split, **row})
    return {'release_id': release_id, 'digest': release['digest'], 'splits': output}


def vocabulary(db, release_id, limit=100):
    integer(limit, 'limit', 1, 10000)
    release = manifests.get(db, release_id)
    frequencies = Counter()
    document_frequency = Counter()
    by_split = {}
    for member in release['members']:
        terms = [token['text'].casefold() for token in tokens(member['payload']['text'])]
        frequencies.update(terms)
        document_frequency.update(set(terms))
        by_split.setdefault(member['split'], set()).update(terms)
    ranked = sorted(frequencies, key=lambda term: (-frequencies[term], term))[:limit]
    return {'release_id': release_id, 'unique_terms': len(frequencies), 'total_tokens': sum(frequencies.values()), 'terms': [{'term': term, 'frequency': frequencies[term], 'documents': document_frequency[term]} for term in ranked], 'split_unique_terms': {key: len(values) for key, values in sorted(by_split.items())}}


def overlap(db, release_id):
    release = manifests.get(db, release_id)
    words = {}
    documents = {}
    for member in release['members']:
        split = member['split']
        words.setdefault(split, set()).update(token['text'].casefold() for token in tokens(member['payload']['text']))
        documents.setdefault(split, set()).add(member['payload']['exact_hash'])
    results = []
    splits = sorted(words)
    for index, left in enumerate(splits):
        for right in splits[index + 1:]:
            union = len(words[left] | words[right])
            intersection = len(words[left] & words[right])
            score = Fraction(intersection, union) if union else Fraction(0)
            results.append({'left': left, 'right': right, 'shared_terms': intersection, 'union_terms': union, 'jaccard': {'numerator': score.numerator, 'denominator': score.denominator}, 'exact_duplicate_texts': len(documents[left] & documents[right])})
    return {'release_id': release_id, 'comparisons': results, 'semantics': 'Vocabulary overlap is expected; exact duplicates are reported separately'}


def length_histogram(db, release_id, boundaries):
    if not isinstance(boundaries, list) or not 1 <= len(boundaries) <= 100:
        fail('histogram_boundaries', 'Histogram requires one to one hundred boundaries')
    for value in boundaries:
        integer(value, 'boundary', 1, 100000000)
    if boundaries != sorted(set(boundaries)):
        fail('histogram_order', 'Histogram boundaries must be strictly increasing')
    release = manifests.get(db, release_id)
    buckets = [0] * (len(boundaries) + 1)
    for member in release['members']:
        length = member['payload']['metrics']['tokens']
        index = 0
        while index < len(boundaries) and length >= boundaries[index]:
            index += 1
        buckets[index] += 1
    rows = []
    for index, count in enumerate(buckets):
        rows.append({'minimum_inclusive': 0 if index == 0 else boundaries[index - 1], 'maximum_exclusive': boundaries[index] if index < len(boundaries) else None, 'documents': count})
    return {'release_id': release_id, 'buckets': rows, 'total': sum(buckets)}
