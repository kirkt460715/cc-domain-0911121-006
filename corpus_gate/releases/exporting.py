import csv
import hashlib
import io
from corpus_gate.errors import canonical, digest, fail, shape, integer
from corpus_gate.releases import manifests, selection


def export(db, release_id, at=None, split=None):
    with db.transaction():
        release = manifests.get(db, release_id)
        if release['state'] != 'published':
            fail('release_not_published', 'Only published releases can export data')
        decision = manifests.authorization(db, release_id, at)
        if not decision['allowed']:
            fail('release_authorization', 'Current authorization blocks export', failures=decision['failures'])
        if split is not None and split not in release['definition']['options']['splits']:
            fail('release_split', 'Unknown dataset split')
        members = [row for row in release['members'] if split is None or row['split'] == split]
        payload = {'format': 'corpus-gate-release', 'version': 1, 'release_id': release_id, 'release_digest': release['digest'], 'context': decision['context'], 'selection': split, 'members': members, 'obligations': sorted({item for row in members for item in row['payload']['obligations']})}
        db.log('release.exported', {'release_id': release_id, 'split': split, 'documents': len(members), 'digest': digest(payload)})
        return {'payload': payload, 'sha256': digest(payload)}


def verify(value):
    shape(value, {'payload', 'sha256'})
    if digest(value['payload']) != value['sha256']:
        fail('export_digest', 'Export payload checksum differs')
    payload = value['payload']
    shape(payload, {'format', 'version', 'release_id', 'release_digest', 'context', 'selection', 'members', 'obligations'})
    if payload['format'] != 'corpus-gate-release' or payload['version'] != 1:
        fail('export_format', 'Unsupported corpus export format')
    identities = set()
    previous = -1
    for member in payload['members']:
        shape(member, {'document_id', 'cluster_id', 'split', 'ordinal', 'payload', 'digest'})
        if member['document_id'] in identities or member['ordinal'] <= previous:
            fail('export_order', 'Export member identities or order are invalid')
        previous = member['ordinal']
        identities.add(member['document_id'])
        if digest(member['payload']) != member['digest']:
            fail('export_document_digest', 'Frozen document checksum differs')
        if digest(member['payload']['text']) != member['payload']['exact_hash']:
            fail('export_text_digest', 'Frozen text checksum differs')
        from corpus_gate.text.chunking import verify as verify_chunks
        if not verify_chunks(member['payload']['text'], member['payload']['chunks'])['ok']:
            fail('export_chunks', 'Frozen chunk integrity differs')
    if not selection.leakage(payload['members'])['ok']:
        fail('export_leakage', 'A cluster crosses train/test splits')
    return {'integrity': True, 'documents': len(identities), 'live_authorization_checked': False}


def jsonlines(db, release_id, at=None, split=None, chunks=False):
    envelope = export(db, release_id, at, split)
    output = []
    for member in envelope['payload']['members']:
        body = member['payload']
        common = {'document_id': member['document_id'], 'source_id': body['source_id'], 'split': member['split'], 'cluster_id': member['cluster_id'], 'obligations': body['obligations']}
        if chunks:
            for piece in body['chunks']:
                output.append({**common, 'chunk_ordinal': piece['ordinal'], 'text': piece['text'], 'digest': piece['digest']})
        else:
            output.append({**common, 'text': body['text'], 'digest': body['exact_hash']})
    content = ''.join(canonical(row) + '\n' for row in output)
    return {'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest(), 'records': len(output), 'manifest': envelope}


def csv_export(db, release_id, at=None, split=None):
    envelope = export(db, release_id, at, split)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['document_id', 'source_id', 'split', 'cluster_id', 'text'])
    for row in envelope['payload']['members']:
        writer.writerow([row['document_id'], row['payload']['source_id'], row['split'], row['cluster_id'], row['payload']['text']])
    content = buffer.getvalue()
    return {'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest(), 'documents': len(envelope['payload']['members']), 'manifest': envelope}


def sample(db, release_id, seed, count=20):
    from corpus_gate.errors import identifier
    identifier(seed)
    integer(count, 'count', 1, 1000)
    release = manifests.get(db, release_id)
    ranked = sorted(release['members'], key=lambda member: (digest([seed, member['document_id']]), member['document_id']))
    return {'release_id': release_id, 'seed': seed, 'sample': [{'document_id': row['document_id'], 'split': row['split'], 'digest': row['digest']} for row in ranked[:count]], 'release_digest': release['digest']}


def compare(db, before, after):
    left = manifests.get(db, before)
    right = manifests.get(db, after)
    old = {row['document_id']: row for row in left['members']}
    new = {row['document_id']: row for row in right['members']}
    shared = sorted(set(old) & set(new))
    return {'before': before, 'after': after, 'added': sorted(set(new) - set(old)), 'removed': sorted(set(old) - set(new)), 'changed': [{'document_id': identity, 'split_before': old[identity]['split'], 'split_after': new[identity]['split'], 'content_changed': old[identity]['digest'] != new[identity]['digest']} for identity in shared if old[identity]['split'] != new[identity]['split'] or old[identity]['digest'] != new[identity]['digest']]}
