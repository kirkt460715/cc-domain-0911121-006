import importlib.util
import threading
import urllib.request
from pathlib import Path

from tests.support import Case, authorization, context
from corpus_gate.errors import GateError
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import documents, inspection
from corpus_gate.mail import connector, parsing
from corpus_gate.mail.connector import parse_message

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'fixtures'


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


class FakeMailbox:
    """In-memory mailbox standing in for the HTTP source."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        for entry in self.entries:
            if url.endswith(entry['url']):
                return fixture_bytes(entry['file'])
        raise AssertionError('unexpected fetch: ' + url)

    def listing(self, epoch=None):
        label = 'epoch-2' if epoch == 2 else 'epoch-1'
        messages = []
        for entry in self.entries:
            entry_epoch = entry.get('epoch', 'epoch-1')
            if entry_epoch == label:
                messages.append({'uid': entry['uid'], 'url': entry['url']})
        return {'mailbox_epoch': label, 'messages': messages}


class MailCase(Case):
    def setUp(self):
        super().setUp()
        self.context = context()
        self.epoch_one = FakeMailbox([
            {'uid': 1, 'url': '/mail/1', 'file': 'message.eml'},
            {'uid': 2, 'url': '/mail/2', 'file': 'bad.eml'},
            {'uid': 3, 'url': '/mail/3', 'file': 'partial.eml'},
            {'uid': 4, 'url': '/mail/4', 'file': 'forward.eml'},
        ])

    def sync(self, mailbox, fetcher=None, epoch=None, limit=None, sync_id=None):
        fake = FakeMailbox(mailbox)
        original = connector._listing_request

        def listing_request(base_url, requested_epoch):
            listing = fake.listing(2 if requested_epoch == 2 else None)
            return listing['mailbox_epoch'], listing['messages']

        connector._listing_request = listing_request
        try:
            return connector.sync(self.db, 'drill-mailbox', 'http://mailbox.invalid', 'source', 'default',
                                  self.context, epoch=epoch, limit=limit,
                                  fetcher=fetcher or fake.fetch, sync_id=sync_id or 'sync-drill')
        finally:
            connector._listing_request = original


class MailParsingTests(Case):
    def test_alternative_prefers_plain_text_and_decodes_rfc2231_filename(self):
        parsed = parse_message(fixture_bytes('message.eml'))
        body = [part for part in parsed['parts'] if part['role'] == 'body']
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['mime_path'], '1.1')
        self.assertEqual(body[0]['text'].strip(), '维护步骤：先核对设备编号，再检查温度。')
        ignored = [part for part in parsed['parts'] if part['state'] == 'ignored']
        self.assertEqual(ignored[0]['ignored_reason'], 'alternative_not_selected')
        attachment = [part for part in parsed['parts'] if part['role'] == 'attachment' and part['state'] == 'staged']
        self.assertEqual(attachment[0]['filename'], '记录.txt')
        self.assertEqual(attachment[0]['text'], '部件 A 的检查记录。')
        self.assertEqual(attachment[0]['mime_path'], '2')

    def test_html_fallback_is_converted_to_text(self):
        eml = (b'MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary="b"\r\n\r\n'
               b'--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<article><p>hello <b>world</b></p></article>\r\n--b--\r\n')
        parsed = parse_message(eml)
        self.assertEqual(parsed['parts'][0]['media_type'], 'text/html')
        self.assertEqual(parsed['parts'][0]['text'], 'hello world')

    def test_unknown_charset_is_a_failed_part_not_a_crash(self):
        parsed = parse_message(fixture_bytes('bad.eml'))
        self.assertEqual(parsed['parts'][0]['state'], 'failed')
        self.assertEqual(parsed['parts'][0]['error']['code'], 'mail_charset')

    def test_signature_images_and_disallowed_types_are_ignored(self):
        parsed = parse_message(fixture_bytes('partial.eml'))
        states = {(part['mime_path'], part['state'], part.get('ignored_reason')) for part in parsed['parts']}
        self.assertIn(('2', 'ignored', 'signature_image'), states)
        self.assertIn(('3', 'failed', None), states)
        self.assertTrue(any(part['role'] == 'body' and part['state'] == 'staged' for part in parsed['parts']))

    def test_missing_charset_does_not_silently_corrupt_utf8(self):
        parsed = parse_message(fixture_bytes('message.eml'))
        attachment = next(part for part in parsed['parts'] if part.get('filename') == '记录.txt')
        self.assertEqual(attachment['charset'], 'utf-8')


class MailConnectorTests(MailCase):
    def test_full_sync_admits_body_and_attachment_with_provenance(self):
        result = self.sync([{'uid': 1, 'url': '/mail/1', 'file': 'message.eml'}])
        self.assertEqual(result['state'], 'complete')
        delivery = connector.get_delivery(self.db, result['deliveries'][0]['delivery_id'])
        self.assertEqual(delivery['state'], 'processed')
        self.assertEqual(delivery['epoch'], 'epoch-1')
        self.assertEqual(delivery['uid'], 1)
        paths = {part['mime_path']: part for part in delivery['parts']}
        self.assertEqual(paths['1.1']['state'], 'processed')
        self.assertEqual(paths['2']['state'], 'processed')
        body_doc = documents.get(self.db, paths['1.1']['document_id'])
        self.assertEqual(body_doc['metadata']['role'], 'body')
        self.assertEqual(paths['1.1']['mime_path'], '1.1')
        attachment_doc = documents.get(self.db, paths['2']['document_id'])
        self.assertEqual(paths['2']['filename'], '记录.txt')
        traced = connector.trace_document(self.db, body_doc['document_id'])
        self.assertEqual(len(traced['parts']), 1)
        self.assertEqual(traced['parts'][0]['delivery']['uid'], 1)
        self.assertEqual(traced['parts'][0]['delivery']['eml_digest'], delivery['eml_digest'])
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_bad_attachment_keeps_body_but_marks_delivery_partial(self):
        result = self.sync([{'uid': 3, 'url': '/mail/3', 'file': 'partial.eml'}])
        self.assertEqual(result['failures'], 1)
        delivery = connector.get_delivery(self.db, result['deliveries'][0]['delivery_id'])
        self.assertEqual(delivery['state'], 'partial')
        states = {part['mime_path']: part['state'] for part in delivery['parts']}
        self.assertEqual(states['1'], 'processed')
        self.assertEqual(states['2'], 'ignored')
        self.assertEqual(states['3'], 'failed')
        failed = next(part for part in delivery['parts'] if part['mime_path'] == '3')
        self.assertEqual(failed['error']['code'], 'mail_charset')
        body_doc_id = next(part['document_id'] for part in delivery['parts'] if part['mime_path'] == '1')
        self.assertTrue(documents.get(self.db, body_doc_id))

    def test_undecodable_delivery_fails_without_silently_succeeding(self):
        result = self.sync([{'uid': 2, 'url': '/mail/2', 'file': 'bad.eml'}])
        delivery = connector.get_delivery(self.db, result['deliveries'][0]['delivery_id'])
        self.assertEqual(delivery['state'], 'failed')
        self.assertEqual(delivery['parts'][0]['state'], 'failed')
        self.assertEqual(documents.listing(self.db), [])
        self.assertEqual(connector.sync_status(self.db, result['sync_id'])['state'], 'partial')

    def test_forwarded_duplicate_collapses_but_keeps_both_traces(self):
        result = self.sync([
            {'uid': 1, 'url': '/mail/1', 'file': 'message.eml'},
            {'uid': 4, 'url': '/mail/4', 'file': 'forward.eml'},
        ])
        self.assertEqual(result['failures'], 0)
        docs = documents.listing(self.db)
        self.assertEqual(len(docs), 2)
        deliveries = connector.list_deliveries(self.db)
        by_uid = {delivery['uid']: connector.get_delivery(self.db, delivery['delivery_id']) for delivery in deliveries}
        first_body = next(part for part in by_uid[1]['parts'] if part['mime_path'] == '1.1')
        second_body = next(part for part in by_uid[4]['parts'] if part['mime_path'] == '')
        self.assertEqual(first_body['document_id'], second_body['document_id'])
        self.assertEqual(second_body['state'], 'duplicate')
        traced = connector.trace_document(self.db, first_body['document_id'])
        self.assertEqual(len(traced['parts']), 2)

    def test_message_id_alone_is_not_a_dedup_key_conflict_is_retained(self):
        self.sync([{'uid': 1, 'url': '/mail/1', 'file': 'message.eml'}], sync_id='s1')
        result = self.sync([{'uid': 1, 'url': '/mail/new1', 'file': 'conflict.eml', 'epoch': 'epoch-2'}], epoch=2, sync_id='s2')
        self.assertEqual(result['epoch'], 'epoch-2')
        conflicts = connector.conflicts(self.db, '<maintenance-1@example.invalid>')
        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(row['state'] == 'conflict' for row in conflicts))
        docs = documents.listing(self.db)
        self.assertEqual(len(docs), 3)

    def test_uid_reuse_in_new_epoch_does_not_skip_new_mail(self):
        self.sync([{'uid': 1, 'url': '/mail/1', 'file': 'message.eml'}], sync_id='s1')
        before = connector.list_deliveries(self.db)
        self.assertEqual(len(before), 1)
        self.sync([{'uid': 1, 'url': '/mail/new1', 'file': 'conflict.eml', 'epoch': 'epoch-2'}], epoch=2, sync_id='s2')
        rows = connector.list_deliveries(self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['epoch'] for row in rows}, {'epoch-1', 'epoch-2'})
        # Re-running the same epoch is idempotent: terminal deliveries are not reprocessed.
        fetcher = FakeMailbox([{'uid': 1, 'url': '/mail/new1', 'file': 'conflict.eml', 'epoch': 'epoch-2'}])
        again = self.sync([{'uid': 1, 'url': '/mail/new1', 'file': 'conflict.eml', 'epoch': 'epoch-2'}], epoch=2, sync_id='s3')
        self.assertEqual(again['deliveries'][0].get('skipped'), 'terminal')
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(len(documents.listing(self.db)), 3)

    def test_revoked_source_stops_import_but_retains_evidence(self):
        first = self.sync([{'uid': 1, 'url': '/mail/1', 'file': 'message.eml'}], sync_id='s1')
        sources.revoke(self.db, 'source', 1, 'Shared mailbox withdrawn')
        with self.assertRaises(GateError) as caught:
            self.sync([{'uid': 4, 'url': '/mail/4', 'file': 'forward.eml'}], sync_id='s2')
        self.assertEqual(caught.exception.code, 'mail_unauthorized')
        self.assertEqual(len(connector.list_deliveries(self.db)), 1)
        retained = connector.get_delivery(self.db, first['deliveries'][0]['delivery_id'])
        self.assertEqual(retained['state'], 'processed')
        self.assertEqual(len(connector.trace_document(self.db, retained['parts'][0]['document_id'])['parts']), 1)

    def test_retry_failed_part_reprocesses_without_duplicate_documents(self):
        self.sync([
            {'uid': 1, 'url': '/mail/1', 'file': 'message.eml'},
            {'uid': 2, 'url': '/mail/2', 'file': 'bad.eml'},
        ])
        before = len(documents.listing(self.db))
        failed_delivery = connector.list_deliveries(self.db, state='failed')[0]
        retried = connector.retry_delivery(self.db, failed_delivery['delivery_id'], self.context,
                                           corrections={'': '恢复后的坏邮件正文 alpha beta gamma delta'},
                                           fetcher=lambda url: fixture_bytes('bad.eml'))
        self.assertEqual(retried['state'], 'processed')
        self.assertEqual(retried['parts'][0]['state'], 'processed')
        self.assertEqual(len(documents.listing(self.db)), before + 1)
        # A second retry hits the same content address and becomes a duplicate.
        again = connector.retry_delivery(self.db, failed_delivery['delivery_id'], self.context,
                                         corrections={'': '恢复后的坏邮件正文 alpha beta gamma delta'},
                                         fetcher=lambda url: fixture_bytes('bad.eml'))
        self.assertEqual(again['parts'][0]['state'], 'duplicate')
        self.assertEqual(len(documents.listing(self.db)), before + 1)
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_retry_without_correction_rejects_changed_eml(self):
        self.sync([{'uid': 2, 'url': '/mail/2', 'file': 'bad.eml'}])
        failed = connector.list_deliveries(self.db, state='failed')[0]
        self.fails('mail_eml_changed', connector.retry_delivery, self.db, failed['delivery_id'], self.context,
                   fetcher=lambda url: fixture_bytes('conflict.eml'))

    def test_interrupted_sync_resumes_from_checkpoint(self):
        entries = [
            {'uid': 1, 'url': '/mail/1', 'file': 'message.eml'},
            {'uid': 3, 'url': '/mail/3', 'file': 'partial.eml'},
            {'uid': 4, 'url': '/mail/4', 'file': 'forward.eml'},
        ]
        first = self.sync(entries, limit=1, sync_id='s1')
        self.assertEqual(first['fetched_total'], 1)
        second = self.sync(entries, limit=2, sync_id='s2')
        third = self.sync(entries, sync_id='s3')
        self.assertEqual(len(second['deliveries']), 2)
        self.assertEqual(len(third['deliveries']), 3)
        deliveries = connector.list_deliveries(self.db)
        self.assertEqual(len(deliveries), 3)
        # The first message was fetched exactly once across the resumable runs.
        fetches = []
        fake = FakeMailbox(entries)
        original = fake.fetch

        def counting(url):
            fetches.append(url)
            return original(url)

        self.sync(entries, fetcher=counting, sync_id='s4')
        self.assertEqual(fetches, [])
        partial = connector.list_deliveries(self.db, state='partial')
        self.assertEqual(len(partial), 1)

    def test_docs_go_through_dedup_and_review_pipeline(self):
        self.sync([
            {'uid': 1, 'url': '/mail/1', 'file': 'message.eml'},
            {'uid': 4, 'url': '/mail/4', 'file': 'forward.eml'},
        ])
        docs = documents.listing(self.db)
        self.assertEqual({doc['state'] for doc in docs}, {'accepted'})
        head = documents.head(self.db, 'source', docs[0]['external_id'])
        self.assertIsNotNone(head)

    def test_partial_delivery_retry_fetches_only_failed_message(self):
        self.sync([{'uid': 3, 'url': '/mail/3', 'file': 'partial.eml'}])
        delivery = connector.list_deliveries(self.db, state='partial')[0]
        retried = connector.retry_delivery(self.db, delivery['delivery_id'], self.context,
                                           corrections={'3': '修复附件内容 alpha beta gamma delta epsilon'},
                                           fetcher=lambda url: fixture_bytes('partial.eml'))
        self.assertEqual(retried['state'], 'processed')
        states = {part['mime_path']: part['state'] for part in retried['parts']}
        self.assertEqual(states['1'], 'duplicate')
        self.assertEqual(states['3'], 'processed')
        self.assertTrue(inspection.check(self.db)['ok'])


class MailEdgeCaseTests(MailCase):
    def test_csv_attachment_is_admitted_and_binary_attachment_ignored(self):
        eml = (b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
               b'--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n\xe6\x8a\xa5\xe8\xa1\xa8\xe6\xad\xa3\xe6\x96\x87\r\n'
               b'--b\r\nContent-Type: text/csv; name="data.csv"\r\nContent-Disposition: attachment; filename="data.csv"\r\n\r\nid,value\r\n1,alpha beta gamma delta\r\n'
               b'--b\r\nContent-Type: application/octet-stream; name="blob.bin"\r\nContent-Disposition: attachment; filename="blob.bin"\r\nContent-Transfer-Encoding: base64\r\n\r\nAAEC\r\n--b--\r\n')
        result = self._sync_eml(9, eml)
        delivery = connector.get_delivery(self.db, result['deliveries'][0]['delivery_id'])
        by_path = {part['mime_path']: part for part in delivery['parts']}
        self.assertEqual(by_path['1']['state'], 'processed')
        self.assertEqual(by_path['2']['state'], 'processed')
        self.assertEqual(by_path['2']['media_type'], 'text/csv')
        self.assertEqual(by_path['3']['state'], 'ignored')
        self.assertEqual(by_path['3']['ignored_reason'], 'disallowed_type')

    def test_html_only_mail_uses_html_fallback(self):
        eml = (b'MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n'
               b'<html><body><p>alpha beta <b>gamma</b> delta epsilon</p></body></html>\r\n')
        result = self._sync_eml(10, eml)
        delivery = connector.get_delivery(self.db, result['deliveries'][0]['delivery_id'])
        self.assertEqual(delivery['parts'][0]['media_type'], 'text/html')
        self.assertEqual(delivery['parts'][0]['state'], 'processed')

    def test_service_operations_are_registered(self):
        from corpus_gate.service import CorpusGate
        app = CorpusGate(clock=lambda: self.time)
        self.addCleanup(app.close)
        operations = app.operations()
        for name in ('mail.sync', 'mail.retry', 'mail.trace', 'mail.conflicts', 'mail.deliveries', 'mail.parts'):
            self.assertIn(name, operations)

    def test_legacy_version_one_database_migrates(self):
        import sqlite3
        import tempfile
        handle = tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False)
        handle.close()
        legacy = sqlite3.connect(handle.name)
        legacy.execute('PRAGMA user_version=1')
        legacy.execute('CREATE TABLE audit(position INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT,payload TEXT,previous_hash TEXT,entry_hash TEXT,at TEXT)')
        legacy.commit()
        legacy.close()
        from corpus_gate.database import Database
        migrated = Database(handle.name, clock=lambda: self.time)
        self.assertEqual(migrated.scalar('PRAGMA user_version'), 2)
        tables = {row['name'] for row in migrated.all("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('mail_deliveries', tables)
        migrated.close()

    def _sync_eml(self, uid, eml):
        fake = FakeMailbox([{'uid': uid, 'url': f'/mail/{uid}', 'file': 'message.eml'}])
        original = connector._listing_request

        def listing_request(base_url, requested_epoch):
            return 'epoch-1', [{'uid': uid, 'url': f'/mail/{uid}'}]

        connector._listing_request = listing_request
        try:
            return connector.sync(self.db, 'drill-mailbox', 'http://mailbox.invalid', 'source', 'default',
                                  self.context, fetcher=lambda url: eml, sync_id=f'sync-{uid}')
        finally:
            connector._listing_request = original


class MailHttpIntegrationTests(MailCase):
    def test_runs_against_loopback_demo_source(self):
        spec = importlib.util.spec_from_file_location('drill_source', ROOT / 'demo_support' / 'source.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        server, base = module.start()
        try:
            result = connector.sync(self.db, 'drill-mailbox', base, 'source', 'default', self.context,
                                    mailbox_endpoint=base + '/mailbox',
                                    fetcher=lambda url: urllib.request.urlopen(url).read(), sync_id='http-sync')
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(result['epoch'], 'epoch-1')
        self.assertEqual(result['fetched_total'], 4)
        states = {row['state'] for row in connector.list_deliveries(self.db)}
        self.assertEqual(states, {'processed', 'failed', 'partial'})
        # Epoch 2 reuses UID 1 and must create a separate delivery.
        server, base = module.start()
        try:
            connector.sync(self.db, 'drill-mailbox', base, 'source', 'default', self.context, epoch=2,
                           mailbox_endpoint=base + '/mailbox',
                           fetcher=lambda url: urllib.request.urlopen(url).read(), sync_id='http-sync-2')
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(connector.list_deliveries(self.db, epoch='epoch-2')), 1)
        self.assertTrue(inspection.check(self.db)['ok'])
        self.assertTrue(self.db.audit_check()['ok'])
