from tests.support import Case, record, context
from corpus_gate.review import desk, redaction, sampling
from corpus_gate import documents, batch
from corpus_gate.ingest import sources


class ReviewTests(Case):
    def prepare_short(self):
        self.ingest([record(value='short')])
        desk.register(self.db, 'reviewer', 'reviewer')
        desk.register(self.db, 'supervisor', 'supervisor')
        return self.document()['document_id']

    def test_review_acknowledges_findings(self):
        identity = self.prepare_short()
        token = desk.claim(self.db, identity, 'reviewer')
        self.fails('review_findings', desk.decide, self.db, identity, token, 1, 'accept', 'Valid brief text', [])
        self.assertEqual(documents.get(self.db, identity)['state'], 'quarantined')
        result = desk.decide(self.db, identity, token, 1, 'accept', 'Valid brief text', ['too_short'])
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(desk.history(self.db, identity)[0]['actor'], 'reviewer')
        self.assertEqual(desk.queue(self.db), [])
        self.assertEqual(result['findings'][0]['code'], 'too_short')
        self.assertTrue(self.db.audit_check()['ok'])

    def test_sensitive_acceptance_requires_supervisor(self):
        self.ingest([record(value='contact synthetic@example.com for alpha beta gamma help')])
        identity = self.document()['document_id']
        desk.register(self.db, 'reviewer', 'reviewer')
        desk.register(self.db, 'supervisor', 'supervisor')
        token = desk.claim(self.db, identity, 'reviewer', 1)
        self.fails('review_supervisor', desk.decide, self.db, identity, token, 1, 'accept', 'Approved', ['email_like'])
        self.time = '2026-11-11T00:00:02.000000Z'
        supervisor = desk.claim(self.db, identity, 'supervisor')
        self.assertEqual(supervisor['generation'], 2)
        self.assertEqual(desk.decide(self.db, identity, supervisor, 1, 'accept', 'Synthetic address', ['email_like'])['state'], 'accepted')
        self.assertEqual(desk.history(self.db, identity)[0]['actor'], 'supervisor')

    def test_lease_takeover_fences_old_reviewer(self):
        identity = self.prepare_short()
        first = desk.claim(self.db, identity, 'reviewer', 1)
        self.fails('review_busy', desk.claim, self.db, identity, 'supervisor')
        self.time = '2026-11-11T00:00:01.000000Z'
        self.fails('review_expired', desk.decide, self.db, identity, first, 1, 'accept', 'Accept', ['too_short'])
        second = desk.claim(self.db, identity, 'supervisor')
        self.fails('review_fenced', desk.decide, self.db, identity, first, 1, 'accept', 'Accept', ['too_short'])
        self.assertEqual(documents.get(self.db, identity)['revision'], 1)
        self.assertEqual(second['generation'], first['generation'] + 1)
        self.assertEqual(desk.decide(self.db, identity, second, 1, 'reject', 'Insufficient', ['too_short'])['state'], 'rejected')
        self.assertEqual(len(desk.history(self.db, identity)), 1)

    def test_hard_rejection_cannot_be_overridden(self):
        self.ingest([record(value='')])
        identity = self.document()['document_id']
        desk.register(self.db, 'supervisor', 'supervisor')
        token = desk.claim(self.db, identity, 'supervisor')
        codes = sorted({finding['code'] for finding in self.document()['findings']})
        self.assertIn('empty_text', codes)
        self.fails('review_nonoverrideable', desk.decide, self.db, identity, token, 1, 'accept', 'Attempt override', codes)
        self.assertEqual(self.document()['state'], 'rejected')
        self.assertEqual(self.document()['revision'], 1)
        self.assertEqual(desk.history(self.db, identity), [])
        self.assertTrue(self.db.audit_check()['ok'])

    def test_new_version_blocks_existing_review(self):
        identity = self.prepare_short()
        token = desk.claim(self.db, identity, 'reviewer')
        self.ingest([record(version=2)], 'corrected')
        self.fails('review_state', desk.decide, self.db, identity, token, 1, 'accept', 'Old review', ['too_short'])
        self.assertEqual(self.document()['version'], 2)
        self.assertEqual(self.document()['state'], 'accepted')
        self.assertFalse(documents.get(self.db, identity)['is_head'])
        self.assertEqual(documents.get(self.db, identity)['state'], 'quarantined')
        self.assertEqual(len(documents.versions(self.db, 'source', 'doc')), 2)
        self.assertEqual(desk.history(self.db, identity), [])

    def test_disabled_actor_and_source_withdrawal_block_acceptance(self):
        identity = self.prepare_short()
        token = desk.claim(self.db, identity, 'reviewer')
        desk.disable(self.db, 'reviewer')
        self.fails('actor_unavailable', desk.heartbeat, self.db, identity, token)
        self.time = '2026-11-11T00:10:00.000000Z'
        token = desk.claim(self.db, identity, 'supervisor')
        sources.revoke(self.db, 'source', 1, 'Closed source')
        self.fails('source_revoked', desk.decide, self.db, identity, token, 1, 'accept', 'Review', ['too_short'])
        self.assertEqual(self.document()['state'], 'quarantined')
        self.assertEqual(desk.history(self.db, identity), [])
        self.assertEqual(self.document()['revision'], 1)

    def test_redaction_creates_a_corrected_version(self):
        self.ingest([record(value='contact synthetic@example.com for alpha beta gamma help')])
        old = self.document()
        desk.register(self.db, 'reviewer', 'reviewer')
        edits = redaction.suggested(self.db, old['document_id'])
        self.assertEqual(len(edits), 1)
        proposal = redaction.propose(self.db, old['document_id'], edits, 'default')
        self.assertEqual(proposal['quality_after']['state'], 'accepted')
        new = redaction.commit(self.db, proposal, 'reviewer', 'Remove synthetic address', 'corrected')
        self.assertEqual(new['version'], 2)
        self.assertNotIn('@', new['normalized'])
        self.assertEqual(new['metadata']['correction_of'], old['document_id'])
        self.assertEqual(documents.get(self.db, old['document_id'])['raw_text'], old['raw_text'])
        self.assertEqual(new['state'], 'accepted')
        self.assertFalse(documents.get(self.db, old['document_id'])['is_head'])

    def test_redaction_offset_and_overlap_guards(self):
        self.ingest([record(value='  contact synthetic@example.com for alpha beta gamma help  ')])
        identity = self.document()['document_id']
        self.fails('redaction_offsets', redaction.suggested, self.db, identity)
        self.fails('redaction_overlap', redaction.apply, 'abcdef', [{'start': 0, 'end': 3, 'replacement': 'x'}, {'start': 2, 'end': 4, 'replacement': 'y'}])
        self.fails('redaction_span', redaction.apply, 'abcdef', [{'start': 2, 'end': 2, 'replacement': ''}])
        self.assertEqual(redaction.apply('abcdef', [{'start': 1, 'end': 3, 'replacement': 'X'}]), 'aXdef')
        self.assertEqual(len(documents.versions(self.db, 'source', 'doc')), 1)
        self.assertEqual(self.document()['state'], 'quarantined')
        self.assertEqual(batch.get(self.db, 'batch')['total'], 1)

    def test_appeal_requires_independent_supervisor(self):
        identity = self.prepare_short()
        desk.register(self.db, 'other-supervisor', 'supervisor')
        token = desk.claim(self.db, identity, 'supervisor')
        desk.decide(self.db, identity, token, 1, 'reject', 'Too short', ['too_short'])
        appeal = self.app.appeals.submit('appeal', identity, 'requester', 'Valid caption')
        self.assertEqual(appeal['document_revision'], 2)
        self.fails('appeal_separation', self.app.appeals.decide, 'appeal', 'supervisor', True, 'Reopen')
        result = self.app.appeals.decide('appeal', 'other-supervisor', True, 'Review caption use')
        self.assertEqual(result['state'], 'reopened')
        self.assertEqual(documents.get(self.db, identity)['state'], 'quarantined')
        self.assertEqual(documents.get(self.db, identity)['revision'], 3)
        self.fails('appeal_terminal', self.app.appeals.decide, 'appeal', 'other-supervisor', False, 'Again')

    def test_review_sample_is_deterministic_and_revision_bound(self):
        self.ingest([record('a', 'short'), record('b', 'brief'), record('c')])
        sample = sampling.stratified_queue(self.db, 'seed', 2)
        self.assertEqual(sample, sampling.stratified_queue(self.db, 'seed', 2))
        self.assertEqual(len(sample['sample']), 2)
        self.assertTrue(sampling.validate_sample(self.db, sample)['fresh'])
        identity = sample['sample'][0]['document_id']
        desk.register(self.db, 'reviewer', 'reviewer')
        token = desk.claim(self.db, identity, 'reviewer')
        desk.decide(self.db, identity, token, 1, 'accept', 'Caption', ['too_short'])
        checked = sampling.validate_sample(self.db, sample)
        self.assertFalse(checked['fresh'])
        self.assertEqual(checked['changes'][0]['document_id'], identity)
        self.assertEqual(len(desk.queue(self.db)), 1)
