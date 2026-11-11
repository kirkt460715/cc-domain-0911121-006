import unittest
from corpus_gate.service import CorpusGate
from corpus_gate.errors import GateError
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import batch, documents
from corpus_gate.dedup import clusters
from corpus_gate.releases import manifests


def authorization(**changes):
    result = {'purposes': ['research'], 'regions': ['cn', 'eu'], 'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z', 'obligations': ['attribution'], 'allow_export': True}
    result.update(changes)
    return result


def context(**changes):
    result = {'purpose': 'research', 'region': 'cn', 'at': '2026-11-11T00:00:00Z', 'fulfilled_obligations': ['attribution']}
    result.update(changes)
    return result


def selection(**changes):
    result = {'seed': 'fixed', 'splits': {'train': 8000, 'test': 2000}}
    result.update(changes)
    return result


def record(identity='doc', value='alpha beta gamma delta epsilon', version=1, **metadata):
    return {'external_id': identity, 'version': version, 'text': value, 'metadata': metadata}


class Case(unittest.TestCase):
    def setUp(self):
        self.time = '2026-11-11T00:00:00.000000Z'
        self.app = CorpusGate(clock=lambda: self.time)
        self.db = self.app.db
        self.addCleanup(self.app.close)
        sources.create(self.db, 'source', authorization())
        profiles.put(self.db, 'default', 0, {})

    def ingest(self, records=None, batch_id='batch', source='source', profile='default'):
        batch.stage(self.db, batch_id, source, profile, records or [record()])
        result = batch.run(self.db, batch_id)
        return result

    def document(self, identity='doc', source='source'):
        return documents.head(self.db, source, identity)

    def release(self, records=None, options=None):
        self.ingest(records)
        clusters.run(self.db, 'run')
        manifests.prepare(self.db, 'release', 'run', context(), options or selection())
        return manifests.commit(self.db, 'release', 1)

    def fails(self, code, function, *args, **kwargs):
        with self.assertRaises(GateError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception
