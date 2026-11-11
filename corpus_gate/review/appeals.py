import json
from corpus_gate.errors import identifier, text, canonical, digest, fail
from corpus_gate.review import desk
from corpus_gate import documents

DDL = '''
CREATE TABLE IF NOT EXISTS appeals(appeal_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,document_revision INTEGER NOT NULL,requester TEXT NOT NULL,reason TEXT NOT NULL,state TEXT NOT NULL,reviewer TEXT,result TEXT,created_at TEXT NOT NULL,decided_at TEXT);
'''


class Appeals:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('appeal_transaction', 'Initialize appeals outside transaction')
        db.db.executescript(DDL)

    def submit(self, appeal_id, document_id, requester, reason):
        identifier(appeal_id)
        identifier(requester)
        reason = text(reason, 'reason')
        with self.db.transaction():
            document = documents.get(self.db, document_id)
            if document['state'] != 'rejected' or not document['is_head']:
                fail('appeal_state', 'Only current rejected documents may be appealed')
            if self.db.one('SELECT appeal_id FROM appeals WHERE appeal_id=?', (appeal_id,)):
                fail('appeal_exists', 'Appeal identifier exists')
            if self.db.one("SELECT appeal_id FROM appeals WHERE document_id=? AND state='pending'", (document_id,)):
                fail('appeal_pending', 'Document already has a pending appeal')
            self.db.execute("INSERT INTO appeals(appeal_id,document_id,document_revision,requester,reason,state,created_at) VALUES(?,?,?,?,?,'pending',?)", (appeal_id, document_id, document['revision'], requester, reason, self.db.clock()))
            self.db.log('appeal.submitted', {'appeal_id': appeal_id, 'document_id': document_id})
            return self.get(appeal_id)

    def get(self, appeal_id):
        row = self.db.one('SELECT * FROM appeals WHERE appeal_id=?', (appeal_id,))
        if row is None:
            fail('appeal_missing', 'Appeal does not exist')
        row['result'] = json.loads(row['result']) if row['result'] else None
        return row

    def decide(self, appeal_id, reviewer, reopen, reason):
        if type(reopen) is not bool:
            fail('appeal_decision', 'Appeal decision must be boolean')
        reason = text(reason, 'reason')
        actor = desk.actor(self.db, reviewer)
        if actor['role'] != 'supervisor':
            fail('appeal_supervisor', 'Appeals require supervisor review')
        with self.db.transaction():
            appeal = self.get(appeal_id)
            if appeal['state'] != 'pending':
                fail('appeal_terminal', 'Appeal already decided')
            document = documents.get(self.db, appeal['document_id'])
            if not document['is_head'] or document['revision'] != appeal['document_revision']:
                fail('appeal_stale', 'Document changed after appeal submission')
            history = desk.history(self.db, document['document_id'])
            previous_actor = history[-1]['actor'] if history else None
            if reviewer in {appeal['requester'], previous_actor}:
                fail('appeal_separation', 'Requester and original reviewer cannot decide appeal')
            if reopen:
                if any(not finding['overrideable'] for finding in document['findings']):
                    fail('appeal_hard_rejection', 'Hard rejection needs corrected content, not reopening')
                self.db.execute("UPDATE documents SET state='quarantined',revision=revision+1 WHERE document_id=?", (document['document_id'],))
                self.db.changed()
            result = {'reopened': reopen, 'reason': reason, 'previous_reviewer': previous_actor}
            state = 'reopened' if reopen else 'denied'
            self.db.execute('UPDATE appeals SET state=?,reviewer=?,result=?,decided_at=? WHERE appeal_id=?', (state, reviewer, canonical(result), self.db.clock(), appeal_id))
            self.db.log('appeal.decided', {'appeal_id': appeal_id, 'reviewer': reviewer, **result})
            return self.get(appeal_id)

    def listing(self, state=None):
        query = 'SELECT appeal_id FROM appeals'
        args = ()
        if state:
            if state not in {'pending', 'reopened', 'denied'}:
                fail('appeal_filter', 'Unknown appeal state')
            query += ' WHERE state=?'
            args = (state,)
        return [self.get(row['appeal_id']) for row in self.db.all(query + ' ORDER BY created_at,appeal_id', args)]
