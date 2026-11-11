import json
from corpus_gate.errors import identifier, integer, symbols, instant, shape, fail, canonical, text


def policy(value):
    shape(value, {'purposes', 'regions', 'valid_from', 'valid_until', 'obligations'}, {'allow_export'})
    start = instant(value['valid_from'])
    end = instant(value['valid_until'])
    if start >= end:
        fail('source_interval', 'Source authorization interval is empty')
    export = value.get('allow_export', True)
    if type(export) is not bool:
        fail('source_export', 'allow_export must be boolean')
    return {'purposes': symbols(value['purposes'], 'purposes'), 'regions': symbols(value['regions'], 'regions'), 'valid_from': start, 'valid_until': end, 'obligations': symbols(value['obligations'], 'obligations', True), 'allow_export': export}


def request(value):
    shape(value, {'purpose', 'region', 'at', 'fulfilled_obligations'})
    return {'purpose': identifier(value['purpose']), 'region': identifier(value['region']), 'at': instant(value['at']), 'fulfilled_obligations': symbols(value['fulfilled_obligations'], 'obligations', True)}


def get(db, source_id):
    row = db.one('SELECT * FROM sources WHERE source_id=?', (source_id,))
    if row is None:
        fail('source_missing', 'Source is not registered')
    row['policy'] = json.loads(row['policy'])
    return row


def create(db, source_id, authorization, priority=100):
    identifier(source_id)
    normalized = policy(authorization)
    integer(priority, 'priority', 0, 1000)
    with db.transaction():
        if db.one('SELECT source_id FROM sources WHERE source_id=?', (source_id,)):
            fail('source_exists', 'Source identifier already exists')
        db.execute("INSERT INTO sources VALUES(?,1,'active',?,?,?)", (source_id, canonical(normalized), priority, db.clock()))
        result = get(db, source_id)
        db.execute('INSERT INTO source_history VALUES(?,?,?)', (source_id, 1, canonical(result)))
        db.changed()
        db.log('source.created', {'source_id': source_id})
        return result


def revise(db, source_id, expected_revision, authorization, priority, reason):
    normalized = policy(authorization)
    integer(priority, 'priority', 0, 1000)
    reason = text(reason, 'reason')
    with db.transaction():
        source = get(db, source_id)
        if source['revision'] != expected_revision:
            fail('source_stale', 'Source revision changed')
        if source['state'] != 'active':
            fail('source_revoked', 'Revoked source cannot be revised')
        db.execute('UPDATE sources SET revision=revision+1,policy=?,priority=? WHERE source_id=?', (canonical(normalized), priority, source_id))
        result = get(db, source_id)
        db.execute('INSERT INTO source_history VALUES(?,?,?)', (source_id, result['revision'], canonical(result)))
        db.changed()
        db.log('source.revised', {'source_id': source_id, 'revision': result['revision'], 'reason': reason})
        return result


def revoke(db, source_id, expected_revision, reason):
    reason = text(reason, 'reason')
    with db.transaction():
        source = get(db, source_id)
        if source['revision'] != expected_revision:
            fail('source_stale', 'Source revision changed')
        if source['state'] == 'revoked':
            return source
        db.execute("UPDATE sources SET state='revoked',revision=revision+1 WHERE source_id=?", (source_id,))
        result = get(db, source_id)
        db.execute('INSERT INTO source_history VALUES(?,?,?)', (source_id, result['revision'], canonical(result)))
        db.changed()
        db.log('source.revoked', {'source_id': source_id, 'reason': reason})
        return result


def authorize(db, source_id, context, exporting=False):
    context = request(context)
    source = get(db, source_id)
    rules = source['policy']
    reasons = []
    if source['state'] != 'active':
        reasons.append('revoked')
    if context['purpose'] not in rules['purposes']:
        reasons.append('purpose')
    if context['region'] not in rules['regions']:
        reasons.append('region')
    if not rules['valid_from'] <= context['at'] < rules['valid_until']:
        reasons.append('validity')
    missing = sorted(set(rules['obligations']) - set(context['fulfilled_obligations']))
    if missing:
        reasons.append('obligations')
    if exporting and not rules['allow_export']:
        reasons.append('export')
    return {'allowed': not reasons, 'source_id': source_id, 'source_revision': source['revision'], 'reasons': reasons, 'missing_obligations': missing, 'context': context}


def history(db, source_id):
    get(db, source_id)
    return [json.loads(row['payload']) for row in db.all('SELECT payload FROM source_history WHERE source_id=? ORDER BY revision', (source_id,))]


def listing(db):
    return [get(db, row['source_id']) for row in db.all('SELECT source_id FROM sources ORDER BY source_id')]
