from tests.support import Case, record, authorization, context
from corpus_gate import batch, documents, inspection
from corpus_gate.ingest import sources, recovery, formats, bundles, validation
from corpus_gate.text import profiles


class BatchTests(Case):
    def test_batch_isolation_and_checkpoint(self):
        staged = batch.stage(self.db, 'input', 'source', 'default', [record('one'), {'bad': True}, record('two', 'different words form another clean document')])
        first = batch.process(self.db, 'input', 1, 1)
        self.assertEqual(first['cursor'], 1)
        self.assertEqual(first['state'], 'processing')
        self.assertTrue(recovery.checkpoint(self.db, 'input')['resumable'])
        self.fails('batch_stale', batch.process, self.db, 'input', 1, 1)
        result = batch.run(self.db, 'input', 1)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['counts'], {'processed': 2, 'failed': 1})
        self.assertEqual(batch.records(self.db, 'input', 'failed')[0]['ordinal'], 1)
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_duplicate_batch_and_document_conflicts(self):
        first = self.ingest()
        self.assertEqual(batch.stage(self.db, 'batch', 'source', 'default', [record()])['digest'], first['digest'])
        self.fails('batch_conflict', batch.stage, self.db, 'batch', 'source', 'default', [record(value='changed')])
        duplicate = self.ingest(batch_id='duplicate')
        self.assertEqual(duplicate['counts'], {'duplicate': 1})
        conflict = self.ingest([record(value='different alpha beta gamma delta')], batch_id='conflict')
        self.assertEqual(conflict['counts'], {'failed': 1})
        self.assertEqual(batch.records(self.db, 'conflict')[0]['error']['code'], 'document_conflict')
        self.assertEqual(len(documents.listing(self.db)), 1)
        self.assertEqual(self.document()['raw_text'], record()['text'])

    def test_versions_supersede_without_destroying_old_content(self):
        self.ingest()
        old = self.document()
        self.ingest([record(version=2, value='new alpha beta gamma delta epsilon')], 'next')
        head = self.document()
        self.assertEqual(head['version'], 2)
        self.assertNotEqual(head['document_id'], old['document_id'])
        self.assertFalse(documents.get(self.db, old['document_id'])['is_head'])
        self.assertEqual(documents.get(self.db, old['document_id'])['raw_text'], record()['text'])
        self.assertEqual(len(documents.versions(self.db, 'source', 'doc')), 2)
        self.assertEqual(len(documents.listing(self.db, heads_only=False)), 2)
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_source_revision_requires_explicit_rebase(self):
        batch.stage(self.db, 'batch', 'source', 'default', [record()])
        sources.revise(self.db, 'source', 1, authorization(regions=['cn']), 50, 'Restrict region')
        self.fails('batch_source_changed', batch.process, self.db, 'batch', 1)
        self.assertFalse(recovery.checkpoint(self.db, 'batch')['resumable'])
        self.assertEqual(len(recovery.queue(self.db)['blocked']), 1)
        updated = batch.rebase_source(self.db, 'batch', 1)
        self.assertEqual(updated['source_revision'], 2)
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(batch.run(self.db, 'batch')['counts'], {'processed': 1})
        self.assertTrue(documents.verify(self.db, self.document()['document_id'])['ok'])

    def test_batch_pins_original_profile_version(self):
        batch.stage(self.db, 'batch', 'source', 'default', [record(value='short')])
        profiles.put(self.db, 'default', 1, {'quality': {'minimum_tokens': 1}})
        completed = batch.run(self.db, 'batch')
        self.assertEqual(completed['profile_revision'], 1)
        self.assertEqual(self.document()['state'], 'quarantined')
        self.assertEqual(self.document()['profile_hash'], profiles.get(self.db, 'default', 1)['digest'])
        self.assertNotEqual(self.document()['profile_hash'], profiles.get(self.db, 'default')['digest'])
        staged = bundles.reprocess_heads(self.db, 'source', 'default', 'new-profile', self.db.revision())
        self.assertEqual(staged['total'], 1)
        batch.run(self.db, 'new-profile')
        self.assertEqual(self.document()['state'], 'accepted')
        self.assertEqual(self.document()['version'], 2)

    def test_failed_input_retry_is_new_batch(self):
        self.ingest([record(), {'external_id': 'bad'}])
        self.assertEqual(batch.get(self.db, 'batch')['counts']['failed'], 1)
        staged = recovery.retry_failures(self.db, 'batch', 'retry', corrections={'1': record('fixed')})
        self.assertEqual(staged['total'], 1)
        self.assertEqual(batch.run(self.db, 'retry')['counts'], {'processed': 1})
        self.assertEqual(batch.get(self.db, 'batch')['counts']['failed'], 1)
        self.assertEqual(self.document('fixed')['state'], 'accepted')
        self.assertEqual(len(documents.listing(self.db)), 2)
        self.fails('retry_ordinal', recovery.retry_failures, self.db, 'batch', 'invalid', corrections={'0': record()})
        self.assertTrue(recovery.reconcile_counts(self.db)['ok'])

    def test_cancel_retains_committed_prefix(self):
        batch.stage(self.db, 'batch', 'source', 'default', [record('one'), record('two')])
        current = batch.process(self.db, 'batch', 1, 1)
        cancelled = batch.cancel(self.db, 'batch', current['revision'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['cursor'], 1)
        self.assertIsNotNone(self.document('one'))
        self.assertIsNone(self.document('two'))
        self.fails('batch_terminal', batch.run, self.db, 'batch')
        self.assertFalse(recovery.checkpoint(self.db, 'batch')['resumable'])
        self.assertEqual(recovery.queue(self.db)['resumable'], [])
        self.assertTrue(recovery.reconcile_counts(self.db)['ok'])

    def test_corrupt_intake_aborts_whole_checkpoint(self):
        batch.stage(self.db, 'batch', 'source', 'default', [record('one'), record('two')])
        self.db.execute("UPDATE intake SET digest='bad' WHERE ordinal=1")
        self.fails('intake_corrupt', batch.process, self.db, 'batch', 1, 2)
        self.assertEqual(batch.get(self.db, 'batch')['cursor'], 0)
        self.assertEqual(documents.listing(self.db), [])
        self.assertEqual(batch.get(self.db, 'batch')['counts'], {'pending': 2})
        self.assertFalse(recovery.checkpoint(self.db, 'batch')['resumable'])
        self.fails('recovery_unsafe', recovery.resume, self.db, 'batch')
        self.assertEqual(self.db.depth, 0)
        self.assertTrue(self.db.audit_check()['ok'])

    def test_format_parsers_preserve_embedded_newline(self):
        parsed = formats.csv_rows('external_id,version,text\nfirst,1,"alpha\nbeta gamma delta epsilon"\n')
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['version'], 1)
        self.assertIn('\n', parsed[0]['text'])
        self.assertEqual(formats.jsonlines(formats.encode_jsonlines(parsed)), parsed)
        self.fails('csv_version', formats.csv_rows, 'external_id,version,text\nfirst,01,hello\n')
        self.fails('csv_width', formats.csv_rows, 'external_id,version,text\nfirst,1\n')
        self.fails('blank_record', formats.jsonlines, '\n')
        self.fails('duplicate_key', formats.jsonlines, '{"x":1,"x":2}\n')
        self.fails('nonfinite', formats.jsonlines, '{"x":NaN}\n')

    def test_manifest_reconciliation(self):
        self.ingest()
        exported = validation.manifest(self.db, 'source')
        row = exported['entries'][0]
        expected = [{key: row[key] for key in ('external_id', 'version', 'exact_hash')}]
        self.assertTrue(validation.reconcile_manifest(self.db, 'source', expected)['ok'])
        expected[0]['version'] = 2
        mismatch = validation.reconcile_manifest(self.db, 'source', expected)
        self.assertFalse(mismatch['ok'])
        self.assertEqual(mismatch['mismatches'][0]['fields'], ['version'])
        self.assertEqual(validation.reconcile_manifest(self.db, 'source', [])['extra'], ['doc'])
        self.assertEqual(len(exported['sha256']), 64)
        self.assertEqual(exported['corpus_revision'], self.db.revision())
