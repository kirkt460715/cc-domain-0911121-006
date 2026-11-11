from tests.support import Case, record, authorization
from corpus_gate.dedup import similarity, clusters, diagnostics
from corpus_gate.review import desk
from corpus_gate import documents
from corpus_gate.ingest import sources


class ClusterTests(Case):
    def test_exact_duplicate_cluster_and_representative(self):
        self.ingest([record('a'), record('b'), record('other', 'new distinct vocabulary makes separate corpus content')])
        run = clusters.run(self.db, 'run')
        groups = clusters.groups(self.db, 'run')
        self.assertEqual(len(run['members']), 3)
        self.assertEqual(len(groups), 2)
        duplicate = next(group for group in groups if len(group['members']) == 2)
        self.assertEqual(duplicate['representative'], self.document('a')['document_id'])
        self.assertEqual(run['edges'][0]['kind'], 'exact')
        self.assertTrue(run['fresh'])
        explained = clusters.explain_membership(self.db, 'run', duplicate['representative'])
        self.assertIn('need not all', explained['transitive_warning'])
        self.assertEqual(explained['group']['cluster_id'], duplicate['cluster_id'])

    def test_similarity_exact_threshold_boundary(self):
        left = frozenset([('a',), ('b',), ('c',)])
        right = frozenset([('a',), ('b',), ('d',)])
        self.assertEqual(similarity.ratio(left, right)['intersection'], 2)
        self.assertEqual(similarity.ratio(left, right)['union'], 4)
        self.assertTrue(similarity.matches(left, right, 1, 2))
        self.assertFalse(similarity.matches(left, right, 51, 100))
        self.assertFalse(similarity.matches(frozenset(), frozenset(), 1, 2))
        self.assertEqual(similarity.shingles('a b', 3), frozenset([('a', 'b')]))
        self.fails('threshold', similarity.matches, left, right, 2, 1)
        self.assertEqual(similarity.distance(similarity.simhash('one two'), similarity.simhash('one two')), 0)

    def test_shared_shingle_candidates_no_false_negatives(self):
        signatures = {'a': similarity.shingles('one two three four', 1), 'b': similarity.shingles('one two three five', 1), 'c': similarity.shingles('other different words exist', 1)}
        pairs = similarity.candidates(signatures, 1, 2)
        self.assertIn(('a', 'b'), pairs)
        self.assertNotIn(('a', 'c'), pairs)
        self.assertNotIn(('b', 'c'), pairs)
        self.assertTrue(similarity.matches(signatures['a'], signatures['b'], 1, 2))
        explanation = similarity.explain('one two three four', 'one two three five', 1)
        self.assertEqual(explanation['intersection'], 3)
        self.assertEqual(explanation['union'], 5)
        self.assertEqual(explanation['left_only'], [['four']])
        self.assertIn('not_semantic', explanation['semantics'])

    def test_transitive_clusters_report_nonpairwise_members(self):
        self.ingest([record('a', 'a b c d e'), record('b', 'b c d e f'), record('c', 'c d e f g')])
        run = clusters.run(self.db, 'run', 3, 5, 1)
        self.assertEqual(len(clusters.groups(self.db, 'run')), 1)
        self.assertEqual(len(run['edges']), 2)
        cohesion = diagnostics.cohesion(self.db, 'run')
        self.assertTrue(cohesion['complete'])
        self.assertEqual(cohesion['inspected_pairs'], 3)
        self.assertEqual(len(cohesion['weak_pairs']), 1)
        bridges = diagnostics.bridges(self.db, 'run')
        self.assertEqual(len(bridges['bridges']), 2)
        self.assertFalse(diagnostics.cohesion(self.db, 'run', 1)['complete'])

    def test_constraints_prevent_transitive_merge(self):
        self.ingest([record('a', 'a b c d e'), record('b', 'b c d e f'), record('c', 'c d e f g')])
        desk.register(self.db, 'supervisor', 'supervisor')
        left = self.document('a')['document_id']
        right = self.document('c')['document_id']
        constraint = self.app.constraints.put(left, right, 'different', 0, 'supervisor', 'Separate semantic records')
        self.assertEqual(constraint['revision'], 1)
        run = clusters.run(self.db, 'run', 3, 5, 1)
        self.assertEqual(len(clusters.groups(self.db, 'run')), 2)
        groups = {row['document_id']: row['cluster_id'] for row in run['members']}
        self.assertNotEqual(groups[left], groups[right])
        self.assertEqual(len(run['parameters']['suppressed']), 1)
        self.assertEqual(len(self.app.constraints.history(left, right)), 1)
        self.fails('constraint_stale', self.app.constraints.put, left, right, 'same', 0, 'supervisor', 'Outdated')

    def test_constraint_removal_restores_auto_matching(self):
        self.ingest([record('a'), record('b')])
        desk.register(self.db, 'supervisor', 'supervisor')
        left = self.document('a')['document_id']
        right = self.document('b')['document_id']
        self.app.constraints.put(left, right, 'different', 0, 'supervisor', 'Distinct')
        clusters.run(self.db, 'before')
        self.assertEqual(len(clusters.groups(self.db, 'before')), 2)
        removed = self.app.constraints.remove(left, right, 1, 'supervisor', 'Correction')
        self.assertEqual(removed['state'], 'removed')
        self.assertEqual(removed['revision'], 2)
        clusters.run(self.db, 'after')
        self.assertEqual(len(clusters.groups(self.db, 'after')), 1)
        self.assertFalse(clusters.get(self.db, 'before')['fresh'])
        self.assertTrue(clusters.compare(self.db, 'before', 'after')['changes'])

    def test_manual_same_constraints_conflict_atomically(self):
        self.ingest([record('a'), record('b'), record('c')])
        desk.register(self.db, 'supervisor', 'supervisor')
        identities = sorted(self.document(key)['document_id'] for key in ('a', 'b', 'c'))
        self.app.constraints.put(identities[0], identities[1], 'same', 0, 'supervisor', 'Related')
        self.app.constraints.put(identities[1], identities[2], 'same', 0, 'supervisor', 'Related')
        self.app.constraints.put(identities[0], identities[2], 'different', 0, 'supervisor', 'Distinct')
        self.fails('constraint_contradiction', clusters.run, self.db, 'run')
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM cluster_runs'), 0)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM cluster_members'), 0)
        self.assertEqual(len(self.app.constraints.listing()), 3)
        self.assertTrue(self.db.audit_check()['ok'])

    def test_source_priority_selects_representative(self):
        sources.create(self.db, 'preferred', authorization(), 1)
        self.ingest([record('ordinary')])
        self.ingest([record('better')], 'preferred-batch', 'preferred')
        clusters.run(self.db, 'run')
        group = clusters.groups(self.db, 'run')[0]
        self.assertEqual(group['representative'], self.document('better', 'preferred')['document_id'])
        self.assertEqual(len(group['members']), 2)
        sources.revoke(self.db, 'preferred', 1, 'Unavailable')
        self.assertFalse(clusters.get(self.db, 'run')['fresh'])
        clusters.run(self.db, 'new-run')
        self.assertEqual(clusters.groups(self.db, 'new-run')[0]['representative'], self.document('ordinary')['document_id'])
        self.assertEqual(len(clusters.get(self.db, 'new-run')['members']), 1)

    def test_cluster_run_content_checksum(self):
        self.ingest([record('a'), record('b')])
        clusters.run(self.db, 'run')
        self.fails('cluster_exists', clusters.run, self.db, 'run')
        self.assertTrue(clusters.get(self.db, 'run')['fresh'])
        self.db.execute("UPDATE cluster_edges SET union_size=2 WHERE run_id='run'")
        self.fails('cluster_corrupt', clusters.get, self.db, 'run')
        from corpus_gate.inspection import check
        self.assertFalse(check(self.db)['ok'])
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM cluster_runs'), 1)
        self.assertTrue(self.db.integrity()['ok'])

    def test_labeled_threshold_diagnostics(self):
        self.ingest([record('a'), record('b'), record('c', 'entirely separate words are present')])
        a, b, c = [self.document(key)['document_id'] for key in ('a', 'b', 'c')]
        pairs = [{'left': a, 'right': b, 'duplicate': True}, {'left': a, 'right': c, 'duplicate': False}]
        result = diagnostics.evaluate_pairs(self.db, pairs)
        self.assertEqual(result['counts']['true_positive'], 1)
        self.assertEqual(result['counts']['true_negative'], 1)
        self.assertEqual(result['f1'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(result['precision'], {'numerator': 1, 'denominator': 1})
        swept = diagnostics.sweep(self.db, pairs, [{'numerator': 1, 'denominator': 2}, {'numerator': 4, 'denominator': 5}])
        self.assertEqual(len(swept['results']), 2)
        self.assertEqual(swept['scope'], 'local_labeled_pairs_only')
        self.fails('pair_duplicate', diagnostics.evaluate_pairs, self.db, pairs + [pairs[0]])
