from corpus_gate.errors import canonical, digest, shape, identifier, fail
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import batch, documents


def export_configuration(db):
    source_rows = []
    for row in sources.listing(db):
        if row['state'] == 'active':
            source_rows.append({'source_id': row['source_id'], 'authorization': row['policy'], 'priority': row['priority']})
    profile_rows = [{'profile_id': row['profile_id'], 'definition': row['definition']} for row in profiles.listing(db)]
    payload = {'format': 'corpus-configuration', 'version': 1, 'sources': source_rows, 'profiles': profile_rows}
    return {'payload': payload, 'sha256': digest(payload), 'scope': 'active_configuration_as_new_heads'}


def import_configuration(db, envelope):
    shape(envelope, {'payload', 'sha256', 'scope'})
    if digest(envelope['payload']) != envelope['sha256']:
        fail('configuration_digest', 'Configuration checksum mismatch')
    payload = envelope['payload']
    shape(payload, {'format', 'version', 'sources', 'profiles'})
    if payload['format'] != 'corpus-configuration' or payload['version'] != 1:
        fail('configuration_format', 'Unsupported configuration version')
    if not isinstance(payload['sources'], list) or not isinstance(payload['profiles'], list):
        fail('configuration_collections', 'Configuration lists are invalid')
    if len(payload['sources']) + len(payload['profiles']) > 1000:
        fail('configuration_size', 'Configuration exceeds item limit')
    result = {'sources': [], 'profiles': []}
    with db.transaction():
        for row in payload['sources']:
            shape(row, {'source_id', 'authorization', 'priority'})
            result['sources'].append(sources.create(db, **row)['source_id'])
        for row in payload['profiles']:
            shape(row, {'profile_id', 'definition'})
            result['profiles'].append(profiles.put(db, expected_revision=0, **row)['profile_id'])
        db.log('configuration.imported', {'sha256': envelope['sha256'], 'sources': len(result['sources']), 'profiles': len(result['profiles'])})
    return result


def export_batch(db, batch_id):
    current = batch.get(db, batch_id)
    records = batch.records(db, batch_id)
    payload = {'format': 'corpus-intake', 'version': 1, 'original_batch_id': batch_id, 'source_id': current['source_id'], 'profile_id': current['profile_id'], 'profile_hash': current['profile_hash'], 'records': [row['payload'] for row in records]}
    if digest(payload['records']) != current['digest']:
        fail('batch_digest', 'Staged records no longer match batch digest')
    return {'payload': payload, 'sha256': digest(payload)}


def import_batch(db, batch_id, envelope):
    identifier(batch_id)
    shape(envelope, {'payload', 'sha256'})
    if digest(envelope['payload']) != envelope['sha256']:
        fail('intake_digest', 'Intake bundle digest mismatch')
    payload = envelope['payload']
    shape(payload, {'format', 'version', 'original_batch_id', 'source_id', 'profile_id', 'profile_hash', 'records'})
    if payload['format'] != 'corpus-intake' or payload['version'] != 1:
        fail('intake_format', 'Unsupported intake bundle format')
    profile = profiles.get(db, payload['profile_id'])
    if profile['digest'] != payload['profile_hash']:
        fail('intake_profile', 'Destination profile differs from source bundle')
    return batch.stage(db, batch_id, payload['source_id'], payload['profile_id'], payload['records'])


def reprocess_heads(db, source_id, profile_id, batch_id, expected_corpus_revision):
    with db.transaction():
        if db.revision() != expected_corpus_revision:
            fail('reprocess_stale', 'Corpus changed since reprocessing was planned')
        sources.get(db, source_id)
        profile = profiles.get(db, profile_id)
        rows = documents.listing(db, source_id=source_id, limit=10000)
        selected = [row for row in rows if row['state'] != 'withdrawn' and row['profile_hash'] != profile['digest']]
        if not selected:
            fail('reprocess_empty', 'No current documents require this profile version')
        records = [{'external_id': row['external_id'], 'version': row['version'] + 1, 'text': row['raw_text'], 'metadata': {**row['metadata'], 'reprocessed_from': row['document_id']}} for row in selected]
        result = batch.stage(db, batch_id, source_id, profile_id, records)
        db.log('profile.reprocessing_staged', {'batch_id': batch_id, 'documents': len(records), 'profile_hash': profile['digest']})
        return result
