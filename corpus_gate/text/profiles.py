import json
from corpus_gate.errors import shape, identifier, integer, digest, canonical, fail
from corpus_gate.text import normalize, quality, chunking


def validate(value):
    shape(value, set(), {'normalization', 'quality', 'chunking'})
    return {'normalization': normalize.settings(value.get('normalization')), 'quality': quality.settings(value.get('quality')), 'chunking': chunking.settings(value.get('chunking'))}


def put(db, profile_id, expected_revision, definition):
    identifier(profile_id)
    integer(expected_revision, 'expected_revision')
    normalized = validate(definition)
    sha = digest(normalized)
    with db.transaction():
        previous = db.one('SELECT revision FROM profiles WHERE profile_id=?', (profile_id,))
        actual = previous['revision'] if previous else 0
        if expected_revision != actual:
            fail('profile_stale', 'Profile revision changed')
        revision = actual + 1
        db.execute('INSERT INTO profiles VALUES(?,?,?,?) ON CONFLICT(profile_id) DO UPDATE SET revision=excluded.revision,definition=excluded.definition,digest=excluded.digest', (profile_id, revision, canonical(normalized), sha))
        db.execute('INSERT INTO profile_history VALUES(?,?,?,?)', (profile_id, revision, canonical(normalized), sha))
        db.log('profile.saved', {'profile_id': profile_id, 'revision': revision, 'sha256': sha})
        return get(db, profile_id)


def get(db, profile_id, revision=None):
    if revision is None:
        row = db.one('SELECT * FROM profiles WHERE profile_id=?', (profile_id,))
    else:
        integer(revision, 'revision', 1)
        row = db.one('SELECT * FROM profile_history WHERE profile_id=? AND revision=?', (profile_id, revision))
    if row is None:
        fail('profile_missing', 'Profile version does not exist')
    row['definition'] = json.loads(row['definition'])
    if digest(row['definition']) != row['digest']:
        fail('profile_corrupt', 'Profile digest mismatch')
    return row


def process(raw, definition):
    normalized = normalize.normalize(raw, definition['normalization'])
    checked = quality.inspect(normalized['text'], definition['quality'])
    pieces = chunking.chunks(normalized['text'], definition['chunking'])
    return {'normalization': normalized, 'quality': checked, 'chunks': pieces, 'profile_hash': digest(definition)}


def compare_profiles(db, profile_id, before, after, texts):
    if not isinstance(texts, list) or len(texts) > 1000:
        fail('profile_samples', 'Profile comparison accepts up to one thousand texts')
    left = get(db, profile_id, before)
    right = get(db, profile_id, after)
    changes = []
    for index, raw in enumerate(texts):
        old = process(raw, left['definition'])
        new = process(raw, right['definition'])
        changes.append({'index': index, 'normalization_changed': old['normalization']['text'] != new['normalization']['text'], 'quality': quality.compare(old['quality'], new['quality']), 'before_chunks': len(old['chunks']), 'after_chunks': len(new['chunks'])})
    return {'profile_id': profile_id, 'before': before, 'after': after, 'changes': changes}


def listing(db):
    return [get(db, row['profile_id']) for row in db.all('SELECT profile_id FROM profiles ORDER BY profile_id')]
