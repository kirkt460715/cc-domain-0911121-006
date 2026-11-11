import json
from corpus_gate.errors import identifier, canonical, digest, fail, text
from corpus_gate import documents
from corpus_gate.ingest import sources
from corpus_gate.releases import selection


def snapshot(db, document_id):
    document = documents.get(db, document_id)
    checked = documents.verify(db, document_id)
    if not checked['ok']:
        fail('release_document_corrupt', 'Document integrity failed', document_id=document_id, problems=checked['problems'])
    source = sources.get(db, document['source_id'])
    return {'document_id': document_id, 'source_id': document['source_id'], 'external_id': document['external_id'], 'version': document['version'], 'document_revision': document['revision'], 'source_revision': source['revision'], 'text': document['normalized'], 'exact_hash': document['exact_hash'], 'metadata': document['metadata'], 'metrics': document['metrics'], 'profile_hash': document['profile_hash'], 'obligations': source['policy']['obligations'], 'chunks': documents.pieces(db, document_id)}


def prepare(db, release_id, run_id, context, definition):
    identifier(release_id)
    with db.transaction():
        if db.one('SELECT release_id FROM releases WHERE release_id=?', (release_id,)):
            fail('release_exists', 'Release identifiers are immutable')
        selected = selection.select(db, run_id, context, definition)
        if not selected['chosen']:
            fail('release_empty', 'No eligible documents survive release selection')
        payload = {'options': selected['options'], 'counts': selected['counts'], 'excluded': selected['excluded'], 'run_digest': selected['run_digest']}
        members = []
        for ordinal, item in enumerate(selected['chosen']):
            document = snapshot(db, item['document_id'])
            members.append({**item, 'ordinal': ordinal, 'payload': document, 'digest': digest(document)})
        sha = digest({'definition': payload, 'context': selected['context'], 'members': members})
        db.execute("INSERT INTO releases VALUES(?,1,'prepared',?,?,?,?,?,?,NULL)", (release_id, run_id, canonical(selected['context']), canonical(payload), sha, selected['corpus_revision'], db.clock()))
        for member in members:
            db.execute('INSERT INTO release_members VALUES(?,?,?,?,?,?,?)', (release_id, member['document_id'], member['cluster_id'], member['split'], member['ordinal'], canonical(member['payload']), member['digest']))
        db.log('release.prepared', {'release_id': release_id, 'sha256': sha, 'documents': len(members)})
        event(db, release_id, 'prepared', {'sha256': sha})
        return get(db, release_id)


def event(db, release_id, action, body):
    db.execute('INSERT INTO release_events(release_id,action,body,at) VALUES(?,?,?,?)', (release_id, action, canonical(body), db.clock()))


def get(db, release_id):
    row = db.one('SELECT * FROM releases WHERE release_id=?', (release_id,))
    if row is None:
        fail('release_missing', 'Release does not exist')
    row['context'] = json.loads(row['context'])
    row['definition'] = json.loads(row['definition'])
    row['members'] = db.all('SELECT document_id,cluster_id,split,ordinal,payload,digest FROM release_members WHERE release_id=? ORDER BY ordinal', (release_id,))
    for member in row['members']:
        member['payload'] = json.loads(member['payload'])
        if digest(member['payload']) != member['digest']:
            fail('release_member_corrupt', 'Frozen document payload differs from digest')
    if digest({'definition': row['definition'], 'context': row['context'], 'members': row['members']}) != row['digest']:
        fail('release_corrupt', 'Release manifest digest mismatch')
    return row


def authorization(db, release_id, at=None):
    release = get(db, release_id)
    context = {**release['context']}
    if at is not None:
        from corpus_gate.errors import instant
        context['at'] = instant(at)
    failures = []
    if release['state'] == 'withdrawn':
        failures.append({'code': 'release_withdrawn'})
    for member in release['members']:
        frozen = member['payload']
        current = documents.get(db, member['document_id'])
        if current['state'] != 'accepted':
            failures.append({'code': 'document_state', 'document_id': current['document_id'], 'state': current['state']})
        decision = sources.authorize(db, frozen['source_id'], context, exporting=True)
        if not decision['allowed']:
            failures.append({'code': 'source_authorization', 'document_id': current['document_id'], 'decision': decision})
        elif decision['source_revision'] != frozen['source_revision']:
            failures.append({'code': 'source_revision', 'document_id': current['document_id'], 'frozen': frozen['source_revision'], 'current': decision['source_revision']})
    return {'allowed': not failures, 'failures': failures, 'context': context}


def commit(db, release_id, expected_revision):
    with db.transaction():
        release = get(db, release_id)
        if release['revision'] != expected_revision:
            fail('release_stale', 'Release revision changed')
        if release['state'] != 'prepared':
            fail('release_state', 'Only prepared releases may be committed')
        if release['corpus_revision'] != db.revision():
            fail('release_corpus_changed', 'Corpus changed after selection; prepare a new release')
        decision = authorization(db, release_id)
        if not decision['allowed']:
            fail('release_authorization', 'Release authorization changed', failures=decision['failures'])
        checked = selection.leakage(release['members'])
        if not checked['ok']:
            fail('release_leakage', 'Duplicate cluster crosses dataset split')
        db.execute("UPDATE releases SET state='published',revision=revision+1,published_at=? WHERE release_id=?", (db.clock(), release_id))
        db.log('release.published', {'release_id': release_id, 'digest': release['digest']})
        event(db, release_id, 'published', {'digest': release['digest']})
        return get(db, release_id)


def withdraw(db, release_id, expected_revision, reason):
    reason = text(reason, 'reason')
    with db.transaction():
        release = get(db, release_id)
        if release['revision'] != expected_revision:
            fail('release_stale', 'Release revision changed')
        if release['state'] == 'withdrawn':
            return release
        db.execute("UPDATE releases SET state='withdrawn',revision=revision+1 WHERE release_id=?", (release_id,))
        event(db, release_id, 'withdrawn', {'reason': reason})
        db.log('release.withdrawn', {'release_id': release_id, 'reason': reason})
        return get(db, release_id)


def listing(db):
    return [get(db, row['release_id']) for row in db.all('SELECT release_id FROM releases ORDER BY created_at,release_id')]


def history(db, release_id):
    get(db, release_id)
    rows = db.all('SELECT * FROM release_events WHERE release_id=? ORDER BY event_id', (release_id,))
    for row in rows:
        row['body'] = json.loads(row['body'])
    return rows
