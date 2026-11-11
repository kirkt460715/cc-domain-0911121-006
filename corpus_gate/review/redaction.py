from corpus_gate.errors import integer, shape, fail, text, digest
from corpus_gate import documents, batch
from corpus_gate.review import desk


def spans(value, length):
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        fail('redaction_spans', 'Redaction requires a bounded nonempty span list')
    result = []
    for item in value:
        shape(item, {'start', 'end', 'replacement'})
        start = integer(item['start'], 'start', 0, length)
        end = integer(item['end'], 'end', 1, length)
        if start >= end:
            fail('redaction_span', 'Redaction span must be nonempty')
        replacement = item['replacement']
        if not isinstance(replacement, str) or len(replacement) > 1000:
            fail('redaction_replacement', 'Replacement must be bounded text')
        result.append({'start': start, 'end': end, 'replacement': replacement})
    result.sort(key=lambda item: (item['start'], item['end']))
    previous = -1
    for item in result:
        if item['start'] < previous:
            fail('redaction_overlap', 'Redaction spans cannot overlap')
        previous = item['end']
    return result


def apply(raw, edits):
    edits = spans(edits, len(raw))
    output = []
    cursor = 0
    for edit in edits:
        output.append(raw[cursor:edit['start']])
        output.append(edit['replacement'])
        cursor = edit['end']
    output.append(raw[cursor:])
    return ''.join(output)


def propose(db, document_id, edits, profile_id):
    document = documents.get(db, document_id)
    if not document['is_head'] or document['state'] == 'withdrawn':
        fail('redaction_state', 'Only current nonwithdrawn documents can be corrected')
    normalized_edits = spans(edits, len(document['raw_text']))
    corrected = apply(document['raw_text'], normalized_edits)
    from corpus_gate.text import profiles, quality
    profile = profiles.get(db, profile_id)
    preview = profiles.process(corrected, profile['definition'])
    return {'document_id': document_id, 'document_revision': document['revision'], 'raw_hash': digest(document['raw_text']), 'edits': normalized_edits, 'corrected_hash': digest(corrected), 'profile_id': profile_id, 'profile_revision': profile['revision'], 'profile_hash': profile['digest'], 'quality_before': {'state': document['state'], 'findings': document['findings'], 'metrics': document['metrics']}, 'quality_after': preview['quality'], 'corrected_text': corrected}


def commit(db, proposal, reviewer, reason, batch_id):
    reason = text(reason, 'reason')
    shape(proposal, {'document_id', 'document_revision', 'raw_hash', 'edits', 'corrected_hash', 'profile_id', 'profile_revision', 'profile_hash', 'quality_before', 'quality_after', 'corrected_text'})
    with db.transaction():
        actor = desk.actor(db, reviewer)
        document = documents.get(db, proposal['document_id'])
        if document['revision'] != proposal['document_revision'] or digest(document['raw_text']) != proposal['raw_hash']:
            fail('redaction_stale', 'Source document changed after proposal')
        current = propose(db, document['document_id'], proposal['edits'], proposal['profile_id'])
        if current != proposal:
            fail('redaction_proposal', 'Proposal changed or was tampered with')
        if current['quality_after']['state'] != 'accepted':
            fail('redaction_quality', 'Correction still fails the admission profile')
        metadata = {**document['metadata'], 'correction_of': document['document_id'], 'correction_reason': reason, 'corrected_by': actor['actor_id']}
        record = {'external_id': document['external_id'], 'version': document['version'] + 1, 'text': current['corrected_text'], 'metadata': metadata}
        staged = batch.stage(db, batch_id, document['source_id'], proposal['profile_id'], [record])
        completed = batch.run(db, batch_id)
        if completed['counts'].get('failed', 0):
            fail('redaction_ingest', 'Corrected document failed ingestion', records=batch.records(db, batch_id))
        result = documents.head(db, document['source_id'], document['external_id'])
        db.log('document.corrected', {'before': document['document_id'], 'after': result['document_id'], 'reviewer': reviewer, 'reason': reason})
        return result


def suggested(db, document_id, replacement='[redacted]'):
    document = documents.get(db, document_id)
    if document['raw_text'] != document['normalized']:
        fail('redaction_offsets', 'Detector offsets refer to normalized text; supply explicit raw-text offsets')
    edits = []
    for finding in document['findings']:
        if finding['code'] in {'email_like', 'phone_like'}:
            for span in finding['evidence']['spans']:
                edits.append({**span, 'replacement': replacement})
    if not edits:
        return []
    return spans(edits, len(document['raw_text']))
