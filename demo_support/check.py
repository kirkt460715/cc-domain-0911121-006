import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source import start
from corpus_gate.service import CorpusGate
from corpus_gate.ingest import sources
from corpus_gate.text import profiles
from corpus_gate import documents
from corpus_gate.mail import connector

AUTHORIZATION = {
    'purposes': ['research'],
    'regions': ['cn', 'eu'],
    'valid_from': '2026-01-01T00:00:00Z',
    'valid_until': '2027-01-01T00:00:00Z',
    'obligations': ['attribution'],
    'allow_export': True,
}
CONTEXT = {'purpose': 'research', 'region': 'cn', 'at': '2026-11-11T00:00:00Z',
           'fulfilled_obligations': ['attribution']}


def fetch(url):
    return urllib.request.urlopen(url).read()


def main():
    server, base = start()
    app = CorpusGate(clock=lambda: '2026-11-11T00:00:00.000000Z')
    try:
        # Input protocol still exposes the original behavior.
        listing = json.load(urllib.request.urlopen(base + '/mailbox'))
        assert listing['mailbox_epoch'] == 'epoch-1'
        from email import policy
        from email.parser import BytesParser
        raw = urllib.request.urlopen(base + '/mail/1').read()
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        assert mail.is_multipart() and len(list(mail.iter_attachments())) == 1
        newer = json.load(urllib.request.urlopen(base + '/mailbox?epoch=2'))
        assert newer['messages'][0]['uid'] == 1
        assert urllib.request.urlopen(base + '/mail/new1').read() != raw

        # Business side: register the authorized source and the text profile.
        sources.create(app.db, 'drill-source', AUTHORIZATION)
        profiles.put(app.db, 'default', 0, {})

        result = connector.sync(app.db, 'drill-mailbox', base, 'drill-source', 'default', CONTEXT,
                                mailbox_endpoint=base + '/mailbox', fetcher=fetch, sync_id='drill-epoch-1')
        assert result['fetched_total'] == 4, result
        states = {row['uid']: row['state'] for row in connector.list_deliveries(app.db)}
        assert states == {1: 'processed', 2: 'failed', 3: 'partial', 4: 'processed'}, states

        # The bad attachment never hides the good body of the same message.
        partial = connector.list_deliveries(app.db, state='partial')[0]
        part_states = {part['mime_path']: part['state'] for part in connector.list_parts(app.db, partial['delivery_id'])}
        assert part_states['1'] == 'processed' and part_states['3'] == 'failed', part_states
        failed_part = next(part for part in connector.list_parts(app.db, partial['delivery_id']) if part['mime_path'] == '3')
        assert failed_part['error']['code'] == 'mail_charset'

        # Forwarded duplicate collapses to one document with two delivery traces.
        deliveries = connector.list_deliveries(app.db)
        forwarded = next(row for row in deliveries if row['uid'] == 4)
        forward_parts = connector.list_parts(app.db, forwarded['delivery_id'])
        assert forward_parts[0]['state'] == 'duplicate', forward_parts
        traced = connector.trace_document(app.db, forward_parts[0]['document_id'])
        assert len(traced['parts']) == 2

        # Every document points back to a concrete mail part; integrity holds.
        for doc in documents.listing(app.db):
            assert connector.trace_document(app.db, doc['document_id'])['parts']

        # Epoch 2 reuses UID 1: the new generation is imported, not skipped.
        second = connector.sync(app.db, 'drill-mailbox', base, 'drill-source', 'default', CONTEXT, epoch=2,
                                mailbox_endpoint=base + '/mailbox', fetcher=fetch, sync_id='drill-epoch-2')
        assert second['epoch'] == 'epoch-2' and second['fetched_total'] == 1
        epoch_rows = connector.list_deliveries(app.db, epoch='epoch-2')
        assert len(epoch_rows) == 1 and epoch_rows[0]['uid'] == 1
        conflicts = connector.conflicts(app.db, '<maintenance-1@example.invalid>')
        assert len(conflicts) == 2 and all(row['state'] == 'conflict' for row in conflicts), conflicts

        # Re-running epoch 1 is idempotent and performs no refetches.
        seen = []
        result_again = connector.sync(app.db, 'drill-mailbox', base, 'drill-source', 'default', CONTEXT,
                                      mailbox_endpoint=base + '/mailbox',
                                      fetcher=lambda url: (seen.append(url), fetch(url))[1], sync_id='drill-rerun')
        assert seen == [] and all(row.get('skipped') == 'terminal' for row in result_again['deliveries'])

        # Reprocess the failed charset message via an explicit correction; retry adds no duplicate.
        broken = connector.list_deliveries(app.db, state='failed')[0]
        retried = connector.retry_delivery(app.db, broken['delivery_id'], CONTEXT,
                                           corrections={'': '恢复后的坏邮件正文 alpha beta gamma delta'},
                                           base_url=base, fetcher=fetch)
        assert retried['state'] == 'processed'
        again = connector.retry_delivery(app.db, broken['delivery_id'], CONTEXT,
                                         corrections={'': '恢复后的坏邮件正文 alpha beta gamma delta'},
                                         base_url=base, fetcher=fetch)
        assert again['parts'][0]['state'] == 'duplicate'

        # Withdrawing the source stops further imports; retained evidence stays explainable.
        sources.revoke(app.db, 'drill-source', 1, 'Shared mailbox withdrawn')
        try:
            connector.sync(app.db, 'drill-mailbox', base, 'drill-source', 'default', CONTEXT,
                           mailbox_endpoint=base + '/mailbox', fetcher=fetch, sync_id='after-revoke')
        except Exception as exc:
            assert getattr(exc, 'code', '') == 'mail_unauthorized'
        else:
            raise AssertionError('revoked source must stop mailbox import')
        evidence = connector.get_delivery(app.db, partial['delivery_id'])
        assert evidence['state'] == 'partial' and evidence['parts']
        assert app.db.audit_check()['ok']
    finally:
        server.shutdown()
        server.server_close()
        app.close()
    print('邮箱连接器端到端演练通过')


if __name__ == '__main__':
    main()
