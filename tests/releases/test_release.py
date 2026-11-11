import copy
import json
from tests.support import Case, record, context, selection, authorization
from corpus_gate import documents
from corpus_gate.ingest import sources
from corpus_gate.dedup import clusters
from corpus_gate.releases import manifests, exporting, transfer, validation, statistics


class ReleaseTests(Case):
    def test_freeze_and_export_repeat_exactly(self):
        release = self.release([record('a'), record('b'), record('c', 'different vocabulary creates another training sample')])
        self.assertEqual(release['state'], 'published')
        self.assertEqual(release['revision'], 2)
        self.assertEqual(len(release['members']), 2)
        first = exporting.export(self.db, 'release')
        second = exporting.export(self.db, 'release')
        self.assertEqual(first, second)
        self.assertTrue(exporting.verify(first)['integrity'])
        self.assertFalse(exporting.verify(first)['live_authorization_checked'])
        self.assertEqual(len(manifests.history(self.db, 'release')), 2)
        self.assertEqual(manifests.get(self.db, 'release')['digest'], release['digest'])

    def test_duplicate_components_never_cross_splits(self):
        release = self.release([record('a'), record('b'), record('c')], selection(include_all_duplicates=True))
        self.assertEqual(len(release['members']), 3)
        self.assertEqual(len({row['cluster_id'] for row in release['members']}), 1)
        self.assertEqual(len({row['split'] for row in release['members']}), 1)
        self.assertEqual(len(exporting.export(self.db, 'release')['payload']['members']), 3)
        report = statistics.distribution(self.db, 'release')
        self.assertEqual(len(report['splits']), 1)
        self.assertEqual(report['splits'][0]['documents'], 3)
        self.assertEqual(report['splits'][0]['clusters'], 1)
        self.assertEqual(statistics.overlap(self.db, 'release')['comparisons'], [])

    def test_corpus_change_blocks_prepared_release(self):
        self.ingest()
        clusters.run(self.db, 'run')
        prepared = manifests.prepare(self.db, 'release', 'run', context(), selection())
        self.assertEqual(prepared['state'], 'prepared')
        self.fails('release_not_published', exporting.export, self.db, 'release')
        self.ingest([record('new', 'new unrelated source document contains useful words')], 'later')
        self.fails('release_corpus_changed', manifests.commit, self.db, 'release', 1)
        self.assertEqual(manifests.get(self.db, 'release')['state'], 'prepared')
        self.fails('cluster_stale', manifests.prepare, self.db, 'new-release', 'run', context(), selection())
        self.assertEqual(manifests.get(self.db, 'release')['digest'], prepared['digest'])
        self.assertFalse(clusters.get(self.db, 'run')['fresh'])

    def test_new_head_does_not_rewrite_published_release(self):
        release = self.release()
        old = self.document()
        self.ingest([record(version=2, value='updated source contents contain new useful facts')], 'update')
        frozen = manifests.get(self.db, 'release')
        self.assertEqual(frozen['digest'], release['digest'])
        self.assertEqual(frozen['members'][0]['payload']['version'], 1)
        self.assertEqual(frozen['members'][0]['payload']['text'], old['normalized'])
        self.assertEqual(self.document()['version'], 2)
        self.assertTrue(manifests.authorization(self.db, 'release')['allowed'])
        self.assertEqual(exporting.export(self.db, 'release')['payload']['members'][0]['document_id'], old['document_id'])
        self.assertFalse(documents.get(self.db, old['document_id'])['is_head'])

    def test_source_revision_and_revocation_block_exports(self):
        release = self.release()
        before = exporting.export(self.db, 'release')
        sources.revise(self.db, 'source', 1, authorization(), 10, 'Revised source agreement')
        decision = manifests.authorization(self.db, 'release')
        self.assertFalse(decision['allowed'])
        self.assertEqual(decision['failures'][0]['code'], 'source_revision')
        self.fails('release_authorization', exporting.export, self.db, 'release')
        self.assertTrue(exporting.verify(before)['integrity'])
        sources.revoke(self.db, 'source', 2, 'Withdraw')
        self.assertFalse(manifests.authorization(self.db, 'release')['allowed'])
        self.assertEqual(manifests.get(self.db, 'release')['digest'], release['digest'])

    def test_expiry_and_document_withdrawal(self):
        release = self.release()
        self.assertFalse(manifests.authorization(self.db, 'release', '2027-01-01T00:00:00Z')['allowed'])
        self.fails('release_authorization', exporting.export, self.db, 'release', '2027-01-01T00:00:00Z')
        identity = self.document()['document_id']
        documents.withdraw(self.db, identity, 1, 'Review needed')
        self.assertEqual(documents.get(self.db, identity)['state'], 'withdrawn')
        self.assertFalse(manifests.authorization(self.db, 'release')['allowed'])
        self.fails('release_authorization', exporting.export, self.db, 'release')
        self.assertEqual(manifests.get(self.db, 'release')['members'][0]['payload']['document_revision'], 1)
        self.assertEqual(manifests.get(self.db, 'release')['state'], 'published')

    def test_source_and_token_quotas(self):
        self.ingest([record('a'), record('b', 'new different words create useful text'), record('c', 'another completely distinct document goes here')])
        clusters.run(self.db, 'run')
        options = selection(source_limits={'source': 1})
        selected = self.app.dispatch('release.select', {'run_id': 'run', 'context': context(), 'definition': options})
        self.assertEqual(selected['counts']['documents'], 1)
        self.assertEqual(selected['counts']['sources'], {'source': 1})
        self.assertEqual(len(selected['excluded']), 2)
        self.assertTrue(all('source_quota' in row['details'] for row in selected['excluded']))
        self.fails('release_empty', manifests.prepare, self.db, 'empty', 'run', context(), selection(max_tokens=1))
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM releases'), 0)
        self.assertEqual(selected['corpus_revision'], self.db.revision())

    def test_export_encodings_and_deterministic_samples(self):
        release = self.release([record('a'), record('b', 'one two three four five six')])
        lines = exporting.jsonlines(self.db, 'release')
        parsed = [json.loads(line) for line in lines['content'].splitlines()]
        self.assertEqual(len(parsed), 2)
        self.assertEqual(lines['records'], 2)
        self.assertIn('attribution', parsed[0]['obligations'])
        csv = exporting.csv_export(self.db, 'release')
        self.assertTrue(csv['content'].startswith('document_id,source_id,split,cluster_id,text'))
        self.assertEqual(csv['documents'], 2)
        self.assertEqual(exporting.sample(self.db, 'release', 'seed', 1), exporting.sample(self.db, 'release', 'seed', 1))
        self.assertEqual(len(exporting.sample(self.db, 'release', 'seed', 1)['sample']), 1)
        self.fails('release_split', exporting.export, self.db, 'release', split='unknown')

    def test_manifest_tampering_detected(self):
        self.release()
        envelope = exporting.export(self.db, 'release')
        altered = copy.deepcopy(envelope)
        altered['payload']['members'][0]['payload']['text'] = 'tampered'
        self.fails('export_digest', exporting.verify, altered)
        from corpus_gate.errors import digest
        altered['sha256'] = digest(altered['payload'])
        self.fails('export_document_digest', exporting.verify, altered)
        self.db.execute("UPDATE release_members SET digest='bad' WHERE release_id='release'")
        self.fails('release_member_corrupt', manifests.get, self.db, 'release')
        self.assertTrue(exporting.verify(envelope)['integrity'])
        self.assertFalse(exporting.verify(envelope)['live_authorization_checked'])

    def test_transfer_out_of_order_resume_and_visibility(self):
        self.release([record('a'), record('b', 'other distinct text has many useful words')])
        envelope = exporting.export(self.db, 'release')
        package = transfer.package(envelope)
        receiver = self.app.receiver
        receiver.open('transfer', package['header'])
        last = package['parts'][-1]
        receiver.receive('transfer', last['ordinal'], last['payload'], last['digest'])
        self.assertEqual(receiver.status('transfer')['missing'], [0])
        self.fails('transfer_unverified', receiver.export, 'transfer')
        self.fails('transfer_incomplete', receiver.commit, 'transfer', receiver.status('transfer')['revision'])
        first = package['parts'][0]
        receiver.receive('transfer', first['ordinal'], first['payload'], first['digest'])
        revision = receiver.status('transfer')['revision']
        self.assertEqual(receiver.receive('transfer', first['ordinal'], first['payload'], first['digest'])['revision'], revision)
        receiver.commit('transfer', revision)
        self.assertEqual(receiver.export('transfer'), envelope)
        self.assertEqual(receiver.status('transfer')['state'], 'verified')

    def test_transfer_conflict_and_corruption(self):
        self.release()
        package = transfer.package(exporting.export(self.db, 'release'))
        receiver = self.app.receiver
        receiver.open('transfer', package['header'])
        part = package['parts'][0]
        self.fails('transfer_part_digest', receiver.receive, 'transfer', 0, part['payload'], 'bad')
        receiver.receive('transfer', 0, part['payload'], part['digest'])
        self.fails('transfer_ordinal', receiver.receive, 'transfer', 1, part['payload'], part['digest'])
        self.db.execute("UPDATE transfer_parts SET digest='bad' WHERE transfer_id='transfer'")
        self.fails('transfer_stored_digest', receiver.commit, 'transfer', receiver.status('transfer')['revision'])
        self.assertEqual(receiver.status('transfer')['state'], 'receiving')
        self.assertEqual(receiver.status('transfer')['missing'], [])

    def test_release_contract_and_statistics(self):
        self.release([record('a'), record('b', 'one two three four five six')])
        valid = validation.validate(self.db, 'release', {})
        self.assertTrue(valid['ok'])
        invalid = validation.validate(self.db, 'release', {'minimum_sources': 2, 'maximum_source_percent': 50})
        self.assertFalse(invalid['ok'])
        codes = {row['code'] for row in invalid['problems']}
        self.assertIn('too_few_sources', codes)
        self.assertIn('source_concentration', codes)
        self.assertEqual(statistics.length_histogram(self.db, 'release', [5, 6, 10])['total'], 2)
        self.assertEqual(statistics.vocabulary(self.db, 'release')['total_tokens'], 11)
        self.assertEqual(len(validation.compare_contracts(self.db, 'release', {}, {'minimum_sources': 2})['introduced']), 1)

    def test_notices_preserve_receipts(self):
        release = self.release()
        notices = self.app.notices
        notices.register('consumer', 'research')
        notices.subscribe('consumer', 'release', release['digest'])
        self.assertEqual(notices.reconcile('2026-11-11T00:00:00Z')['count'], 0)
        sources.revoke(self.db, 'source', 1, 'Withdraw source')
        self.assertEqual(notices.reconcile('2026-11-11T00:00:00Z')['count'], 1)
        self.assertEqual(notices.reconcile('2026-11-11T00:00:00Z')['count'], 0)
        notice = notices.pending('consumer')[0]
        self.assertEqual(notice['payload']['release_digest'], release['digest'])
        self.fails('notice_identity', notices.acknowledge, 'other', notice['notice_id'], notice['digest'], 'receipt')
        self.assertEqual(notices.acknowledge('consumer', notice['notice_id'], notice['digest'], 'receipt')['state'], 'acknowledged')
        self.assertEqual(notices.pending('consumer'), [])
        self.fails('notice_receipt', notices.acknowledge, 'consumer', notice['notice_id'], notice['digest'], 'different')

    def test_explicit_release_withdrawal_is_irreversible(self):
        release = self.release()
        result = manifests.withdraw(self.db, 'release', 2, 'Dataset recalled')
        self.assertEqual(result['state'], 'withdrawn')
        self.assertEqual(result['revision'], 3)
        self.assertEqual(result['digest'], release['digest'])
        self.fails('release_not_published', exporting.export, self.db, 'release')
        self.fails('release_state', manifests.commit, self.db, 'release', 3)
        self.assertEqual(manifests.history(self.db, 'release')[-1]['body']['reason'], 'Dataset recalled')
        self.assertEqual(manifests.withdraw(self.db, 'release', 3, 'Already recalled')['revision'], 3)
        self.assertFalse(manifests.authorization(self.db, 'release')['allowed'])
