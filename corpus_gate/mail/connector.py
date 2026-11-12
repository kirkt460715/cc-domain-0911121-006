"""Mailbox connector: fetch EML deliveries and admit their parts as documents.

Mail delivery evidence (mailbox epoch, UID, MIME path, EML digest) is kept in
its own tables, separate from document identity.  Documents are content
addressed, so forwarded duplicates collapse through the normal document
deduplication while every retained part still records where it came from.
"""
import hashlib
import json
import urllib.request
from corpus_gate.errors import GateError, canonical, digest, fail, identifier, integer, shape, text
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import batch
from corpus_gate.mail.parsing import parse_message

PART_STATES = ('staged', 'processed', 'duplicate', 'failed', 'ignored')
DELIVERY_TERMINAL = ('processed', 'partial', 'failed')


def http_fetch(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except (OSError, ValueError) as exc:
        fail('mail_fetch', 'Mailbox endpoint could not be fetched', url=url, detail=str(exc))


def _listing_request(base_url, epoch):
    address = base_url if epoch is None else base_url + '?epoch=' + str(epoch)
    try:
        with urllib.request.urlopen(address, timeout=30) as response:
            listing = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        fail('mail_fetch', 'Mailbox listing could not be fetched', url=address, detail=str(exc))
    shape(listing, {'mailbox_epoch', 'messages'})
    mailbox_epoch = identifier(listing['mailbox_epoch'], 'mailbox_epoch')
    if not isinstance(listing['messages'], list) or not listing['messages']:
        fail('mail_listing', 'Mailbox listing must contain at least one message')
    messages = []
    for entry in listing['messages']:
        shape(entry, {'uid', 'url'})
        integer(entry['uid'], 'uid', 1, 1000000000)
        text(entry['url'], 'url')
        messages.append({'uid': entry['uid'], 'url': entry['url']})
    return mailbox_epoch, messages


def _resolve_url(base_url, url):
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return base_url.rstrip('/') + '/' + url.lstrip('/')


def _delivery_id(mailbox_id, epoch, uid):
    return 'mail-' + digest([mailbox_id, epoch, uid])[:24]


def _part_id(delivery_id, mime_path):
    return delivery_id + '-p' + digest(mime_path)[:16]


def _external_id(role, content_digest, mime_path, filename):
    if role == 'body':
        return 'mail-body-' + content_digest[:24]
    name = filename or mime_path
    return 'mail-att-' + digest([content_digest, name])[:24]


def _record_for_part(part):
    # Provenance (MIME path, filename, charset) lives in mail_parts and stays
    # mutable delivery evidence.  Document identity is content addressed, so a
    # forwarded copy carrying the same text must dedupe even though its MIME
    # path differs; document metadata therefore stays minimal and stable.
    metadata = {'origin': 'mailbox', 'role': part['role'], 'content_digest': part['content_digest']}
    return {'external_id': _external_id(part['role'], part['content_digest'], part['mime_path'], part.get('filename')),
            'version': 1, 'text': part['text'], 'metadata': metadata}


def _row(db, table, key_field, key_value):
    row = db.one(f'SELECT * FROM {table} WHERE {key_field}=?', (key_value,))
    if row is None:
        fail('mail_record_missing', 'Mailbox record does not exist', **{key_field: key_value})
    for field in ('error',):
        if row.get(field):
            row[field] = json.loads(row[field])
    return row


def _part_rows(db, delivery_id, state=None):
    query = 'SELECT * FROM mail_parts WHERE delivery_id=?'
    args = [delivery_id]
    if state is not None:
        query += ' AND state=?'
        args.append(state)
    rows = db.all(query + ' ORDER BY mime_path', args)
    for row in rows:
        if row['error']:
            row['error'] = json.loads(row['error'])
    return rows


def _delivery_state(parts):
    staged = [part for part in parts if part['state'] in ('staged', 'processed', 'duplicate')]
    failed = [part for part in parts if part['state'] == 'failed']
    if not staged and failed:
        return 'failed'
    if failed:
        return 'partial'
    return 'processed'


def _index_message(db, delivery_id, message_id, eml_digest):
    if not message_id:
        return None
    existing = db.one('SELECT * FROM mail_message_index WHERE message_id=? AND eml_digest=?', (message_id, eml_digest))
    at = db.clock()
    if existing:
        return {'message_id': message_id, 'eml_digest': eml_digest, 'state': existing['state']}
    siblings = db.all('SELECT DISTINCT eml_digest FROM mail_message_index WHERE message_id=?', (message_id,))
    state = 'conflict' if siblings else 'unique'
    db.execute('INSERT INTO mail_message_index VALUES(?,?,?,?,?)', (message_id, eml_digest, delivery_id, at, state))
    if siblings:
        # Same Message-ID now points at more than one distinct EML: backfill the
        # earlier rows so the conflict is queryable from either side.
        db.execute("UPDATE mail_message_index SET state='conflict' WHERE message_id=?", (message_id,))
    return {'message_id': message_id, 'eml_digest': eml_digest, 'state': state}


def _persist_parts(db, delivery, parsed, batch_id):
    records = []
    saved = []
    now = db.clock()
    for part in parsed['parts']:
        part_id = _part_id(delivery['delivery_id'], part['mime_path'])
        values = {
            'part_id': part_id, 'delivery_id': delivery['delivery_id'], 'mime_path': part['mime_path'],
            'role': part['role'], 'media_type': part['media_type'], 'charset': part.get('charset'),
            'filename': part.get('filename'), 'content_digest': part.get('content_digest'),
            'byte_size': part.get('byte_size'), 'summary': part.get('summary'),
            'state': part['state'], 'error': None, 'document_id': None, 'batch_id': None,
            'ordinal': None, 'ignored_reason': None, 'created_at': now, 'updated_at': now,
        }
        if part['state'] == 'failed':
            values['error'] = canonical(part['error'])
        elif part['state'] == 'ignored':
            values['ignored_reason'] = part['ignored_reason']
        else:
            record = _record_for_part(part)
            values['ordinal'] = len(records)
            values['batch_id'] = batch_id
            records.append(record)
            saved.append((part_id, values['ordinal']))
        columns = tuple(values)
        db.execute(f"INSERT INTO mail_parts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                   tuple(values[column] for column in columns))
    return records, saved


def _complete_delivery(db, delivery_id, batch_id, records, saved):
    completed = batch.run(db, batch_id)
    intake = {row['ordinal']: row for row in batch.records(db, batch_id)}
    parts = _part_rows(db, delivery_id)
    now = db.clock()
    for part_id, ordinal in saved:
        row = intake[ordinal]
        if row['state'] in ('processed', 'duplicate'):
            db.execute('UPDATE mail_parts SET state=?,document_id=?,updated_at=? WHERE part_id=?', (row['state'], row['document_id'], now, part_id))
        else:
            db.execute("UPDATE mail_parts SET state='failed',error=?,updated_at=? WHERE part_id=?", (row['error'] and canonical(row['error']), now, part_id))
    state = _delivery_state(_part_rows(db, delivery_id))
    db.execute('UPDATE mail_deliveries SET state=?,batch_id=?,updated_at=? WHERE delivery_id=?', (state, batch_id, now, delivery_id))
    db.log('mail.delivery_completed', {'delivery_id': delivery_id, 'state': state, 'batch_id': batch_id, 'records': len(records)})
    return state, completed


def _process_delivery(db, delivery_id, raw, source_id, profile_id, eml_digest):
    parsed = parse_message(raw)
    parts = parsed['parts']
    staged = [part for part in parts if part['state'] == 'staged']
    now = db.clock()
    # A re-fetch after an interrupted attempt rebuilds part evidence from
    # scratch; the underlying intake batches stay immutable and dedupe.
    db.execute('DELETE FROM mail_parts WHERE delivery_id=?', (delivery_id,))
    batch_id = None
    state = 'failed'
    if staged:
        batch_id = 'batch-' + delivery_id
        with db.transaction():
            records, saved = _persist_parts(db, {'delivery_id': delivery_id}, parsed, batch_id)
            db.execute('UPDATE mail_deliveries SET state=?,batch_id=?,subject=?,message_id=?,updated_at=? WHERE delivery_id=?',
                       ('staged', batch_id, parsed['subject'], parsed['message_id'], now, delivery_id))
            batch.stage(db, batch_id, source_id, profile_id, records)
        state, completed = _complete_delivery(db, delivery_id, batch_id, records, saved)
    else:
        with db.transaction():
            _persist_parts(db, {'delivery_id': delivery_id}, parsed, batch_id)
            db.execute('UPDATE mail_deliveries SET state=?,subject=?,message_id=?,updated_at=? WHERE delivery_id=?',
                       ('failed', parsed['subject'], parsed['message_id'], now, delivery_id))
            db.log('mail.delivery_failed', {'delivery_id': delivery_id, 'reason': 'no_extractable_part'})
        completed = None
    with db.transaction():
        conflict = _index_message(db, delivery_id, parsed['message_id'], eml_digest)
    return {'delivery_id': delivery_id, 'state': state, 'parts': len(parts), 'staged': len(staged),
            'batch_id': batch_id, 'message_id': parsed['message_id'], 'subject': parsed['subject'],
            'conflict': conflict, 'batch': completed}


def _authorize(db, source_id, context):
    sources.get(db, source_id)
    decision = sources.authorize(db, source_id, context)
    if not decision['allowed']:
        fail('mail_unauthorized', 'Source authorization does not permit mailbox import', reasons=decision['reasons'])
    return decision


def sync(db, mailbox_id, base_url, source_id, profile_id, context, epoch=None, limit=None, fetcher=http_fetch, sync_id=None, mailbox_endpoint=None):
    """Fetch one mailbox listing and admit every message that is not terminal."""
    identifier(mailbox_id)
    identifier(source_id)
    identifier(profile_id)
    if limit is not None:
        integer(limit, 'limit', 1, 10000)
    if sync_id is not None:
        identifier(sync_id)
    context = sources.request(context)
    _authorize(db, source_id, context)
    profiles.get(db, profile_id)
    endpoint = mailbox_endpoint or (base_url.rstrip('/') + '/mailbox')
    mailbox_epoch, messages = _listing_request(endpoint, epoch)
    if limit is not None:
        messages = messages[:limit]
    sync_id = sync_id or 'sync-' + digest([mailbox_id, mailbox_epoch, db.clock()])[:20]
    now = db.clock()
    with db.transaction():
        db.execute('INSERT INTO mail_syncs VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (sync_id, mailbox_id, mailbox_epoch, source_id, profile_id, 'running', len(messages), 0, now, now))
        db.log('mail.sync_started', {'sync_id': sync_id, 'mailbox': mailbox_id, 'epoch': mailbox_epoch, 'messages': len(messages)})
    results = []
    for entry in messages:
        delivery_id = _delivery_id(mailbox_id, mailbox_epoch, entry['uid'])
        existing = db.one('SELECT * FROM mail_deliveries WHERE delivery_id=?', (delivery_id,))
        if existing and existing['state'] in DELIVERY_TERMINAL:
            results.append({'delivery_id': delivery_id, 'state': existing['state'], 'skipped': 'terminal'})
            continue
        raw = fetcher(_resolve_url(base_url, entry['url']))
        eml_digest = hashlib.sha256(raw).hexdigest()
        now = db.clock()
        with db.transaction():
            if existing:
                db.execute('UPDATE mail_deliveries SET url=?,eml_digest=?,state=?,error=?,updated_at=? WHERE delivery_id=?',
                           (entry['url'], eml_digest, 'fetching', None, now, delivery_id))
            else:
                db.execute('INSERT INTO mail_deliveries(delivery_id,mailbox_id,epoch,uid,url,source_id,profile_id,message_id,subject,eml_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (delivery_id, mailbox_id, mailbox_epoch, entry['uid'], entry['url'], source_id, profile_id, None, None, eml_digest, 'fetching', now, now))
        try:
            result = _process_delivery(db, delivery_id, raw, source_id, profile_id, eml_digest)
        except GateError as exc:
            now = db.clock()
            with db.transaction():
                db.execute("UPDATE mail_deliveries SET state='failed',error=?,updated_at=? WHERE delivery_id=?",
                           (canonical(exc.as_dict()), now, delivery_id))
                db.log('mail.delivery_error', {'delivery_id': delivery_id, 'code': exc.code})
            result = {'delivery_id': delivery_id, 'state': 'failed', 'error': exc.as_dict()}
        results.append(result)
    failures = sum(1 for row in results if row['state'] in ('failed', 'partial'))
    state = 'complete' if failures == 0 else 'partial'
    now = db.clock()
    with db.transaction():
        db.execute('UPDATE mail_syncs SET state=?,deliveries=?,updated_at=? WHERE sync_id=?', (state, len(results), now, sync_id))
        db.log('mail.sync_completed', {'sync_id': sync_id, 'state': state, 'deliveries': len(results), 'failures': failures})
    return {'sync_id': sync_id, 'mailbox_id': mailbox_id, 'epoch': mailbox_epoch, 'state': state,
            'fetched_total': len(messages), 'deliveries': results, 'failures': failures}


def get_delivery(db, delivery_id):
    row = _row(db, 'mail_deliveries', 'delivery_id', delivery_id)
    row['parts'] = _part_rows(db, delivery_id)
    return row


def list_deliveries(db, mailbox_id=None, epoch=None, state=None):
    query = 'SELECT * FROM mail_deliveries WHERE 1=1'
    args = []
    if mailbox_id is not None:
        query += ' AND mailbox_id=?'
        args.append(identifier(mailbox_id))
    if epoch is not None:
        query += ' AND epoch=?'
        args.append(identifier(epoch))
    if state is not None:
        if state not in DELIVERY_TERMINAL + ('fetching', 'staged'):
            fail('mail_state', 'Unknown delivery state')
        query += ' AND state=?'
        args.append(state)
    return [_row(db, 'mail_deliveries', 'delivery_id', row['delivery_id']) for row in db.all(query + ' ORDER BY created_at,delivery_id', args)]


def get_part(db, part_id):
    row = _row(db, 'mail_parts', 'part_id', part_id)
    return row


def list_parts(db, delivery_id=None, state=None):
    if state is not None and state not in PART_STATES:
        fail('mail_state', 'Unknown part state')
    if delivery_id is not None:
        return _part_rows(db, delivery_id, state)
    query = 'SELECT part_id FROM mail_parts'
    args = []
    if state is not None:
        query += ' WHERE state=?'
        args.append(state)
    return [get_part(db, row['part_id']) for row in db.all(query + ' ORDER BY delivery_id,mime_path', args)]


def trace_document(db, document_id):
    """Return every mail part that produced or reproduced a document."""
    rows = db.all('SELECT * FROM mail_parts WHERE document_id=? ORDER BY delivery_id,mime_path', (document_id,))
    for row in rows:
        if row['error']:
            row['error'] = json.loads(row['error'])
        row['delivery'] = get_delivery(db, row['delivery_id'])
    return {'document_id': document_id, 'parts': rows}


def conflicts(db, message_id=None):
    if message_id is None:
        return db.all("SELECT * FROM mail_message_index WHERE state='conflict' ORDER BY message_id,eml_digest")
    return db.all('SELECT * FROM mail_message_index WHERE state=? AND message_id=? ORDER BY eml_digest',
                  ('conflict', message_id))


def sync_status(db, sync_id):
    return _row(db, 'mail_syncs', 'sync_id', sync_id)


def list_syncs(db, mailbox_id=None):
    if mailbox_id is None:
        return [_row(db, 'mail_syncs', 'sync_id', row['sync_id']) for row in db.all('SELECT sync_id FROM mail_syncs ORDER BY created_at')]
    identifier(mailbox_id)
    return [_row(db, 'mail_syncs', 'sync_id', row['sync_id']) for row in db.all('SELECT sync_id FROM mail_syncs WHERE mailbox_id=? ORDER BY created_at', (mailbox_id,))]


def retry_delivery(db, delivery_id, context, corrections=None, base_url=None, fetcher=http_fetch, refetch=True, raw_override=None):
    """Re-fetch a failed/partial delivery and reprocess only its parts.

    Corrections map a MIME path to replacement document text (for parts whose
    declared charset made the original bytes unreadable).  Repeated retries do
    not create duplicate documents: content addressing and document
    immutability collapse every reprocessed part through the same pipeline.
    """
    delivery = get_delivery(db, delivery_id)
    _authorize(db, delivery['source_id'], context)
    corrections = {} if corrections is None else corrections
    if not isinstance(corrections, dict):
        fail('mail_corrections', 'Corrections must map MIME paths to replacement text')
    if refetch:
        url = delivery['url'] if base_url is None else _resolve_url(base_url, delivery['url'])
        raw = fetcher(url)
        eml_digest = hashlib.sha256(raw).hexdigest()
        if eml_digest != delivery['eml_digest'] and not corrections:
            fail('mail_eml_changed', 'Retried delivery bytes differ from retained evidence')
    else:
        raw = raw_override
        if raw is None:
            fail('mail_retry', 'Retry without refetch requires retained raw bytes')
    parsed = parse_message(raw)
    for path, replacement in corrections.items():
        text(replacement, 'correction')
        matched = [part for part in parsed['parts'] if part['mime_path'] == path]
        if not matched:
            fail('mail_correction_path', 'Correction refers to an unknown MIME path', mime_path=path)
        if matched[0]['state'] != 'failed':
            fail('mail_correction_part', 'Only failed parts accept corrections', mime_path=path)
        part = matched[0]
        part['state'] = 'staged'
        part.pop('error', None)
        part['text'] = replacement
        part['charset'] = (part.get('charset') or 'utf-8') + '+corrected'
        part['byte_size'] = len(replacement.encode('utf-8'))
        part['content_digest'] = digest(replacement)
        part['summary'] = replacement[:160]
    # Retry batches are never reused or deleted, so the existing batch count is
    # exactly the next attempt number; reprocessed content still dedupes.
    attempt = db.scalar('SELECT COUNT(*) FROM batches WHERE batch_id LIKE ?',
                        ('retry-' + delivery_id + '-%',))
    batch_id = 'retry-' + delivery_id + '-' + str(attempt)
    now = db.clock()
    with db.transaction():
        db.execute('DELETE FROM mail_parts WHERE delivery_id=?', (delivery_id,))
        records, saved = _persist_parts(db, delivery, parsed, batch_id if any(p['state'] == 'staged' for p in parsed['parts']) else None)
        if not records:
            db.execute("UPDATE mail_deliveries SET state='failed',updated_at=? WHERE delivery_id=?", (now, delivery_id))
            db.log('mail.delivery_retried', {'delivery_id': delivery_id, 'outcome': 'failed'})
            return get_delivery(db, delivery_id)
        batch.stage(db, batch_id, delivery['source_id'], delivery['profile_id'], records)
    state, completed = _complete_delivery(db, delivery_id, batch_id, records, saved)
    with db.transaction():
        _index_message(db, delivery_id, parsed['message_id'], delivery['eml_digest'])
        db.log('mail.delivery_retried', {'delivery_id': delivery_id, 'outcome': state, 'batch_id': batch_id})
    return get_delivery(db, delivery_id)
