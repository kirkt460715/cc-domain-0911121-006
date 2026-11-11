from tests.support import Case, record, context, authorization
from corpus_gate import documents
from corpus_gate.ingest import sources
from corpus_gate.text import drift, profiles
from corpus_gate.review import desk


class AnalysisTests(Case):
    def test_search_boolean_and_phrase_semantics(self):
        self.ingest([record('a', 'alpha beta gamma delta epsilon'), record('b', 'beta alpha gamma delta zeta'), record('c', 'unrelated words create separate content here')])
        index = self.app.search
        self.assertEqual(index.build('search')['documents'], 3)
        found = index.query('search', {'all': ['alpha'], 'exclude': ['zeta']}, context())
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['results'][0]['document_id'], self.document('a')['document_id'])
        phrase = index.query('search', {'phrase': 'alpha beta'}, context())
        self.assertEqual(phrase['total'], 1)
        self.assertEqual(phrase['results'][0]['phrase_positions'], [0])
        self.assertEqual(index.query('search', {'any': ['zeta', 'unrelated']}, context())['total'], 2)
        self.assertTrue(index.verify('search')['ok'])

    def test_search_rechecks_authorization_and_freshness(self):
        self.ingest()
        self.app.search.build('search')
        self.assertEqual(self.app.search.query('search', {'all': ['alpha']}, context(region='us'))['total'], 0)
        self.assertEqual(self.app.search.query('search', {'all': ['alpha']}, context(fulfilled_obligations=[]))['total'], 0)
        self.assertEqual(self.app.search.query('search', {'all': ['alpha']}, context())['total'], 1)
        sources.revoke(self.db, 'source', 1, 'Withdraw source')
        self.assertFalse(self.app.search.status('search')['fresh'])
        self.fails('search_stale', self.app.search.query, 'search', {'all': ['alpha']}, context())
        self.app.search.build('empty')
        self.assertEqual(self.app.search.status('empty')['documents'], 0)
        self.assertEqual(self.app.search.query('empty', {'all': ['alpha']}, context())['total'], 0)

    def test_concordance_offsets_and_index_corruption(self):
        self.ingest()
        self.app.search.build('search')
        identity = self.document()['document_id']
        result = self.app.search.concordance('search', identity, 'gamma', context(), 1)
        span = result['spans'][0]
        self.assertEqual(span['text'], 'beta gamma delta')
        self.assertEqual(span['text'][span['match_start']:span['match_end']], 'gamma')
        self.fails('search_empty', self.app.search.query, 'search', {}, context())
        self.fails('search_term', self.app.search.query, 'search', {'all': ['two words']}, context())
        self.db.execute("DELETE FROM search_postings WHERE term='gamma'")
        checked = self.app.search.verify('search')
        self.assertFalse(checked['ok'])
        self.assertEqual(checked['problems'][0]['code'], 'search_postings')

    def test_baseline_detects_language_and_admission_drift(self):
        self.ingest([record('a'), record('b', 'other clean words are useful here')])
        baseline = self.app.baselines.capture('baseline', 'source', 2)
        self.assertEqual(baseline['snapshot']['documents'], 2)
        self.assertEqual(baseline['snapshot']['states'], {'accepted': 2})
        self.ingest([record('c', '短'), record('d', '简')], 'later')
        compared = self.app.baselines.compare('baseline', 20, only_new=True)
        self.assertTrue(compared['alarm'])
        self.assertFalse(compared['insufficient'])
        self.assertEqual(compared['current']['states'], {'quarantined': 2})
        self.assertEqual(compared['drift'][0]['distance'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(compared['findings'][0]['code'], 'too_short')
        self.assertTrue(compared['findings'][0]['breached'])

    def test_baseline_is_immutable_and_handles_empty_comparison(self):
        self.ingest()
        baseline = self.app.baselines.capture('baseline', 'source')
        self.fails('baseline_exists', self.app.baselines.capture, 'baseline', 'source')
        compared = self.app.baselines.compare('baseline', only_new=True)
        self.assertTrue(compared['insufficient'])
        self.assertFalse(compared['alarm'])
        self.assertIsNone(compared['drift'][0]['distance'])
        self.assertEqual(self.app.baselines.get('baseline')['digest'], baseline['digest'])
        self.assertEqual(len(self.app.baselines.listing('source')), 1)
        self.db.execute("UPDATE quality_baselines SET digest='bad'")
        self.fails('baseline_corrupt', self.app.baselines.get, 'baseline')

    def test_fractional_distribution_distance(self):
        result = drift.distribution_distance({'a': 1, 'b': 1}, {'a': 3, 'b': 1})
        self.assertEqual(result, {'numerator': 1, 'denominator': 4})
        self.assertIsNone(drift.distribution_distance({}, {'a': 1}))
        self.assertEqual(drift.proportion(2, 6), {'numerator': 1, 'denominator': 3})
        self.assertEqual(drift.proportion(0, 0), {'numerator': 0, 'denominator': 1})
        self.ingest()
        summary = drift.summarize([self.document()])
        self.assertEqual(summary['p50_tokens'], 5)
        self.assertEqual(summary['p90_tokens'], 5)
        self.assertEqual(summary['tokens'], 5)
        self.assertEqual(summary['languages'], {'latin': 1})

    def test_annotation_coverage_merges_overlap(self):
        self.ingest()
        identity = self.document()['document_id']
        desk.register(self.db, 'reviewer', 'reviewer')
        first = self.app.annotations.add('one', identity, 0, 5, 'topic', 'reviewer', 'First word')
        second = self.app.annotations.add('two', identity, 3, 10, 'topic', 'reviewer', 'Overlapping phrase')
        self.assertEqual(first['revision'], 1)
        self.assertEqual(second['state'], 'active')
        coverage = self.app.annotations.coverage(identity)
        self.assertEqual(coverage['labels']['topic']['characters'], 10)
        self.assertEqual(coverage['labels']['topic']['intervals'], [[0, 10]])
        self.assertEqual(len(self.app.annotations.chunk_projection(identity)[0]['annotations']), 2)
        self.assertEqual(len(self.app.annotations.for_document(identity, 'topic')), 2)

    def test_annotations_project_to_overlapping_chunks(self):
        profiles.put(self.db, 'default', 1, {'chunking': {'max_tokens': 3, 'overlap_tokens': 1}})
        self.ingest([record(value='one two three four five six seven')])
        identity = self.document()['document_id']
        desk.register(self.db, 'reviewer', 'reviewer')
        self.app.annotations.add('one', identity, 8, 18, 'span', 'reviewer', 'Across boundary')
        projection = self.app.annotations.chunk_projection(identity)
        self.assertEqual(len(projection), 3)
        self.assertEqual(projection[0]['annotations'][0]['start'], 8)
        self.assertTrue(projection[0]['annotations'][0]['clipped'])
        self.assertEqual(projection[1]['annotations'][0]['start'], 0)
        self.assertFalse(projection[1]['annotations'][0]['clipped'])
        self.assertEqual(projection[2]['annotations'], [])
        self.assertEqual(self.app.annotations.coverage(identity)['labels']['span']['characters'], 10)

    def test_annotation_retraction_has_permissions_and_history(self):
        self.ingest()
        identity = self.document()['document_id']
        desk.register(self.db, 'reviewer', 'reviewer')
        desk.register(self.db, 'other', 'reviewer')
        desk.register(self.db, 'supervisor', 'supervisor')
        self.app.annotations.add('one', identity, 0, 5, 'topic', 'reviewer', 'Word')
        self.fails('annotation_owner', self.app.annotations.retract, 'one', 1, 'other', 'Remove')
        result = self.app.annotations.retract('one', 1, 'supervisor', 'Corrected')
        self.assertEqual(result['state'], 'retracted')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(len(self.app.annotations.history('one')), 2)
        self.assertEqual(self.app.annotations.for_document(identity), [])
        self.fails('annotation_revision', self.app.annotations.retract, 'one', 1, 'reviewer', 'Again')
