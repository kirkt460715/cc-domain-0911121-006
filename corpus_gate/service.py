from corpus_gate.database import Database
from corpus_gate.errors import shape, fail
from corpus_gate import documents, batch, inspection
from corpus_gate.ingest import sources, formats, recovery
from corpus_gate.text import profiles
from corpus_gate.text.drift import Baselines
from corpus_gate.text.annotations import Annotations
from corpus_gate.dedup import clusters, similarity
from corpus_gate.dedup.constraints import Constraints
from corpus_gate.review import desk, redaction
from corpus_gate.review.appeals import Appeals
from corpus_gate.releases import manifests, exporting, selection
from corpus_gate.releases.transfer import Receiver
from corpus_gate.releases.withdrawals import Notices


class CorpusGate:
    def __init__(self, path=':memory:', **options):
        self.db = Database(path, **options)
        self.constraints = Constraints(self.db)
        self.baselines = Baselines(self.db)
        self.annotations = Annotations(self.db)
        self.appeals = Appeals(self.db)
        self.receiver = Receiver(self.db)
        self.notices = Notices(self.db)
        from corpus_gate.text.search import SearchIndex
        self.search = SearchIndex(self.db)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close()

    def close(self):
        self.db.close()

    def dispatch(self, operation, arguments):
        if not isinstance(arguments, dict):
            fail('request_arguments', 'Operation arguments must be an object')
        registry = self.operations()
        if operation not in registry:
            fail('operation_missing', 'Unknown corpus operation', operation=operation)
        function, required, optional = registry[operation]
        shape(arguments, set(required.split()), set(optional.split()))
        return function(**arguments)

    def operations(self):
        def bind(function):
            return lambda **arguments: function(self.db, **arguments)
        from corpus_gate.dedup import diagnostics
        from corpus_gate.ingest import bundles, validation as intake_validation
        from corpus_gate.releases import validation, statistics
        from corpus_gate.review import sampling
        return {
            'intake.preflight': (bind(intake_validation.preflight), 'source_id profile_id records context', ''),
            'intake.manifest': (bind(intake_validation.manifest), 'source_id', ''),
            'intake.reconcile': (bind(intake_validation.reconcile_manifest), 'source_id external_entries', ''),
            'intake.capacity': (intake_validation.batch_capacity, 'records', 'maximum_bytes maximum_records'),
            'search.build': (self.search.build, 'index_id', ''),
            'search.status': (self.search.status, 'index_id', ''),
            'search.verify': (self.search.verify, 'index_id', ''),
            'search.query': (self.search.query, 'index_id query context', 'limit'),
            'search.concordance': (self.search.concordance, 'index_id document_id term context', 'radius'),
            'release.statistics': (bind(statistics.distribution), 'release_id', ''),
            'release.vocabulary': (bind(statistics.vocabulary), 'release_id', 'limit'),
            'release.overlap': (bind(statistics.overlap), 'release_id', ''),
            'release.histogram': (bind(statistics.length_histogram), 'release_id boundaries', ''),
            'review.sample': (bind(sampling.stratified_queue), 'seed', 'total by include_accepted'),
            'review.validate_sample': (bind(sampling.validate_sample), 'sample', ''),
            'diagnostics.pairs': (bind(diagnostics.evaluate_pairs), 'pairs', 'numerator denominator size'),
            'diagnostics.sweep': (bind(diagnostics.sweep), 'pairs thresholds', 'size'),
            'diagnostics.bridges': (bind(diagnostics.bridges), 'run_id', ''),
            'diagnostics.cohesion': (bind(diagnostics.cohesion), 'run_id', 'maximum_pairs'),
            'configuration.export': (bind(bundles.export_configuration), '', ''),
            'configuration.import': (bind(bundles.import_configuration), 'envelope', ''),
            'intake.export': (bind(bundles.export_batch), 'batch_id', ''),
            'intake.import': (bind(bundles.import_batch), 'batch_id envelope', ''),
            'profile.reprocess': (bind(bundles.reprocess_heads), 'source_id profile_id batch_id expected_corpus_revision', ''),
            'release.validate': (bind(validation.validate), 'release_id requirements', ''),
            'release.compare_contracts': (bind(validation.compare_contracts), 'release_id before after', ''),
            'source.create': (bind(sources.create), 'source_id authorization', 'priority'),
            'source.get': (bind(sources.get), 'source_id', ''),
            'source.revise': (bind(sources.revise), 'source_id expected_revision authorization priority reason', ''),
            'source.revoke': (bind(sources.revoke), 'source_id expected_revision reason', ''),
            'source.authorize': (bind(sources.authorize), 'source_id context', 'exporting'),
            'source.history': (bind(sources.history), 'source_id', ''),
            'source.list': (bind(sources.listing), '', ''),
            'profile.put': (bind(profiles.put), 'profile_id expected_revision definition', ''),
            'profile.get': (bind(profiles.get), 'profile_id', 'revision'),
            'profile.list': (bind(profiles.listing), '', ''),
            'profile.compare': (bind(profiles.compare_profiles), 'profile_id before after texts', ''),
            'batch.stage': (bind(batch.stage), 'batch_id source_id profile_id records', ''),
            'batch.get': (bind(batch.get), 'batch_id', ''),
            'batch.process': (bind(batch.process), 'batch_id expected_revision', 'limit'),
            'batch.run': (bind(batch.run), 'batch_id', 'limit'),
            'batch.cancel': (bind(batch.cancel), 'batch_id expected_revision', ''),
            'batch.records': (bind(batch.records), 'batch_id', 'state'),
            'batch.rebase': (bind(batch.rebase_source), 'batch_id expected_revision', ''),
            'batch.checkpoint': (bind(recovery.checkpoint), 'batch_id', ''),
            'batch.resume': (bind(recovery.resume), 'batch_id', 'limit'),
            'batch.retry': (bind(recovery.retry_failures), 'original_batch_id new_batch_id', 'profile_id corrections'),
            'batch.queue': (bind(recovery.queue), '', ''),
            'document.get': (bind(documents.get), 'document_id', ''),
            'document.list': (bind(documents.listing), '', 'state source_id heads_only after limit'),
            'document.chunks': (bind(documents.pieces), 'document_id', ''),
            'document.versions': (bind(documents.versions), 'source_id external_id', ''),
            'document.verify': (bind(documents.verify), 'document_id', ''),
            'document.withdraw': (bind(documents.withdraw), 'document_id expected_revision reason', ''),
            'review.register': (bind(desk.register), 'actor_id role', ''),
            'review.disable': (bind(desk.disable), 'actor_id', ''),
            'review.claim': (bind(desk.claim), 'document_id reviewer', 'ttl'),
            'review.heartbeat': (bind(desk.heartbeat), 'document_id token', 'ttl'),
            'review.decide': (bind(desk.decide), 'document_id token expected_revision decision reason acknowledged_findings', ''),
            'review.queue': (bind(desk.queue), '', 'finding source_id limit'),
            'review.history': (bind(desk.history), 'document_id', ''),
            'redaction.propose': (bind(redaction.propose), 'document_id edits profile_id', ''),
            'redaction.commit': (bind(redaction.commit), 'proposal reviewer reason batch_id', ''),
            'redaction.suggest': (bind(redaction.suggested), 'document_id', 'replacement'),
            'appeal.submit': (self.appeals.submit, 'appeal_id document_id requester reason', ''),
            'appeal.decide': (self.appeals.decide, 'appeal_id reviewer reopen reason', ''),
            'appeal.list': (self.appeals.listing, '', 'state'),
            'cluster.run': (bind(clusters.run), 'run_id', 'numerator denominator shingle_size maximum_documents'),
            'cluster.get': (bind(clusters.get), 'run_id', ''),
            'cluster.groups': (bind(clusters.groups), 'run_id', ''),
            'cluster.explain': (bind(clusters.explain_membership), 'run_id document_id', ''),
            'cluster.compare': (bind(clusters.compare), 'before after', ''),
            'similarity.explain': (similarity.explain, 'left_text right_text', 'size'),
            'constraint.put': (self.constraints.put, 'left_id right_id kind expected_revision actor reason', ''),
            'constraint.remove': (self.constraints.remove, 'left_id right_id expected_revision actor reason', ''),
            'constraint.list': (self.constraints.listing, '', 'active_only'),
            'constraint.history': (self.constraints.history, 'left_id right_id', ''),
            'release.select': (bind(selection.select), 'run_id context definition', ''),
            'release.prepare': (bind(manifests.prepare), 'release_id run_id context definition', ''),
            'release.commit': (bind(manifests.commit), 'release_id expected_revision', ''),
            'release.get': (bind(manifests.get), 'release_id', ''),
            'release.withdraw': (bind(manifests.withdraw), 'release_id expected_revision reason', ''),
            'release.history': (bind(manifests.history), 'release_id', ''),
            'release.list': (bind(manifests.listing), '', ''),
            'release.authorize': (bind(manifests.authorization), 'release_id', 'at'),
            'release.export': (bind(exporting.export), 'release_id', 'at split'),
            'release.jsonlines': (bind(exporting.jsonlines), 'release_id', 'at split chunks'),
            'release.csv': (bind(exporting.csv_export), 'release_id', 'at split'),
            'release.sample': (bind(exporting.sample), 'release_id seed', 'count'),
            'release.compare': (bind(exporting.compare), 'before after', ''),
            'baseline.capture': (self.baselines.capture, 'baseline_id source_id', 'minimum_documents'),
            'baseline.get': (self.baselines.get, 'baseline_id', ''),
            'baseline.compare': (self.baselines.compare, 'baseline_id', 'threshold_percent only_new'),
            'baseline.list': (self.baselines.listing, '', 'source_id'),
            'annotation.add': (self.annotations.add, 'annotation_id document_id start end label actor reason', ''),
            'annotation.retract': (self.annotations.retract, 'annotation_id expected_revision actor reason', ''),
            'annotation.list': (self.annotations.for_document, 'document_id', 'label'),
            'annotation.chunks': (self.annotations.chunk_projection, 'document_id', ''),
            'annotation.coverage': (self.annotations.coverage, 'document_id', ''),
            'annotation.history': (self.annotations.history, 'annotation_id', ''),
            'transfer.open': (self.receiver.open, 'transfer_id header', ''),
            'transfer.status': (self.receiver.status, 'transfer_id', ''),
            'transfer.receive': (self.receiver.receive, 'transfer_id ordinal part expected_digest', ''),
            'transfer.commit': (self.receiver.commit, 'transfer_id expected_revision', ''),
            'transfer.export': (self.receiver.export, 'transfer_id', ''),
            'notice.register': (self.notices.register, 'subscriber_id purpose', ''),
            'notice.subscribe': (self.notices.subscribe, 'subscriber_id release_id expected_digest', ''),
            'notice.reconcile': (self.notices.reconcile, 'at', ''),
            'notice.pending': (self.notices.pending, 'subscriber_id', ''),
            'notice.acknowledge': (self.notices.acknowledge, 'subscriber_id notice_id expected_digest receipt', ''),
            'notice.impact': (self.notices.impact, 'release_id', ''),
            'system.provenance': (bind(inspection.provenance), 'document_id', ''),
            'system.history': (bind(inspection.source_histories), '', ''),
            'system.check': (bind(inspection.check), '', ''),
            'system.overview': (bind(inspection.overview), '', ''),
            'system.quality': (bind(inspection.quality_report), '', 'source_id'),
            'system.source_impact': (bind(inspection.source_impact), 'source_id', ''),
            'audit.page': (bind(inspection.audit_page), '', 'after limit'),
        }

    def transaction(self, requests):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
            fail('transaction_size', 'Atomic request batch requires one to one hundred operations')
        with self.db.transaction():
            results = []
            for request in requests:
                shape(request, {'operation', 'arguments'})
                results.append(self.dispatch(request['operation'], request['arguments']))
            return results
