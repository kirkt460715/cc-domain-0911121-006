import json
from corpus_gate.errors import identifier, text, canonical, fail
from corpus_gate import documents
from corpus_gate.review import desk

DDL = '''
CREATE TABLE IF NOT EXISTS duplicate_constraints(left_id TEXT NOT NULL,right_id TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,PRIMARY KEY(left_id,right_id));
CREATE TABLE IF NOT EXISTS constraint_history(left_id TEXT NOT NULL,right_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(left_id,right_id,revision));
'''


class Constraints:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('constraint_transaction', 'Initialize constraint registry outside transactions')
        db.db.executescript(DDL)

    def pair(self, left_id, right_id):
        documents.get(self.db, left_id)
        documents.get(self.db, right_id)
        if left_id == right_id:
            fail('constraint_self', 'Pair constraint requires distinct documents')
        return tuple(sorted([left_id, right_id]))

    def put(self, left_id, right_id, kind, expected_revision, actor, reason):
        if kind not in {'same', 'different'}:
            fail('constraint_kind', 'Pair kind must be same or different')
        reason = text(reason, 'reason')
        reviewer = desk.actor(self.db, actor)
        if reviewer['role'] != 'supervisor':
            fail('constraint_supervisor', 'Duplicate constraints require a supervisor')
        with self.db.transaction():
            left_id, right_id = self.pair(left_id, right_id)
            row = self.db.one('SELECT * FROM duplicate_constraints WHERE left_id=? AND right_id=?', (left_id, right_id))
            revision = row['revision'] if row else 0
            if revision != expected_revision:
                fail('constraint_stale', 'Duplicate constraint revision changed')
            self.db.execute("INSERT INTO duplicate_constraints VALUES(?,?,?,?,'active',?,?) ON CONFLICT(left_id,right_id) DO UPDATE SET kind=excluded.kind,revision=excluded.revision,state=excluded.state,reason=excluded.reason,actor=excluded.actor", (left_id, right_id, kind, revision + 1, reason, actor))
            result = self.get(left_id, right_id)
            self.db.execute('INSERT INTO constraint_history VALUES(?,?,?,?)', (left_id, right_id, result['revision'], canonical(result)))
            self.db.changed()
            self.db.log('duplicate.constraint_set', result)
            return result

    def get(self, left_id, right_id):
        left_id, right_id = self.pair(left_id, right_id)
        row = self.db.one('SELECT * FROM duplicate_constraints WHERE left_id=? AND right_id=?', (left_id, right_id))
        if row is None:
            fail('constraint_missing', 'Duplicate constraint does not exist')
        return row

    def remove(self, left_id, right_id, expected_revision, actor, reason):
        reason = text(reason, 'reason')
        if desk.actor(self.db, actor)['role'] != 'supervisor':
            fail('constraint_supervisor', 'Only a supervisor may remove duplicate constraints')
        with self.db.transaction():
            row = self.get(left_id, right_id)
            if row['revision'] != expected_revision:
                fail('constraint_stale', 'Duplicate constraint changed')
            if row['state'] != 'active':
                fail('constraint_inactive', 'Constraint already removed')
            self.db.execute("UPDATE duplicate_constraints SET state='removed',revision=revision+1,actor=?,reason=? WHERE left_id=? AND right_id=?", (actor, reason, row['left_id'], row['right_id']))
            result = self.get(left_id, right_id)
            self.db.execute('INSERT INTO constraint_history VALUES(?,?,?,?)', (row['left_id'], row['right_id'], result['revision'], canonical(result)))
            self.db.changed()
            self.db.log('duplicate.constraint_removed', result)
            return result

    def listing(self, active_only=True):
        query = 'SELECT * FROM duplicate_constraints'
        if active_only:
            query += " WHERE state='active'"
        return self.db.all(query + ' ORDER BY left_id,right_id')

    def history(self, left_id, right_id):
        left_id, right_id = self.pair(left_id, right_id)
        return [json.loads(row['payload']) for row in self.db.all('SELECT payload FROM constraint_history WHERE left_id=? AND right_id=? ORDER BY revision', (left_id, right_id))]


def active(db, identities):
    exists = db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='duplicate_constraints'")
    if not exists:
        return []
    return [row for row in db.all("SELECT * FROM duplicate_constraints WHERE state='active' ORDER BY left_id,right_id") if row['left_id'] in identities and row['right_id'] in identities]


def can_union(sets, left, right, forbidden):
    roots = {sets.root(left), sets.root(right)}
    for a, b in forbidden:
        if sets.root(a) in roots and sets.root(b) in roots:
            return False
    return True
