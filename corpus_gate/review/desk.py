import datetime
import json
from corpus_gate.errors import identifier, integer, text, fail, canonical
from corpus_gate import documents
from corpus_gate.ingest import sources


def register(db, actor_id, role):
    identifier(actor_id)
    if role not in {'reviewer', 'supervisor'}:
        fail('actor_role', 'Unknown review role')
    with db.transaction():
        existing = db.one('SELECT * FROM actors WHERE actor_id=?', (actor_id,))
        if existing:
            if existing['role'] != role:
                fail('actor_conflict', 'Actor role is already registered differently')
            return existing
        db.execute("INSERT INTO actors VALUES(?,?,'active')", (actor_id, role))
        db.log('actor.registered', {'actor_id': actor_id, 'role': role})
        return actor(db, actor_id)


def actor(db, actor_id):
    row = db.one('SELECT * FROM actors WHERE actor_id=?', (actor_id,))
    if row is None or row['state'] != 'active':
        fail('actor_unavailable', 'Review actor is not active')
    return row


def disable(db, actor_id):
    with db.transaction():
        actor(db, actor_id)
        db.execute("UPDATE actors SET state='disabled' WHERE actor_id=?", (actor_id,))
        db.log('actor.disabled', {'actor_id': actor_id})
        return {'actor_id': actor_id, 'state': 'disabled'}


def expires(db, ttl):
    integer(ttl, 'ttl', 1, 3600)
    start = datetime.datetime.fromisoformat(db.clock().replace('Z', '+00:00'))
    return (start + datetime.timedelta(seconds=ttl)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def claim(db, document_id, reviewer, ttl=300):
    actor(db, reviewer)
    with db.transaction():
        document = documents.get(db, document_id)
        if document['state'] not in {'quarantined', 'rejected'} or not document['is_head']:
            fail('review_state', 'Only current quarantined or rejected documents can be reviewed')
        previous = db.one('SELECT * FROM review_claims WHERE document_id=?', (document_id,))
        if previous and previous['expires_at'] > db.clock():
            fail('review_busy', 'Document already has an active review claim')
        generation = previous['generation'] + 1 if previous else 1
        expiration = expires(db, ttl)
        db.execute('INSERT INTO review_claims VALUES(?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET owner=excluded.owner,generation=excluded.generation,expires_at=excluded.expires_at', (document_id, reviewer, generation, expiration))
        db.log('review.claimed', {'document_id': document_id, 'reviewer': reviewer, 'generation': generation})
        return {'document_id': document_id, 'owner': reviewer, 'generation': generation, 'expires_at': expiration, 'document_revision': document['revision']}


def assert_claim(db, document_id, token):
    if not isinstance(token, dict):
        fail('review_token', 'Review token must be an object')
    current = db.one('SELECT * FROM review_claims WHERE document_id=?', (document_id,))
    if current is None or token.get('document_id') != document_id or token.get('owner') != current['owner'] or token.get('generation') != current['generation']:
        fail('review_fenced', 'Review claim has been superseded')
    if current['expires_at'] <= db.clock():
        fail('review_expired', 'Review claim expired')
    return actor(db, current['owner'])


def decide(db, document_id, token, expected_revision, decision, reason, acknowledged_findings):
    reason = text(reason, 'reason')
    if decision not in {'accept', 'reject'}:
        fail('review_decision', 'Decision must be accept or reject')
    if not isinstance(acknowledged_findings, list) or any(not isinstance(code, str) for code in acknowledged_findings):
        fail('review_findings', 'Acknowledged findings must be a code list')
    with db.transaction():
        reviewer = assert_claim(db, document_id, token)
        document = documents.get(db, document_id)
        if document['revision'] != expected_revision:
            fail('document_stale', 'Document review revision changed')
        if not document['is_head'] or document['state'] not in {'quarantined', 'rejected'}:
            fail('review_state', 'Document is no longer reviewable')
        codes = {finding['code'] for finding in document['findings']}
        if codes != set(acknowledged_findings):
            fail('review_findings', 'Reviewer must acknowledge all and only current finding codes')
        if decision == 'accept':
            if any(not finding['overrideable'] for finding in document['findings']):
                fail('review_nonoverrideable', 'Hard rejection cannot be overridden; submit a corrected version')
            sensitive = {'email_like', 'phone_like'} & codes
            if sensitive and reviewer['role'] != 'supervisor':
                fail('review_supervisor', 'Potential identifying content requires supervisor review')
            if sources.get(db, document['source_id'])['state'] != 'active':
                fail('source_revoked', 'Cannot accept data from revoked source')
        state = 'accepted' if decision == 'accept' else 'rejected'
        db.execute('UPDATE documents SET state=?,revision=revision+1 WHERE document_id=?', (state, document_id))
        db.execute('INSERT INTO review_events(document_id,revision,actor,decision,reason,payload,at) VALUES(?,?,?,?,?,?,?)', (document_id, expected_revision + 1, reviewer['actor_id'], decision, reason, canonical({'acknowledged_findings': sorted(codes), 'claim_generation': token['generation']}), db.clock()))
        db.execute('UPDATE review_claims SET expires_at=? WHERE document_id=?', (db.clock(), document_id))
        db.changed()
        db.log('review.decided', {'document_id': document_id, 'decision': decision, 'reviewer': reviewer['actor_id'], 'revision': expected_revision + 1})
        return documents.get(db, document_id)


def heartbeat(db, document_id, token, ttl=300):
    with db.transaction():
        assert_claim(db, document_id, token)
        expiration = expires(db, ttl)
        db.execute('UPDATE review_claims SET expires_at=? WHERE document_id=?', (expiration, document_id))
        return {**token, 'expires_at': expiration}


def history(db, document_id):
    documents.get(db, document_id)
    rows = db.all('SELECT * FROM review_events WHERE document_id=? ORDER BY event_id', (document_id,))
    for row in rows:
        row['payload'] = json.loads(row['payload'])
    return rows


def queue(db, finding=None, source_id=None, limit=100):
    rows = documents.listing(db, state='quarantined', source_id=source_id, limit=10000)
    if finding is not None:
        rows = [row for row in rows if any(item['code'] == finding for item in row['findings'])]
    integer(limit, 'limit', 1, 1000)
    rows.sort(key=lambda row: (row['created_at'], row['source_id'], row['external_id'], row['document_id']))
    result = []
    for row in rows[:limit]:
        claim_row = db.one('SELECT * FROM review_claims WHERE document_id=?', (row['document_id'],))
        result.append({'document_id': row['document_id'], 'source_id': row['source_id'], 'findings': row['findings'], 'revision': row['revision'], 'claimed': bool(claim_row and claim_row['expires_at'] > db.clock()), 'tokens': row['metrics']['tokens']})
    return result
