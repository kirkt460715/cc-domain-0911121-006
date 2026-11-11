import json
from fractions import Fraction
from corpus_gate.errors import identifier, integer, shape, canonical, digest, fail
from corpus_gate import documents

DDL = '''
CREATE TABLE IF NOT EXISTS quality_baselines(baseline_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,definition TEXT NOT NULL,snapshot TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def summarize(rows):
    states = {}
    languages = {}
    findings = {}
    lengths = []
    for row in rows:
        states[row['state']] = states.get(row['state'], 0) + 1
        language = row['metrics']['language']
        languages[language] = languages.get(language, 0) + 1
        lengths.append(row['metrics']['tokens'])
        for code in {finding['code'] for finding in row['findings']}:
            findings[code] = findings.get(code, 0) + 1
    lengths.sort()
    def quantile(numerator, denominator):
        if not lengths:
            return None
        index = ((len(lengths) - 1) * numerator) // denominator
        return lengths[index]
    return {'documents': len(rows), 'states': states, 'languages': languages, 'findings': findings, 'tokens': sum(lengths), 'min_tokens': lengths[0] if lengths else None, 'max_tokens': lengths[-1] if lengths else None, 'p50_tokens': quantile(1, 2), 'p90_tokens': quantile(9, 10), 'document_ids': sorted(row['document_id'] for row in rows)}


def proportion(count, total):
    value = Fraction(count, total) if total else Fraction(0)
    return {'numerator': value.numerator, 'denominator': value.denominator}


def distribution_distance(left, right):
    left_total = sum(left.values())
    right_total = sum(right.values())
    if not left_total or not right_total:
        return None
    distance = sum(abs(Fraction(left.get(key, 0), left_total) - Fraction(right.get(key, 0), right_total)) for key in set(left) | set(right)) / 2
    return {'numerator': distance.numerator, 'denominator': distance.denominator}


class Baselines:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('baseline_transaction', 'Initialize baseline store outside transactions')
        db.db.executescript(DDL)

    def capture(self, baseline_id, source_id, minimum_documents=1):
        identifier(baseline_id)
        integer(minimum_documents, 'minimum_documents', 1, 10000)
        with self.db.transaction():
            if self.db.one('SELECT baseline_id FROM quality_baselines WHERE baseline_id=?', (baseline_id,)):
                fail('baseline_exists', 'Baseline identifiers are immutable')
            from corpus_gate.ingest import sources
            source = sources.get(self.db, source_id)
            rows = documents.listing(self.db, source_id=source_id, limit=10000)
            if len(rows) < minimum_documents:
                fail('baseline_insufficient', 'Too few documents for baseline')
            snapshot = summarize(rows)
            definition = {'minimum_documents': minimum_documents, 'source_revision': source['revision'], 'corpus_revision': self.db.revision()}
            self.db.execute('INSERT INTO quality_baselines VALUES(?,?,?,?,?,?)', (baseline_id, source_id, canonical(definition), canonical(snapshot), digest(snapshot), self.db.clock()))
            self.db.log('baseline.captured', {'baseline_id': baseline_id, 'documents': len(rows)})
            return self.get(baseline_id)

    def get(self, baseline_id):
        row = self.db.one('SELECT * FROM quality_baselines WHERE baseline_id=?', (baseline_id,))
        if row is None:
            fail('baseline_missing', 'Quality baseline does not exist')
        row['definition'] = json.loads(row['definition'])
        row['snapshot'] = json.loads(row['snapshot'])
        if digest(row['snapshot']) != row['digest']:
            fail('baseline_corrupt', 'Baseline digest mismatch')
        return row

    def compare(self, baseline_id, threshold_percent=20, only_new=False):
        integer(threshold_percent, 'threshold_percent', 0, 100)
        baseline = self.get(baseline_id)
        rows = documents.listing(self.db, source_id=baseline['source_id'], limit=10000)
        if only_new:
            previous_ids = set(baseline['snapshot']['document_ids'])
            rows = [row for row in rows if row['document_id'] not in previous_ids]
        current = summarize(rows)
        previous = baseline['snapshot']
        drift = []
        for dimension in ('states', 'languages'):
            distance = distribution_distance(previous[dimension], current[dimension])
            breached = distance is not None and distance['numerator'] * 100 > threshold_percent * distance['denominator']
            drift.append({'dimension': dimension, 'distance': distance, 'breached': breached})
        findings = []
        for code in sorted(set(previous['findings']) | set(current['findings'])):
            before = Fraction(previous['findings'].get(code, 0), previous['documents']) if previous['documents'] else Fraction(0)
            after = Fraction(current['findings'].get(code, 0), current['documents']) if current['documents'] else Fraction(0)
            delta = after - before
            findings.append({'code': code, 'delta': {'numerator': delta.numerator, 'denominator': delta.denominator}, 'breached': bool(current['documents']) and delta * 100 > threshold_percent})
        return {'baseline_id': baseline_id, 'current': current, 'baseline': previous, 'drift': drift, 'findings': findings, 'insufficient': len(rows) < baseline['definition']['minimum_documents'], 'alarm': any(row['breached'] for row in drift + findings)}

    def listing(self, source_id=None):
        query = 'SELECT baseline_id FROM quality_baselines'
        args = ()
        if source_id:
            query += ' WHERE source_id=?'
            args = (source_id,)
        return [self.get(row['baseline_id']) for row in self.db.all(query + ' ORDER BY baseline_id', args)]
