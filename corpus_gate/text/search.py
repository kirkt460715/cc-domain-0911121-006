import json
from corpus_gate.errors import identifier, integer, canonical, digest, fail, shape
from corpus_gate.text.normalize import tokens
from corpus_gate import documents
from corpus_gate.ingest import sources

DDL = '''
CREATE TABLE IF NOT EXISTS search_indexes(index_id TEXT PRIMARY KEY,corpus_revision INTEGER NOT NULL,documents INTEGER NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_documents(index_id TEXT NOT NULL,document_id TEXT NOT NULL,tokens INTEGER NOT NULL,terms TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(index_id,document_id));
CREATE TABLE IF NOT EXISTS search_postings(index_id TEXT NOT NULL,term TEXT NOT NULL,document_id TEXT NOT NULL,positions TEXT NOT NULL,PRIMARY KEY(index_id,term,document_id));
CREATE INDEX IF NOT EXISTS search_term ON search_postings(index_id,term);
'''


class SearchIndex:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('search_transaction', 'Initialize search index outside transactions')
        db.db.executescript(DDL)

    def build(self, index_id):
        identifier(index_id)
        from corpus_gate.dedup.clusters import eligible
        with self.db.transaction():
            if self.db.one('SELECT index_id FROM search_indexes WHERE index_id=?', (index_id,)):
                fail('search_exists', 'Search index identifiers are immutable')
            rows = eligible(self.db)
            evidence = []
            for row in rows:
                terms = [token['text'].casefold() for token in tokens(row['normalized'])]
                positions = {}
                for ordinal, term in enumerate(terms):
                    positions.setdefault(term, []).append(ordinal)
                sha = digest(terms)
                self.db.execute('INSERT INTO search_documents VALUES(?,?,?,?,?)', (index_id, row['document_id'], len(terms), canonical(terms), sha))
                for term in sorted(positions):
                    self.db.execute('INSERT INTO search_postings VALUES(?,?,?,?)', (index_id, term, row['document_id'], canonical(positions[term])))
                evidence.append({'document_id': row['document_id'], 'digest': sha})
            self.db.execute('INSERT INTO search_indexes VALUES(?,?,?,?,?)', (index_id, self.db.revision(), len(rows), digest(evidence), self.db.clock()))
            self.db.log('search.indexed', {'index_id': index_id, 'documents': len(rows)})
            return self.status(index_id)

    def status(self, index_id):
        row = self.db.one('SELECT * FROM search_indexes WHERE index_id=?', (index_id,))
        if row is None:
            fail('search_missing', 'Search index does not exist')
        row['fresh'] = row['corpus_revision'] == self.db.revision()
        return row

    def verify(self, index_id):
        current = self.status(index_id)
        rows = self.db.all('SELECT * FROM search_documents WHERE index_id=? ORDER BY document_id', (index_id,))
        errors = []
        evidence = []
        for row in rows:
            terms = json.loads(row['terms'])
            if digest(terms) != row['digest'] or len(terms) != row['tokens']:
                errors.append({'code': 'search_document', 'document_id': row['document_id']})
            expected = {}
            for ordinal, term in enumerate(terms):
                expected.setdefault(term, []).append(ordinal)
            actual = {entry['term']: json.loads(entry['positions']) for entry in self.db.all('SELECT term,positions FROM search_postings WHERE index_id=? AND document_id=? ORDER BY term', (index_id, row['document_id']))}
            if actual != expected:
                errors.append({'code': 'search_postings', 'document_id': row['document_id']})
            evidence.append({'document_id': row['document_id'], 'digest': row['digest']})
        if len(rows) != current['documents'] or digest(evidence) != current['digest']:
            errors.append({'code': 'search_manifest'})
        return {'ok': not errors, 'problems': errors, 'fresh': current['fresh']}

    def query(self, index_id, query, context, limit=20):
        shape(query, set(), {'all', 'any', 'exclude', 'phrase', 'source_id'})
        integer(limit, 'limit', 1, 1000)
        if not self.status(index_id)['fresh']:
            fail('search_stale', 'Corpus changed; rebuild search index')
        def parse(key):
            values = query.get(key, [])
            if not isinstance(values, list) or len(values) > 30 or any(not isinstance(value, str) for value in values):
                fail('search_terms', 'Search terms must be a bounded string list')
            result = []
            for value in values:
                parsed = tokens(value)
                if len(parsed) != 1:
                    fail('search_term', 'Each query term must tokenize to exactly one term')
                result.append(parsed[0]['text'].casefold())
            return sorted(set(result))
        required = parse('all')
        optional = parse('any')
        excluded = parse('exclude')
        phrase_text = query.get('phrase', '')
        if not isinstance(phrase_text, str) or len(phrase_text) > 1000:
            fail('search_phrase', 'Phrase must be bounded text')
        phrase = [token['text'].casefold() for token in tokens(phrase_text)]
        if not required and not optional and not phrase:
            fail('search_empty', 'Query needs at least one positive criterion')
        context = sources.request(context)
        all_ids = {row['document_id'] for row in self.db.all('SELECT document_id FROM search_documents WHERE index_id=?', (index_id,))}
        def matches(term):
            return {row['document_id'] for row in self.db.all('SELECT document_id FROM search_postings WHERE index_id=? AND term=?', (index_id, term))}
        candidates = set(all_ids)
        for term in required + phrase:
            candidates &= matches(term)
        if optional:
            candidates &= set().union(*(matches(term) for term in optional))
        for term in excluded:
            candidates -= matches(term)
        ranked = []
        for identity in sorted(candidates):
            document = documents.get(self.db, identity)
            if query.get('source_id') and document['source_id'] != query['source_id']:
                continue
            if not sources.authorize(self.db, document['source_id'], context)['allowed']:
                continue
            row = self.db.one('SELECT * FROM search_documents WHERE index_id=? AND document_id=?', (index_id, identity))
            terms = json.loads(row['terms'])
            if digest(terms) != row['digest']:
                fail('search_corrupt', 'Indexed term list checksum differs')
            phrase_positions = []
            if phrase:
                phrase_positions = [start for start in range(len(terms) - len(phrase) + 1) if terms[start:start + len(phrase)] == phrase]
                if not phrase_positions:
                    continue
            matched_terms = sorted(set(terms) & set(required + optional + phrase))
            frequency = sum(terms.count(term) for term in matched_terms)
            ranked.append({'document_id': identity, 'source_id': document['source_id'], 'matched_terms': matched_terms, 'term_frequency': frequency, 'phrase_positions': phrase_positions, 'tokens': len(terms), 'exact_hash': document['exact_hash']})
        ranked.sort(key=lambda row: (-len(row['matched_terms']), -row['term_frequency'], row['tokens'], row['document_id']))
        return {'index_id': index_id, 'total': len(ranked), 'results': ranked[:limit], 'context': context}

    def concordance(self, index_id, document_id, term, context, radius=3):
        integer(radius, 'radius', 0, 20)
        result = self.query(index_id, {'all': [term]}, context, 1000)
        if document_id not in {row['document_id'] for row in result['results']}:
            fail('search_not_visible', 'Document is not visible under this query and authorization')
        document = documents.get(self.db, document_id)
        terms = tokens(document['normalized'])
        normalized = tokens(term)[0]['text'].casefold()
        spans = []
        for index, item in enumerate(terms):
            if item['text'].casefold() == normalized:
                start = terms[max(0, index - radius)]['start']
                end = terms[min(len(terms) - 1, index + radius)]['end']
                spans.append({'start': start, 'end': end, 'text': document['normalized'][start:end], 'match_start': item['start'] - start, 'match_end': item['end'] - start})
        return {'document_id': document_id, 'term': normalized, 'spans': spans}
