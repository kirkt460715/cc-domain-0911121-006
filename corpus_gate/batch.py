import json
from corpus_gate.errors import identifier, integer, canonical, digest, fail, GateError
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import documents


def stage(db, batch_id, source_id, profile_id, records):
    identifier(batch_id)
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        fail('batch_records', 'Batch must contain one to ten thousand records')
    payload_hash = digest(records)
    with db.transaction():
        source = sources.get(db, source_id)
        if source['state'] != 'active':
            fail('source_revoked', 'Cannot stage data from revoked source')
        profile = profiles.get(db, profile_id)
        existing = db.one('SELECT * FROM batches WHERE batch_id=?', (batch_id,))
        if existing:
            if existing['source_id'] != source_id or existing['profile_id'] != profile_id or existing['digest'] != payload_hash:
                fail('batch_conflict', 'Batch identifier is already bound to other inputs')
            return get(db, batch_id)
        db.execute("INSERT INTO batches(batch_id,source_id,profile_id,profile_revision,profile_hash,source_revision,state,revision,total,digest,created_at) VALUES(?,?,?,?,?,?,'staged',1,?,?,?)", (batch_id, source_id, profile_id, profile['revision'], profile['digest'], source['revision'], len(records), payload_hash, db.clock()))
        for ordinal, record in enumerate(records):
            db.execute("INSERT INTO intake(batch_id,ordinal,payload,digest,state) VALUES(?,?,?,?,'pending')", (batch_id, ordinal, canonical(record), digest(record)))
        db.log('batch.staged', {'batch_id': batch_id, 'records': len(records), 'sha256': payload_hash})
        return get(db, batch_id)


def get(db, batch_id):
    row = db.one('SELECT * FROM batches WHERE batch_id=?', (batch_id,))
    if row is None:
        fail('batch_missing', 'Batch does not exist')
    counts = db.all('SELECT state,COUNT(*) AS count FROM intake WHERE batch_id=? GROUP BY state', (batch_id,))
    row['counts'] = {item['state']: item['count'] for item in counts}
    return row


def process(db, batch_id, expected_revision, limit=100):
    integer(limit, 'limit', 1, 10000)
    with db.transaction():
        current = get(db, batch_id)
        if current['revision'] != expected_revision:
            fail('batch_stale', 'Batch checkpoint changed')
        if current['state'] in {'complete', 'cancelled'}:
            fail('batch_terminal', 'Batch cannot be processed in terminal state')
        source = sources.get(db, current['source_id'])
        if source['state'] != 'active' or source['revision'] != current['source_revision']:
            fail('batch_source_changed', 'Source authorization changed after staging')
        profile = profiles.get(db, current['profile_id'], current['profile_revision'])
        if profile['digest'] != current['profile_hash']:
            fail('batch_profile_changed', 'Pinned profile content changed')
        rows = db.all('SELECT * FROM intake WHERE batch_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?', (batch_id, current['cursor'], limit))
        for row in rows:
            value = json.loads(row['payload'])
            if digest(value) != row['digest']:
                fail('intake_corrupt', 'Staged input checksum mismatch', ordinal=row['ordinal'])
            try:
                with db.transaction():
                    result = documents.ingest(db, batch_id, current['source_id'], value, profile)
                state = 'duplicate' if result['duplicate'] else 'processed'
                db.execute('UPDATE intake SET state=?,document_id=? WHERE batch_id=? AND ordinal=?', (state, result['document']['document_id'], batch_id, row['ordinal']))
            except GateError as exc:
                db.execute("UPDATE intake SET state='failed',error=? WHERE batch_id=? AND ordinal=?", (canonical(exc.as_dict()), batch_id, row['ordinal']))
        cursor = current['cursor'] + len(rows)
        state = 'complete' if cursor == current['total'] else 'processing'
        db.execute('UPDATE batches SET cursor=?,state=?,revision=revision+1 WHERE batch_id=?', (cursor, state, batch_id))
        db.log('batch.checkpoint', {'batch_id': batch_id, 'cursor': cursor, 'state': state})
        return get(db, batch_id)


def run(db, batch_id, limit=100):
    current = get(db, batch_id)
    if current['state'] == 'cancelled':
        fail('batch_terminal', 'Cancelled batch cannot run')
    while current['state'] != 'complete':
        current = process(db, batch_id, current['revision'], limit)
    return current


def cancel(db, batch_id, expected_revision):
    with db.transaction():
        current = get(db, batch_id)
        if current['revision'] != expected_revision:
            fail('batch_stale', 'Batch checkpoint changed')
        if current['state'] in {'complete', 'cancelled'}:
            fail('batch_terminal', 'Terminal batch cannot cancel')
        db.execute("UPDATE batches SET state='cancelled',revision=revision+1 WHERE batch_id=?", (batch_id,))
        db.log('batch.cancelled', {'batch_id': batch_id, 'cursor': current['cursor']})
        return get(db, batch_id)


def records(db, batch_id, state=None):
    get(db, batch_id)
    query = 'SELECT * FROM intake WHERE batch_id=?'
    args = [batch_id]
    if state is not None:
        query += ' AND state=?'
        args.append(state)
    rows = db.all(query + ' ORDER BY ordinal', args)
    for row in rows:
        row['payload'] = json.loads(row['payload'])
        row['error'] = json.loads(row['error']) if row['error'] else None
    return rows


def rebase_source(db, batch_id, expected_revision):
    with db.transaction():
        current = get(db, batch_id)
        if current['revision'] != expected_revision:
            fail('batch_stale', 'Batch checkpoint changed')
        if current['state'] not in {'staged', 'processing'}:
            fail('batch_terminal', 'Only unfinished batches may refresh authorization')
        source = sources.get(db, current['source_id'])
        if source['state'] != 'active':
            fail('source_revoked', 'Source is revoked')
        db.execute('UPDATE batches SET source_revision=?,revision=revision+1 WHERE batch_id=?', (source['revision'], batch_id))
        db.log('batch.authorization_refreshed', {'batch_id': batch_id, 'source_revision': source['revision']})
        return get(db, batch_id)
