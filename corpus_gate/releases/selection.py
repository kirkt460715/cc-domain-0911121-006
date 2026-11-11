import hashlib
from corpus_gate.errors import identifier, integer, shape, fail, digest
from corpus_gate import documents
from corpus_gate.ingest import sources
from corpus_gate.dedup import clusters


def settings(value):
    shape(value, {'seed', 'splits'}, {'max_documents', 'max_tokens', 'source_limits', 'language_limits', 'include_all_duplicates'})
    seed = identifier(value['seed'], 'seed')
    splits = value['splits']
    if not isinstance(splits, dict) or not splits or len(splits) > 10:
        fail('release_splits', 'Splits must be a nonempty bounded mapping')
    for key, weight in splits.items():
        identifier(key, 'split')
        integer(weight, 'split_weight', 1, 10000)
    if sum(splits.values()) != 10000:
        fail('release_splits', 'Split weights must sum to ten thousand')
    result = {'seed': seed, 'splits': dict(sorted(splits.items())), 'max_documents': integer(value.get('max_documents', 10000), 'max_documents', 1, 10000), 'max_tokens': integer(value.get('max_tokens', 100000000), 'max_tokens', 1, 100000000), 'source_limits': value.get('source_limits', {}), 'language_limits': value.get('language_limits', {}), 'include_all_duplicates': value.get('include_all_duplicates', False)}
    if type(result['include_all_duplicates']) is not bool:
        fail('release_duplicates', 'Duplicate inclusion must be boolean')
    for key in ('source_limits', 'language_limits'):
        if not isinstance(result[key], dict) or len(result[key]) > 1000:
            fail('release_limits', 'Quota limits must be bounded mappings')
        for identity, limit in result[key].items():
            identifier(identity)
            integer(limit, 'limit', 0, 10000)
        result[key] = dict(sorted(result[key].items()))
    return result


def partition(cluster_id, options):
    number = int(hashlib.sha256((options['seed'] + '\0' + cluster_id).encode()).hexdigest(), 16) % 10000
    boundary = 0
    for split, weight in options['splits'].items():
        boundary += weight
        if number < boundary:
            return split
    fail('release_split_internal', 'Split weights did not cover hash bucket')


def select(db, run_id, context, definition):
    options = settings(definition)
    context = sources.request(context)
    run = clusters.get(db, run_id)
    if not run['fresh']:
        fail('cluster_stale', 'Recompute clusters after corpus changes')
    chosen = []
    excluded = []
    source_counts = {}
    language_counts = {}
    token_count = 0
    ordered = sorted(clusters.groups(db, run_id), key=lambda group: (digest([options['seed'], group['cluster_id']]), group['cluster_id']))
    for group in ordered:
        eligible = []
        for identity in group['members']:
            document = documents.get(db, identity)
            authorization = sources.authorize(db, document['source_id'], context, exporting=True)
            if document['state'] != 'accepted' or not document['is_head']:
                excluded.append({'document_id': identity, 'reason': 'not_current_accepted'})
            elif not authorization['allowed']:
                excluded.append({'document_id': identity, 'reason': 'source_authorization', 'details': authorization})
            else:
                eligible.append(document)
        if not eligible:
            continue
        eligible.sort(key=lambda document: (sources.get(db, document['source_id'])['priority'], -document['metrics']['tokens'], document['source_id'], document['external_id'], -document['version'], document['document_id']))
        candidates = eligible if options['include_all_duplicates'] else eligible[:1]
        for document in candidates:
            source = document['source_id']
            language = document['metrics']['language']
            reasons = []
            if len(chosen) >= options['max_documents']:
                reasons.append('document_budget')
            if token_count + document['metrics']['tokens'] > options['max_tokens']:
                reasons.append('token_budget')
            if source_counts.get(source, 0) >= options['source_limits'].get(source, 10000):
                reasons.append('source_quota')
            if language_counts.get(language, 0) >= options['language_limits'].get(language, 10000):
                reasons.append('language_quota')
            if reasons:
                excluded.append({'document_id': document['document_id'], 'reason': 'quota', 'details': reasons})
                continue
            chosen.append({'document_id': document['document_id'], 'cluster_id': group['cluster_id'], 'split': partition(group['cluster_id'], options)})
            source_counts[source] = source_counts.get(source, 0) + 1
            language_counts[language] = language_counts.get(language, 0) + 1
            token_count += document['metrics']['tokens']
    return {'options': options, 'context': context, 'chosen': chosen, 'excluded': excluded, 'counts': {'documents': len(chosen), 'tokens': token_count, 'sources': source_counts, 'languages': language_counts}, 'corpus_revision': db.revision(), 'run_digest': run['digest']}


def leakage(members):
    assignments = {}
    for member in members:
        assignments.setdefault(member['cluster_id'], set()).add(member['split'])
    leaking = [{'cluster_id': key, 'splits': sorted(values)} for key, values in sorted(assignments.items()) if len(values) > 1]
    return {'ok': not leaking, 'leaking_clusters': leaking}
