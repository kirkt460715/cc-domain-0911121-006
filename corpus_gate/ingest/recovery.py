import json
from corpus_gate.errors import fail, digest, integer
from corpus_gate import batch, documents
from corpus_gate.ingest import sources
from corpus_gate.text import profiles


def checkpoint(db, batch_id):
    current = batch.get(db, batch_id)
    rows = batch.records(db, batch_id)
    problems = []
    if len(rows) != current['total'] or [row['ordinal'] for row in rows] != list(range(current['total'])):
        problems.append({'code': 'intake_order'})
    if digest([row['payload'] for row in rows]) != current['digest']:
        problems.append({'code': 'batch_digest'})
    for row in rows:
        if digest(row['payload']) != row['digest']:
            problems.append({'code': 'intake_digest', 'ordinal': row['ordinal']})
        if row['ordinal'] < current['cursor'] and row['state'] == 'pending':
            problems.append({'code': 'checkpoint_pending', 'ordinal': row['ordinal']})
        if row['ordinal'] >= current['cursor'] and row['state'] != 'pending':
            problems.append({'code': 'checkpoint_ahead', 'ordinal': row['ordinal']})
        if row['state'] in {'processed', 'duplicate'}:
            checked = documents.verify(db, row['document_id'])
            if not checked['ok']:
                problems.append({'code': 'document_integrity', 'ordinal': row['ordinal'], 'details': checked})
    profile = profiles.get(db, current['profile_id'], current['profile_revision'])
    if profile['digest'] != current['profile_hash']:
        problems.append({'code': 'profile_digest'})
    source = sources.get(db, current['source_id'])
    if source['state'] != 'active':
        problems.append({'code': 'source_revoked'})
    if source['revision'] != current['source_revision']:
        problems.append({'code': 'source_revision'})
    return {'batch_id': batch_id, 'state': current['state'], 'cursor': current['cursor'], 'total': current['total'], 'problems': problems, 'resumable': not problems and current['state'] in {'staged', 'processing'}}


def resume(db, batch_id, limit=100):
    with db.transaction():
        report = checkpoint(db, batch_id)
        if not report['resumable']:
            fail('recovery_unsafe', 'Batch cannot resume from current checkpoint', checkpoint=report)
        current = batch.get(db, batch_id)
        result = batch.process(db, batch_id, current['revision'], limit)
        db.log('batch.resumed', {'batch_id': batch_id, 'from': report['cursor'], 'to': result['cursor']})
        return result


def retry_failures(db, original_batch_id, new_batch_id, profile_id=None, corrections=None):
    original = batch.get(db, original_batch_id)
    if original['state'] not in {'complete', 'cancelled'}:
        fail('retry_incomplete', 'Retry only after the original batch stops processing')
    corrections = {} if corrections is None else corrections
    if not isinstance(corrections, dict):
        fail('retry_corrections', 'Corrections must map original ordinal strings to documents')
    failed = batch.records(db, original_batch_id, 'failed')
    known = {str(row['ordinal']) for row in failed}
    if set(corrections) - known:
        fail('retry_ordinal', 'Correction refers to a nonfailed input')
    if not failed:
        fail('retry_empty', 'Original batch has no failed inputs')
    records = [corrections.get(str(row['ordinal']), row['payload']) for row in failed]
    result = batch.stage(db, new_batch_id, original['source_id'], profile_id or original['profile_id'], records)
    db.log('batch.retry_staged', {'original_batch_id': original_batch_id, 'new_batch_id': new_batch_id, 'ordinals': sorted(int(key) for key in known)})
    return result


def queue(db):
    rows = db.all("SELECT batch_id FROM batches WHERE state IN ('staged','processing') ORDER BY created_at,batch_id")
    reports = [checkpoint(db, row['batch_id']) for row in rows]
    return {'resumable': [row for row in reports if row['resumable']], 'blocked': [row for row in reports if not row['resumable']]}


def reconcile_counts(db):
    issues = []
    for row in db.all('SELECT batch_id FROM batches ORDER BY batch_id'):
        current = batch.get(db, row['batch_id'])
        counts = current['counts']
        if sum(counts.values()) != current['total']:
            issues.append({'batch_id': current['batch_id'], 'code': 'total'})
        processed = sum(count for state, count in counts.items() if state != 'pending')
        if processed != current['cursor']:
            issues.append({'batch_id': current['batch_id'], 'code': 'cursor'})
        if current['state'] == 'complete' and current['cursor'] != current['total']:
            issues.append({'batch_id': current['batch_id'], 'code': 'premature_complete'})
    return {'ok': not issues, 'issues': issues}
