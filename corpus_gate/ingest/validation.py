from corpus_gate.errors import integer, fail, GateError, digest
from corpus_gate.ingest import formats, sources
from corpus_gate.text import profiles
from corpus_gate import documents


def preflight(db, source_id, profile_id, records, context):
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        fail('preflight_records', 'Preflight requires a bounded nonempty list')
    source = sources.get(db, source_id)
    profile = profiles.get(db, profile_id)
    authorization = sources.authorize(db, source_id, context)
    results = []
    seen = {}
    for ordinal, raw in enumerate(records):
        try:
            document = formats.document(raw)
            key = (document['external_id'], document['version'])
            payload_hash = digest(document)
            if key in seen and seen[key] != payload_hash:
                fail('preflight_conflict', 'Same document version has conflicting payloads within input')
            seen[key] = payload_hash
            existing = documents.head(db, source_id, document['external_id'])
            duplicate = False
            if existing and document['version'] < existing['version']:
                fail('document_old_version', 'Input version is older than current head')
            if existing and document['version'] == existing['version']:
                duplicate = existing['raw_text'] == document['text'] and existing['metadata'] == document['metadata'] and existing['profile_hash'] == profile['digest']
                if not duplicate:
                    fail('document_conflict', 'Current document version has different content or profile')
            result = profiles.process(document['text'], profile['definition'])
            results.append({'ordinal': ordinal, 'valid': True, 'external_id': document['external_id'], 'version': document['version'], 'duplicate': duplicate, 'state': result['quality']['state'], 'findings': result['quality']['findings'], 'tokens': result['quality']['metrics']['tokens'], 'chunks': len(result['chunks'])})
        except GateError as exc:
            results.append({'ordinal': ordinal, 'valid': False, 'error': exc.as_dict()})
    return {'source_id': source_id, 'source_revision': source['revision'], 'profile_id': profile_id, 'profile_revision': profile['revision'], 'profile_hash': profile['digest'], 'input_digest': digest(records), 'authorization': authorization, 'results': results, 'valid': sum(row['valid'] for row in results), 'invalid': sum(not row['valid'] for row in results), 'writes_performed': False}


def compare_preflight(before, after):
    if before['input_digest'] != after['input_digest']:
        fail('preflight_inputs', 'Preflight comparison requires identical inputs')
    changes = []
    old = {row['ordinal']: row for row in before['results']}
    new = {row['ordinal']: row for row in after['results']}
    for ordinal in sorted(set(old) | set(new)):
        if old.get(ordinal) != new.get(ordinal):
            changes.append({'ordinal': ordinal, 'before': old.get(ordinal), 'after': new.get(ordinal)})
    return {'changes': changes, 'profile_changed': before['profile_hash'] != after['profile_hash'], 'source_changed': before['source_revision'] != after['source_revision']}


def manifest(db, source_id):
    sources.get(db, source_id)
    heads = documents.listing(db, source_id=source_id, limit=10000)
    entries = [{'external_id': row['external_id'], 'version': row['version'], 'document_id': row['document_id'], 'exact_hash': row['exact_hash'], 'state': row['state'], 'revision': row['revision']} for row in heads]
    entries.sort(key=lambda row: row['external_id'])
    return {'source_id': source_id, 'entries': entries, 'sha256': digest(entries), 'corpus_revision': db.revision()}


def reconcile_manifest(db, source_id, external_entries):
    if not isinstance(external_entries, list) or len(external_entries) > 10000:
        fail('manifest_entries', 'Expected bounded external manifest')
    from corpus_gate.errors import shape, identifier
    expected = {}
    for entry in external_entries:
        shape(entry, {'external_id', 'version', 'exact_hash'})
        identifier(entry['external_id'])
        integer(entry['version'], 'version', 1)
        sha = entry['exact_hash']
        if not isinstance(sha, str) or len(sha) != 64 or any(char not in '0123456789abcdef' for char in sha):
            fail('manifest_hash', 'External manifest hash is invalid')
        if entry['external_id'] in expected:
            fail('manifest_duplicate', 'External manifest identity repeats')
        expected[entry['external_id']] = entry
    actual = {entry['external_id']: entry for entry in manifest(db, source_id)['entries']}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches = []
    for identity in sorted(set(actual) & set(expected)):
        fields = [key for key in ('version', 'exact_hash') if actual[identity][key] != expected[identity][key]]
        if fields:
            mismatches.append({'external_id': identity, 'fields': fields, 'expected': expected[identity], 'actual': actual[identity]})
    return {'ok': not missing and not extra and not mismatches, 'missing': missing, 'extra': extra, 'mismatches': mismatches}


def batch_capacity(records, maximum_bytes=33554432, maximum_records=10000):
    from corpus_gate.errors import canonical
    integer(maximum_bytes, 'maximum_bytes', 1, 100000000)
    integer(maximum_records, 'maximum_records', 1, 10000)
    if not isinstance(records, list):
        fail('capacity_records', 'Capacity assessment requires a list')
    sizes = [len(canonical(record).encode('utf-8')) for record in records]
    total = sum(sizes) + max(0, len(records) - 1) + 2
    return {'fits': len(records) <= maximum_records and total <= maximum_bytes, 'records': len(records), 'canonical_bytes': total, 'largest_record_bytes': max(sizes, default=0), 'maximum_bytes': maximum_bytes, 'maximum_records': maximum_records}
