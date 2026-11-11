import json
from corpus_gate.errors import GateError, digest, integer
from corpus_gate import documents, batch
from corpus_gate.text import profiles
from corpus_gate.ingest import recovery
from corpus_gate.dedup import clusters
from corpus_gate.releases import manifests, selection


def check(db):
    problems = []
    with db.transaction():
        database = db.integrity()
        audit = db.audit_check()
        if not database['ok']:
            problems.append({'code': 'database', 'details': database})
        if not audit['ok']:
            problems.append({'code': 'audit', 'details': audit})
        counts = recovery.reconcile_counts(db)
        problems.extend(counts['issues'])
        for row in db.all('SELECT document_id FROM documents ORDER BY document_id'):
            result = documents.verify(db, row['document_id'])
            if not result['ok']:
                problems.append({'code': 'document', 'details': result})
        for row in db.all('SELECT source_id,external_id,document_id FROM document_heads'):
            current = documents.get(db, row['document_id'])
            maximum = db.scalar('SELECT MAX(version) FROM documents WHERE source_id=? AND external_id=?', (row['source_id'], row['external_id']))
            if current['source_id'] != row['source_id'] or current['external_id'] != row['external_id'] or current['version'] != maximum:
                problems.append({'code': 'document_head', 'document_id': row['document_id']})
        for row in db.all('SELECT profile_id FROM profiles'):
            try:
                profiles.get(db, row['profile_id'])
            except GateError as exc:
                problems.append(exc.as_dict())
        for row in db.all('SELECT run_id FROM cluster_runs ORDER BY run_id'):
            try:
                clusters.get(db, row['run_id'])
                for group in clusters.groups(db, row['run_id']):
                    if group['representative'] is None:
                        problems.append({'code': 'cluster_representative', 'run_id': row['run_id'], 'cluster_id': group['cluster_id']})
            except GateError as exc:
                problems.append(exc.as_dict())
        for row in db.all('SELECT release_id FROM releases ORDER BY release_id'):
            try:
                release = manifests.get(db, row['release_id'])
                if not selection.leakage(release['members'])['ok']:
                    problems.append({'code': 'release_leakage', 'release_id': row['release_id']})
            except GateError as exc:
                problems.append(exc.as_dict())
        return {'ok': not problems, 'problems': problems, 'database': database, 'audit': audit, 'corpus_revision': db.revision()}


def overview(db):
    states = db.all('SELECT d.state,COUNT(*) AS count FROM documents d JOIN document_heads h ON h.document_id=d.document_id GROUP BY d.state ORDER BY d.state')
    batches = db.all('SELECT state,COUNT(*) AS count FROM batches GROUP BY state ORDER BY state')
    releases = db.all('SELECT state,COUNT(*) AS count FROM releases GROUP BY state ORDER BY state')
    return {'corpus_revision': db.revision(), 'documents': {row['state']: row['count'] for row in states}, 'batches': {row['state']: row['count'] for row in batches}, 'releases': {row['state']: row['count'] for row in releases}, 'sources': db.scalar('SELECT COUNT(*) FROM sources'), 'chunks': db.scalar('SELECT COUNT(*) FROM chunks'), 'cluster_runs': db.scalar('SELECT COUNT(*) FROM cluster_runs')}


def quality_report(db, source_id=None):
    rows = documents.listing(db, source_id=source_id, limit=10000)
    findings = {}
    languages = {}
    states = {}
    total_tokens = 0
    for document in rows:
        states[document['state']] = states.get(document['state'], 0) + 1
        language = document['metrics']['language']
        languages[language] = languages.get(language, 0) + 1
        total_tokens += document['metrics']['tokens']
        for finding in document['findings']:
            entry = findings.setdefault(finding['code'], {'documents': 0, 'accepted_after_review': 0})
            entry['documents'] += 1
            entry['accepted_after_review'] += document['state'] == 'accepted'
    return {'source_id': source_id, 'documents': len(rows), 'tokens': total_tokens, 'states': states, 'languages': languages, 'findings': findings, 'limit': 10000}


def source_impact(db, source_id):
    from corpus_gate.ingest import sources
    source = sources.get(db, source_id)
    docs = documents.listing(db, source_id=source_id, limit=10000)
    releases = []
    for release in manifests.listing(db):
        matching = [member['document_id'] for member in release['members'] if member['payload']['source_id'] == source_id]
        if matching:
            releases.append({'release_id': release['release_id'], 'state': release['state'], 'documents': matching, 'authorization': manifests.authorization(db, release['release_id'])})
    return {'source': source, 'current_documents': [row['document_id'] for row in docs], 'releases': releases}


def audit_page(db, after=0, limit=100):
    integer(after, 'after')
    integer(limit, 'limit', 1, 1000)
    rows = db.all('SELECT * FROM audit WHERE position>? ORDER BY position LIMIT ?', (after, limit))
    for row in rows:
        row['payload'] = json.loads(row['payload'])
    return rows


def provenance(db, document_id):
    document = documents.get(db, document_id)
    intake = db.all('SELECT batch_id,ordinal,state FROM intake WHERE document_id=? ORDER BY batch_id,ordinal', (document_id,))
    review = db.all('SELECT event_id,revision,actor,decision,reason,at FROM review_events WHERE document_id=? ORDER BY event_id', (document_id,))
    cluster_history = db.all('SELECT m.run_id,m.cluster_id,m.representative,r.corpus_revision FROM cluster_members m JOIN cluster_runs r ON r.run_id=m.run_id WHERE m.document_id=? ORDER BY r.created_at,m.run_id', (document_id,))
    release_history = db.all('SELECT m.release_id,m.split,m.digest,r.state FROM release_members m JOIN releases r ON r.release_id=m.release_id WHERE m.document_id=? ORDER BY r.created_at,m.release_id', (document_id,))
    versions = documents.versions(db, document['source_id'], document['external_id'])
    previous = [row['document_id'] for row in versions if row['version'] < document['version']]
    following = [row['document_id'] for row in versions if row['version'] > document['version']]
    return {'document_id': document_id, 'source_id': document['source_id'], 'external_id': document['external_id'], 'version': document['version'], 'state': document['state'], 'is_head': document['is_head'], 'profile_hash': document['profile_hash'], 'previous_versions': previous, 'later_versions': following, 'intake': intake, 'reviews': review, 'clusters': cluster_history, 'releases': release_history, 'integrity': documents.verify(db, document_id)}


def source_histories(db):
    problems = []
    for source in db.all('SELECT source_id,revision FROM sources ORDER BY source_id'):
        rows = db.all('SELECT * FROM source_history WHERE source_id=? ORDER BY revision', (source['source_id'],))
        revisions = [row['revision'] for row in rows]
        if revisions != list(range(1, source['revision'] + 1)):
            problems.append({'code': 'source_history_gap', 'source_id': source['source_id']})
        for row in rows:
            payload = json.loads(row['payload'])
            if payload['source_id'] != source['source_id'] or payload['revision'] != row['revision']:
                problems.append({'code': 'source_history_identity', 'source_id': source['source_id'], 'revision': row['revision']})
    for profile in db.all('SELECT profile_id,revision FROM profiles ORDER BY profile_id'):
        revisions = db.all('SELECT revision FROM profile_history WHERE profile_id=? ORDER BY revision', (profile['profile_id'],))
        if [row['revision'] for row in revisions] != list(range(1, profile['revision'] + 1)):
            problems.append({'code': 'profile_history_gap', 'profile_id': profile['profile_id']})
        for row in revisions:
            try:
                profiles.get(db, profile['profile_id'], row['revision'])
            except GateError as exc:
                problems.append(exc.as_dict())
    return {'ok': not problems, 'problems': problems}
