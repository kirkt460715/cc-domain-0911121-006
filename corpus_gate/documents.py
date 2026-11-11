import json
from corpus_gate.errors import identifier, integer, canonical, digest, fail, text
from corpus_gate.text import profiles, chunking
from corpus_gate.ingest import sources, formats


def get(db, document_id):
    row = db.one('SELECT * FROM documents WHERE document_id=?', (document_id,))
    if row is None:
        fail('document_missing', 'Document does not exist')
    for key in ('metadata', 'metrics', 'findings'):
        row[key] = json.loads(row[key])
    row['is_head'] = bool(db.one('SELECT document_id FROM document_heads WHERE document_id=?', (document_id,)))
    return row


def head(db, source_id, external_id):
    row = db.one('SELECT document_id FROM document_heads WHERE source_id=? AND external_id=?', (source_id, external_id))
    return get(db, row['document_id']) if row else None


def ingest(db, batch_id, source_id, value, profile):
    value = formats.document(value)
    source = sources.get(db, source_id)
    if source['state'] != 'active':
        fail('source_revoked', 'Cannot ingest from revoked source')
    identity = 'doc-' + digest([source_id, value['external_id'], value['version']])
    existing = db.one('SELECT document_id FROM documents WHERE document_id=?', (identity,))
    if existing:
        previous = get(db, identity)
        if previous['raw_text'] != value['text'] or previous['metadata'] != value['metadata'] or previous['profile_hash'] != profile['digest']:
            fail('document_conflict', 'Document version is immutable; content or profile differs')
        return {'document': previous, 'duplicate': True}
    prior = head(db, source_id, value['external_id'])
    if prior and value['version'] <= prior['version']:
        fail('document_old_version', 'New document version must exceed current head')
    processed = profiles.process(value['text'], profile['definition'])
    normalized = processed['normalization']['text']
    checked = processed['quality']
    db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, source_id, value['external_id'], value['version'], value['text'], normalized, digest(normalized), canonical(value['metadata']), canonical(checked['metrics']), canonical(checked['findings']), checked['state'], 1, profile['digest'], batch_id, db.clock(),))
    for piece in processed['chunks']:
        db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?)', (identity, piece['ordinal'], piece['start_offset'], piece['end_offset'], piece['text'], piece['digest'], piece['tokens']))
    db.execute('INSERT INTO document_heads VALUES(?,?,?) ON CONFLICT(source_id,external_id) DO UPDATE SET document_id=excluded.document_id', (source_id, value['external_id'], identity))
    db.changed()
    db.log('document.ingested', {'document_id': identity, 'state': checked['state'], 'supersedes': prior['document_id'] if prior else None})
    return {'document': get(db, identity), 'duplicate': False}


def pieces(db, document_id):
    get(db, document_id)
    return db.all('SELECT ordinal,start_offset,end_offset,text,digest,tokens FROM chunks WHERE document_id=? ORDER BY ordinal', (document_id,))


def verify(db, document_id):
    row = get(db, document_id)
    checked = chunking.verify(row['normalized'], pieces(db, document_id))
    errors = list(checked['problems'])
    if digest(row['normalized']) != row['exact_hash']:
        errors.append({'code': 'document_hash'})
    if row['document_id'] != 'doc-' + digest([row['source_id'], row['external_id'], row['version']]):
        errors.append({'code': 'document_identity'})
    return {'ok': not errors, 'document_id': document_id, 'problems': errors}


def listing(db, state=None, source_id=None, heads_only=True, after='', limit=100):
    integer(limit, 'limit', 1, 10000)
    conditions = ['d.document_id>?']
    args = [after]
    if state is not None:
        if state not in {'accepted', 'quarantined', 'rejected', 'withdrawn'}:
            fail('document_state', 'Unknown document state')
        conditions.append('d.state=?')
        args.append(state)
    if source_id is not None:
        conditions.append('d.source_id=?')
        args.append(identifier(source_id))
    query = 'SELECT d.document_id FROM documents d'
    if heads_only:
        query += ' JOIN document_heads h ON h.document_id=d.document_id'
    query += ' WHERE ' + ' AND '.join(conditions) + ' ORDER BY d.document_id LIMIT ?'
    args.append(limit)
    return [get(db, row['document_id']) for row in db.all(query, args)]


def versions(db, source_id, external_id):
    return [get(db, row['document_id']) for row in db.all('SELECT document_id FROM documents WHERE source_id=? AND external_id=? ORDER BY version', (source_id, external_id))]


def withdraw(db, document_id, expected_revision, reason):
    reason = text(reason, 'reason')
    with db.transaction():
        row = get(db, document_id)
        if row['revision'] != expected_revision:
            fail('document_stale', 'Document review revision changed')
        if row['state'] == 'withdrawn':
            return row
        db.execute("UPDATE documents SET state='withdrawn',revision=revision+1 WHERE document_id=?", (document_id,))
        db.changed()
        db.log('document.withdrawn', {'document_id': document_id, 'reason': reason})
        return get(db, document_id)
