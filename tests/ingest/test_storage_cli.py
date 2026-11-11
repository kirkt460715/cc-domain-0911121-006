import contextlib
import io
import json
import pathlib
import tempfile
from tests.support import Case, record, context, selection
from corpus_gate.service import CorpusGate
from corpus_gate import batch, documents, inspection
from corpus_gate.ingest import bundles, sources
from corpus_gate.cli import main, read, write
from corpus_gate.dedup import clusters
from corpus_gate.releases import manifests


class StorageCliTests(Case):
    def test_configuration_and_intake_roundtrip(self):
        self.ingest()
        configuration = bundles.export_configuration(self.db)
        intake = bundles.export_batch(self.db, 'batch')
        with CorpusGate() as other:
            result = bundles.import_configuration(other.db, configuration)
            self.assertEqual(result['sources'], ['source'])
            self.assertEqual(result['profiles'], ['default'])
            bundles.import_batch(other.db, 'copied', intake)
            self.assertEqual(batch.run(other.db, 'copied')['counts'], {'processed': 1})
            copied = documents.head(other.db, 'source', 'doc')
            self.assertEqual(copied['exact_hash'], self.document()['exact_hash'])
            self.assertEqual(copied['profile_hash'], self.document()['profile_hash'])
            self.assertTrue(inspection.check(other.db)['ok'])

    def test_configuration_conflict_rolls_back(self):
        envelope = bundles.export_configuration(self.db)
        self.fails('source_exists', bundles.import_configuration, self.db, envelope)
        self.assertEqual(len(sources.listing(self.db)), 1)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM profiles'), 1)
        envelope['payload']['sources'][0]['source_id'] = 'new'
        from corpus_gate.errors import digest
        envelope['sha256'] = digest(envelope['payload'])
        self.fails('profile_stale', bundles.import_configuration, self.db, envelope)
        self.fails('source_missing', sources.get, self.db, 'new')
        self.assertEqual(self.db.depth, 0)
        self.assertTrue(self.db.audit_check()['ok'])

    def test_disk_checkpoint_survives_reopen(self):
        configuration = bundles.export_configuration(self.db)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'gate.sqlite3'
            with CorpusGate(path) as first:
                bundles.import_configuration(first.db, configuration)
                batch.stage(first.db, 'batch', 'source', 'default', [record('one'), record('two')])
                batch.process(first.db, 'batch', 1, 1)
                self.assertEqual(batch.get(first.db, 'batch')['cursor'], 1)
            with CorpusGate(path) as second:
                self.assertEqual(batch.get(second.db, 'batch')['cursor'], 1)
                result = batch.run(second.db, 'batch')
                self.assertEqual(result['cursor'], 2)
                self.assertEqual(result['counts'], {'processed': 2})
                self.assertEqual(len(documents.listing(second.db)), 2)
                self.assertTrue(inspection.check(second.db)['ok'])

    def test_two_connections_reject_stale_batch_checkpoint(self):
        configuration = bundles.export_configuration(self.db)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / 'gate.sqlite3'
            with CorpusGate(path) as first, CorpusGate(path) as second:
                bundles.import_configuration(first.db, configuration)
                batch.stage(first.db, 'batch', 'source', 'default', [record('one'), record('two')])
                observed = batch.get(second.db, 'batch')
                batch.process(first.db, 'batch', observed['revision'], 1)
                self.fails('batch_stale', batch.process, second.db, 'batch', observed['revision'], 1)
                self.assertEqual(batch.get(second.db, 'batch')['cursor'], 1)
                self.assertEqual(len(documents.listing(second.db)), 1)
                batch.run(second.db, 'batch')
                self.assertEqual(len(documents.listing(first.db)), 2)
                self.assertTrue(first.db.audit_check()['ok'])

    def test_atomic_service_operation_rollback(self):
        requests = [{'operation': 'review.register', 'arguments': {'actor_id': 'reviewer', 'role': 'reviewer'}}, {'operation': 'missing', 'arguments': {}}]
        self.fails('operation_missing', self.app.transaction, requests)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM actors'), 0)
        result = self.app.transaction(requests[:1])
        self.assertEqual(result[0]['actor_id'], 'reviewer')
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM actors'), 1)
        self.assertEqual(self.db.depth, 0)
        self.assertTrue(self.app.dispatch('system.check', {})['ok'])
        self.assertTrue(self.app.dispatch('system.history', {})['ok'])
        self.fails('fields', self.app.dispatch, 'source.get', {'source_id': 'source', 'extra': True})

    def test_audit_and_history_corruption(self):
        self.ingest()
        self.assertTrue(inspection.source_histories(self.db)['ok'])
        self.assertTrue(self.db.audit_check()['ok'])
        page = inspection.audit_page(self.db, limit=2)
        self.assertEqual(len(page), 2)
        self.assertEqual(page[0]['position'], 1)
        self.assertTrue(inspection.audit_page(self.db, after=2))
        self.db.execute("UPDATE audit SET payload='{}' WHERE position=2")
        self.assertFalse(self.db.audit_check()['ok'])
        self.assertEqual(self.db.audit_check()['position'], 2)
        self.assertFalse(inspection.check(self.db)['ok'])
        self.db.execute('DELETE FROM source_history')
        self.assertFalse(inspection.source_histories(self.db)['ok'])

    def test_provenance_connects_batch_cluster_and_release(self):
        self.release()
        identity = self.document()['document_id']
        report = inspection.provenance(self.db, identity)
        self.assertEqual(report['intake'][0]['batch_id'], 'batch')
        self.assertEqual(report['clusters'][0]['run_id'], 'run')
        self.assertEqual(report['releases'][0]['release_id'], 'release')
        self.assertEqual(report['releases'][0]['state'], 'published')
        self.assertEqual(report['previous_versions'], [])
        self.assertEqual(report['later_versions'], [])
        self.assertTrue(report['integrity']['ok'])
        self.assertEqual(report['reviews'], [])
        self.assertTrue(report['is_head'])

    def test_cli_error_and_check_exit_codes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(['--db', ':memory:', 'check'])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())['ok'])
        with tempfile.TemporaryDirectory() as folder:
            request = pathlib.Path(folder) / 'input.json'
            request.write_text('{"source_id":"missing"}')
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                status = main(['--db', ':memory:', 'call', 'source.get', '--input', str(request)])
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(error.getvalue())['error']['code'], 'source_missing')
            self.assertFalse(json.loads(error.getvalue())['ok'])

    def test_cli_output_never_silently_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / 'result.json'
            write({'ok': True}, str(target))
            self.assertEqual(json.loads(read(str(target))), {'ok': True})
            self.fails('file_exists', write, {'ok': False}, str(target))
            self.assertTrue(json.loads(target.read_text())['ok'])
            self.fails('file_size', read, str(target), 1)
            self.assertEqual(target.read_text().count('\n'), 1)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['--db', ':memory:', 'operations']), 0)
            operations = json.loads(output.getvalue())
            self.assertIn('release.prepare', {row['operation'] for row in operations})
            self.assertIn('search.query', {row['operation'] for row in operations})

    def test_manual_same_exact_pair_does_not_duplicate_edge(self):
        from corpus_gate.review import desk
        self.ingest([record('a'), record('b')])
        desk.register(self.db, 'supervisor', 'supervisor')
        left = self.document('a')['document_id']
        right = self.document('b')['document_id']
        self.app.constraints.put(left, right, 'same', 0, 'supervisor', 'Confirmed duplicate')
        run = clusters.run(self.db, 'run')
        self.assertEqual(len(run['edges']), 1)
        self.assertEqual(run['edges'][0]['kind'], 'manual')
        self.assertEqual(len(clusters.groups(self.db, 'run')), 1)
        self.assertEqual(len(clusters.groups(self.db, 'run')[0]['members']), 2)
        self.assertTrue(inspection.check(self.db)['ok'])
