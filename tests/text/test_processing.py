from tests.support import Case, record, context
from corpus_gate.text import normalize, quality, chunking, profiles
from corpus_gate import documents
from corpus_gate.ingest import validation


class TextTests(Case):
    def test_unicode_normalization_and_controls(self):
        result = normalize.normalize('  Ａ\r\nB\u200b\t C  ')
        self.assertEqual(result['text'], 'A\nB C')
        self.assertIn('unicode', result['changes'])
        self.assertIn('controls', result['changes'])
        self.assertIn('newlines', result['changes'])
        self.assertEqual(normalize.normalize(result['text'])['text'], result['text'])
        self.assertEqual(normalize.normalize('Straße', {'casefold': True})['text'], 'strasse')
        self.fails('unicode_surrogate', normalize.normalize, '\ud800')
        self.fails('document_size', normalize.normalize, 'abcd', {'max_characters': 3})

    def test_tokens_and_offsets_for_mixed_scripts(self):
        value = 'hello 中文 cafe\u0301 42'
        rows = normalize.tokens(value)
        self.assertEqual([row['text'] for row in rows], ['hello', '中', '文', 'cafe\u0301', '42'])
        for row in rows:
            self.assertEqual(value[row['start']:row['end']], row['text'])
        self.assertEqual(normalize.language_hint('中文测试'), 'zh')
        self.assertEqual(normalize.language_hint('alpha beta'), 'latin')
        self.assertEqual(normalize.language_hint('123 !!!'), 'und')
        self.assertEqual(normalize.tokens('!!!'), [])
        self.assertEqual(rows[-1]['end'], len(value))

    def test_chunk_overlap_and_integrity(self):
        value = 'one two three four five six seven'
        pieces = chunking.chunks(value, {'max_tokens': 3, 'overlap_tokens': 1})
        self.assertEqual([piece['text'] for piece in pieces], ['one two three', 'three four five', 'five six seven'])
        self.assertEqual([piece['ordinal'] for piece in pieces], [0, 1, 2])
        self.assertTrue(chunking.verify(value, pieces)['ok'])
        pieces[1]['digest'] = 'bad'
        checked = chunking.verify(value, pieces)
        self.assertFalse(checked['ok'])
        self.assertEqual(checked['problems'][0]['code'], 'chunk_digest')
        self.fails('chunk_overlap', chunking.settings, {'max_tokens': 2, 'overlap_tokens': 2})
        self.assertEqual(chunking.chunks('!!!'), [])

    def test_minimum_tail_and_unicode_chunks(self):
        pieces = chunking.chunks('甲乙丙丁戊', {'max_tokens': 3, 'overlap_tokens': 0, 'minimum_tokens': 3})
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]['text'], '甲乙丙')
        self.assertEqual(pieces[0]['start_offset'], 0)
        self.assertEqual(pieces[0]['end_offset'], 3)
        self.assertEqual(pieces[0]['tokens'], 3)
        self.fails('chunk_minimum', chunking.settings, {'max_tokens': 2, 'overlap_tokens': 0, 'minimum_tokens': 3})
        self.assertTrue(chunking.verify('甲乙丙丁戊', pieces)['ok'])

    def test_quality_soft_and_hard_findings(self):
        short = quality.inspect('hello')
        empty = quality.inspect('')
        clean = quality.inspect('alpha beta gamma delta epsilon')
        self.assertEqual(short['state'], 'quarantined')
        self.assertEqual(short['findings'][0]['code'], 'too_short')
        self.assertTrue(short['findings'][0]['overrideable'])
        self.assertEqual(empty['state'], 'rejected')
        self.assertFalse(empty['findings'][0]['overrideable'])
        self.assertEqual(clean['state'], 'accepted')
        self.assertEqual(clean['metrics']['tokens'], 5)
        self.assertEqual(clean['findings'], [])

    def test_sensitive_patterns_only_expose_spans(self):
        value = 'contact synthetic@example.com for alpha beta gamma assistance'
        result = quality.inspect(value)
        finding = next(row for row in result['findings'] if row['code'] == 'email_like')
        self.assertEqual(result['state'], 'quarantined')
        self.assertNotIn('synthetic@example.com', str(finding))
        span = finding['evidence']['spans'][0]
        self.assertEqual(value[span['start']:span['end']], 'synthetic@example.com')
        self.assertTrue(finding['overrideable'])
        disabled = quality.inspect(value, {'detect_email': False})
        self.assertEqual(disabled['state'], 'accepted')
        self.assertEqual(result['metrics']['language'], 'latin')

    def test_diversity_lines_and_blocked_terms(self):
        repeated = quality.inspect('repeat\n' * 10)
        codes = {finding['code'] for finding in repeated['findings']}
        self.assertIn('low_diversity', codes)
        self.assertIn('repeated_lines', codes)
        self.assertEqual(repeated['metrics']['repeated_lines'], 9)
        blocked = quality.inspect('alpha beta forbidden gamma delta', {'blocked_terms': ['FORBIDDEN']})
        self.assertEqual(blocked['state'], 'rejected')
        self.assertFalse(blocked['findings'][0]['overrideable'])
        self.assertEqual(blocked['findings'][0]['evidence']['term'], 'forbidden')
        self.fails('quality_token_range', quality.settings, {'minimum_tokens': 10, 'maximum_tokens': 5})

    def test_profile_revision_is_immutable_and_comparable(self):
        first = profiles.get(self.db, 'default')
        second = profiles.put(self.db, 'default', 1, {'quality': {'minimum_tokens': 1}})
        self.assertEqual(second['revision'], 2)
        self.assertNotEqual(first['digest'], second['digest'])
        self.assertEqual(profiles.get(self.db, 'default', 1)['definition']['quality']['minimum_tokens'], 5)
        compared = profiles.compare_profiles(self.db, 'default', 1, 2, ['hello'])
        self.assertEqual(compared['changes'][0]['quality']['resolved'], ['too_short'])
        self.assertEqual(compared['changes'][0]['quality']['after_state'], 'accepted')
        self.fails('profile_stale', profiles.put, self.db, 'default', 1, {})
        self.assertEqual(len(profiles.listing(self.db)), 1)

    def test_preflight_does_not_write(self):
        before = self.db.revision()
        audit_count = self.db.scalar('SELECT COUNT(*) FROM audit')
        result = validation.preflight(self.db, 'source', 'default', [record(), {'bad': 1}], context())
        self.assertEqual(result['valid'], 1)
        self.assertEqual(result['invalid'], 1)
        self.assertFalse(result['writes_performed'])
        self.assertEqual(result['results'][0]['state'], 'accepted')
        self.assertEqual(self.db.revision(), before)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM audit'), audit_count)
        self.assertEqual(documents.listing(self.db), [])

    def test_profile_change_preflight_and_capacity(self):
        records = [record(value='short')]
        before = validation.preflight(self.db, 'source', 'default', records, context())
        profiles.put(self.db, 'default', 1, {'quality': {'minimum_tokens': 1}})
        after = validation.preflight(self.db, 'source', 'default', records, context())
        comparison = validation.compare_preflight(before, after)
        self.assertTrue(comparison['profile_changed'])
        self.assertEqual(len(comparison['changes']), 1)
        self.assertFalse(comparison['source_changed'])
        self.assertTrue(validation.batch_capacity(records)['fits'])
        self.assertFalse(validation.batch_capacity(records, maximum_bytes=1)['fits'])
        self.assertEqual(validation.batch_capacity(records)['records'], 1)
