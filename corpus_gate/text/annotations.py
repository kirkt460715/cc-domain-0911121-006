import json
from corpus_gate.errors import identifier, integer, canonical, digest, shape, text, fail
from corpus_gate import documents
from corpus_gate.review import desk

DDL = '''
CREATE TABLE IF NOT EXISTS annotations(annotation_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,label TEXT NOT NULL,actor TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,text_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotation_history(annotation_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(annotation_id,revision));
'''


class Annotations:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('annotation_transaction', 'Initialize annotation store outside transaction')
        db.db.executescript(DDL)

    def add(self, annotation_id, document_id, start, end, label, actor, reason):
        identifier(annotation_id)
        identifier(label)
        reason = text(reason, 'reason')
        desk.actor(self.db, actor)
        with self.db.transaction():
            document = documents.get(self.db, document_id)
            integer(start, 'start', 0, len(document['normalized']))
            integer(end, 'end', 1, len(document['normalized']))
            if start >= end:
                fail('annotation_span', 'Annotation span must be nonempty')
            if self.db.one('SELECT annotation_id FROM annotations WHERE annotation_id=?', (annotation_id,)):
                fail('annotation_exists', 'Annotation identifier exists')
            sha = digest(document['normalized'][start:end])
            self.db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,'active',1,?,?)", (annotation_id, document_id, start, end, label, actor, reason, sha))
            row = self.get(annotation_id)
            self.db.execute('INSERT INTO annotation_history VALUES(?,?,?)', (annotation_id, 1, canonical(row)))
            self.db.log('annotation.added', {'annotation_id': annotation_id, 'document_id': document_id, 'label': label})
            return row

    def get(self, annotation_id):
        row = self.db.one('SELECT * FROM annotations WHERE annotation_id=?', (annotation_id,))
        if row is None:
            fail('annotation_missing', 'Annotation does not exist')
        document = documents.get(self.db, row['document_id'])
        current = document['normalized'][row['start_offset']:row['end_offset']]
        if digest(current) != row['text_hash']:
            fail('annotation_stale', 'Annotated text no longer matches stored span hash')
        return row

    def retract(self, annotation_id, expected_revision, actor, reason):
        reason = text(reason, 'reason')
        reviewer = desk.actor(self.db, actor)
        with self.db.transaction():
            row = self.get(annotation_id)
            if row['revision'] != expected_revision:
                fail('annotation_revision', 'Annotation revision changed')
            if row['actor'] != actor and reviewer['role'] != 'supervisor':
                fail('annotation_owner', 'Only author or supervisor can retract annotation')
            if row['state'] != 'active':
                fail('annotation_state', 'Annotation is already retracted')
            self.db.execute("UPDATE annotations SET state='retracted',revision=revision+1,reason=? WHERE annotation_id=?", (reason, annotation_id))
            result = self.get(annotation_id)
            self.db.execute('INSERT INTO annotation_history VALUES(?,?,?)', (annotation_id, result['revision'], canonical(result)))
            self.db.log('annotation.retracted', {'annotation_id': annotation_id, 'actor': actor})
            return result

    def for_document(self, document_id, label=None):
        documents.get(self.db, document_id)
        query = "SELECT annotation_id FROM annotations WHERE document_id=? AND state='active'"
        args = [document_id]
        if label is not None:
            query += ' AND label=?'
            args.append(identifier(label))
        return [self.get(row['annotation_id']) for row in self.db.all(query + ' ORDER BY start_offset,end_offset,annotation_id', args)]

    def chunk_projection(self, document_id):
        annotations = self.for_document(document_id)
        chunks = documents.pieces(self.db, document_id)
        result = []
        for chunk in chunks:
            mapped = []
            for annotation in annotations:
                start = max(chunk['start_offset'], annotation['start_offset'])
                end = min(chunk['end_offset'], annotation['end_offset'])
                if start < end:
                    mapped.append({'annotation_id': annotation['annotation_id'], 'label': annotation['label'], 'start': start - chunk['start_offset'], 'end': end - chunk['start_offset'], 'clipped': start != annotation['start_offset'] or end != annotation['end_offset']})
            result.append({'chunk_ordinal': chunk['ordinal'], 'annotations': mapped})
        return result

    def coverage(self, document_id):
        document = documents.get(self.db, document_id)
        grouped = {}
        for annotation in self.for_document(document_id):
            grouped.setdefault(annotation['label'], []).append((annotation['start_offset'], annotation['end_offset']))
        result = {}
        for label, intervals in grouped.items():
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            result[label] = {'characters': sum(end - start for start, end in merged), 'intervals': [list(pair) for pair in merged]}
        return {'document_id': document_id, 'characters': len(document['normalized']), 'labels': result}

    def history(self, annotation_id):
        self.get(annotation_id)
        return [json.loads(row['payload']) for row in self.db.all('SELECT payload FROM annotation_history WHERE annotation_id=? ORDER BY revision', (annotation_id,))]
