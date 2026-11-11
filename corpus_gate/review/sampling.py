from corpus_gate.errors import integer, identifier, digest, fail
from corpus_gate import documents


def stratified_queue(db, seed, total=20, by='source', include_accepted=False):
    identifier(seed)
    integer(total, 'total', 1, 1000)
    if by not in {'source', 'language', 'finding'}:
        fail('sampling_dimension', 'Unknown review sampling dimension')
    if type(include_accepted) is not bool:
        fail('sampling_flag', 'include_accepted must be boolean')
    rows = documents.listing(db, limit=10000)
    groups = {}
    for row in rows:
        if row['state'] != 'quarantined' and not (include_accepted and row['state'] == 'accepted'):
            continue
        if by == 'source':
            key = row['source_id']
        elif by == 'language':
            key = row['metrics']['language']
        else:
            key = '+'.join(sorted({finding['code'] for finding in row['findings']})) or 'clean'
        groups.setdefault(key, []).append(row)
    for key in groups:
        groups[key].sort(key=lambda row: (digest([seed, key, row['document_id']]), row['document_id']))
    chosen = []
    round_index = 0
    while len(chosen) < total:
        added = False
        for key in sorted(groups):
            if round_index < len(groups[key]):
                row = groups[key][round_index]
                chosen.append({'document_id': row['document_id'], 'revision': row['revision'], 'stratum': key, 'state': row['state']})
                added = True
                if len(chosen) == total:
                    break
        if not added:
            break
        round_index += 1
    return {'seed': seed, 'dimension': by, 'sample': chosen, 'available': {key: len(values) for key, values in sorted(groups.items())}, 'corpus_revision': db.revision()}


def validate_sample(db, sample):
    if not isinstance(sample, dict) or 'sample' not in sample or 'corpus_revision' not in sample:
        fail('sample_format', 'Invalid review sample')
    changes = []
    for selected in sample['sample']:
        document = documents.get(db, selected['document_id'])
        if document['revision'] != selected['revision'] or document['state'] != selected['state'] or not document['is_head']:
            changes.append({'document_id': selected['document_id'], 'old_revision': selected['revision'], 'current_revision': document['revision'], 'is_head': document['is_head']})
    return {'fresh': not changes and sample['corpus_revision'] == db.revision(), 'changes': changes, 'current_corpus_revision': db.revision()}
